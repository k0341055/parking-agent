"""
監控邏輯的單元測試：名額、狀態、通知次數、個資不外洩。不開瀏覽器也不連網。

    python -m unittest tests.test_agent
"""

import asyncio
import json
import os
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "parking_book"))
import parking_agent_v2 as agent  # noqa: E402

NAME, PLATE = "王小明", "AB-1234"
SETTINGS = dict(agent.DEFAULT_SETTINGS)


def make_booking(**kw):
    defaults = dict(
        alias="Booking_1", site="p4", date=agent.now_tw().date() + timedelta(days=18), days=7,
        recipients=["Recipient_1"], name=NAME, plate=PLATE, rev=1,
    )
    defaults.update(kw)
    return agent.Booking(**defaults)


class ConfigTest(unittest.TestCase):
    def test_settings_are_clamped(self):
        s = agent.parse_settings({"settings": {"run_minutes": 99, "interval_min": 1, "interval_max": 0, "enabled": False}})
        self.assertEqual(s["run_minutes"], 12)
        self.assertEqual(s["interval_min"], 5)
        self.assertEqual(s["interval_max"], 5)
        self.assertFalse(s["enabled"])

    def test_invalid_bookings_are_dropped(self):
        day = "2026-12-01"
        data = {"bookings": [
            {"id": "Booking_1", "site": "p4", "date": day, "days": 7},
            {"id": "Booking_1", "site": "p4", "date": day, "days": 7},   # 重複
            {"id": "nope", "site": "p4", "date": day, "days": 7},
            {"id": "Booking_3", "site": "x", "date": day, "days": 7},
            {"id": "Booking_4", "site": "p4", "date": "bad", "days": 7},
            {"id": "Booking_5", "site": "p4", "date": day, "days": 2},   # 少於 3 天
        ]}
        self.assertEqual([b.alias for b in agent.parse_bookings(data)], ["Booking_1"])

    def test_profile_from_secret(self):
        with mock.patch.dict(os.environ, {"BOOKING_2": json.dumps({"name": NAME, "plate": "ab-1234"})}):
            self.assertEqual(agent.load_profile(2), (NAME, PLATE))
        with mock.patch.dict(os.environ, {"BOOKING_2": "not json"}):
            self.assertEqual(agent.load_profile(2), ("", ""))
        with mock.patch.dict(os.environ, {"BOOKING_2": json.dumps({"name": NAME, "plate": "壞車牌"})}):
            self.assertEqual(agent.load_profile(2), ("", ""))

    def test_contact_secret_and_legacy_fallback(self):
        line = "U" + "a" * 32
        with mock.patch.dict(os.environ, {"RECIPIENT_2": f"a@x.com,{line}"}):
            c = agent.load_contact(2)
            self.assertEqual((c.emails, c.line_ids), (["a@x.com"], [line]))
        env = {"GMAIL_RECIPIENTS": "a@x.com, b@x.com", "LINE_USER_ID": line, "RECIPIENT_1": "", "RECIPIENT_2": ""}
        with mock.patch.dict(os.environ, env):
            first, second = agent.load_contact(1), agent.load_contact(2)
            self.assertEqual((first.emails, first.line_ids), (["a@x.com"], [line]))
            self.assertEqual((second.emails, second.line_ids), (["b@x.com"], []))

    def test_rule_dates(self):
        b = make_booking(date=date(2026, 11, 21), days=7)
        self.assertEqual(b.end_date, date(2026, 11, 27))
        self.assertEqual(b.open_date, date(2026, 11, 1))
        # 網站範例：01/01 進場，12/12 開放
        self.assertEqual(make_booking(date=date(2027, 1, 1)).open_date, date(2026, 12, 12))


class MidnightBurstTest(unittest.TestCase):
    def at(self, *args):
        return datetime(*args, tzinfo=agent.TZ)

    def test_burst_only_when_target_opens_tonight(self):
        target = make_booking(date=date(2026, 11, 23))   # 11/03 00:00 開放
        self.assertTrue(agent.in_midnight_burst(self.at(2026, 11, 2, 23, 56), [target]))
        self.assertTrue(agent.in_midnight_burst(self.at(2026, 11, 3, 0, 2), [target]))
        self.assertFalse(agent.in_midnight_burst(self.at(2026, 11, 3, 0, 3), [target]))
        self.assertFalse(agent.in_midnight_burst(self.at(2026, 11, 2, 23, 54), [target]))
        self.assertFalse(agent.in_midnight_burst(self.at(2026, 11, 1, 23, 56), [target]))

    def test_short_interval_only_right_around_midnight(self):
        self.assertEqual(agent.wait_seconds(self.at(2026, 11, 2, 23, 59, 50), SETTINGS, True), 3)
        self.assertEqual(agent.wait_seconds(self.at(2026, 11, 3, 0, 1), SETTINGS, True), 3)
        self.assertGreaterEqual(agent.wait_seconds(self.at(2026, 11, 2, 23, 57), SETTINGS, True), 15)
        self.assertGreaterEqual(agent.wait_seconds(self.at(2026, 11, 3, 0, 1), SETTINGS, False), 15)


class Harness:
    """把瀏覽器、記錄查詢、通知換成假的，記下每一次呼叫。"""

    def __init__(self, test, outcomes=(), records=()):
        self.status = {"bookings": {}}
        self.sent = []
        self.attempts = 0
        self.outcomes = list(outcomes)
        self.records = list(records)    # 每次查詢依序回傳；用完回傳 None
        self.now = agent.now_tw()
        for target, new in {
            "attempt_booking": self._attempt,
            "find_record": self._record,
            "_send_to_recipient": self._send,
            "now_tw": lambda: self.now,
        }.items():
            patcher = mock.patch.object(agent, target, new)
            patcher.start()
            test.addCleanup(patcher.stop)

    async def _attempt(self, booking, **kw):
        self.attempts += 1
        return self.outcomes.pop(0)

    def _record(self, booking):
        return self.records.pop(0) if self.records else None

    def _send(self, alias, subject, body, booking):
        self.sent.append((alias, subject, body))
        return True

    def run(self, booking, left, settings=SETTINGS):
        lefts = {booking.date.isoformat(): left} if left is not None else {}
        asyncio.run(agent.check_booking(booking, lefts, self.status, settings))

    def state(self, booking):
        return self.status["bookings"][booking.alias]["state"]

    def subjects(self):
        return [s for _, s, _ in self.sent]


class WatchTest(unittest.TestCase):
    def test_full_and_not_open_stay_quiet(self):
        h = Harness(self)
        b = make_booking()
        h.run(b, None)
        self.assertEqual(h.state(b), "waiting")
        self.assertIn("00:00 開放登記", h.status["bookings"][b.alias]["detail"])
        h.run(b, 0)
        self.assertEqual(h.state(b), "full")
        self.assertEqual((h.sent, h.attempts), ([], 0))

    def test_available_then_booked_notifies_twice_and_stops(self):
        h = Harness(self, outcomes=[agent.Outcome("ok")], records=[None, {"Model_StartDate": "x"}])
        b = make_booking()
        h.run(b, 5)
        self.assertEqual(h.state(b), "booked")
        self.assertEqual(h.attempts, 1)
        self.assertEqual(len(h.sent), 2)
        self.assertIn("有名額", h.sent[0][1])
        self.assertIn("預約成功", h.sent[1][1])
        # 可以手動完成的資料都在第一則
        for needle in (PLATE, NAME, "天數：7", b.index_url, "#quick=Booking_1"):
            self.assertIn(needle, h.sent[0][2])
        self.assertEqual(agent.active_bookings([b], h.status), [])

    def test_manual_booking_is_detected_before_submitting(self):
        h = Harness(self, records=[{"Model_StartDate": "x"}])
        b = make_booking()
        h.run(b, 3)
        self.assertEqual(h.state(b), "booked")
        self.assertEqual(h.attempts, 0)
        self.assertEqual(len(h.sent), 1)
        self.assertIn("預約成功", h.sent[0][1])

    def test_captcha_needs_human_without_spamming(self):
        captcha = agent.Outcome("captcha", "網站要求人工驗證（reCAPTCHA）")
        h = Harness(self, outcomes=[captcha, captcha, captcha])
        b = make_booking()
        h.run(b, 5)
        self.assertEqual(h.state(b), "needs_manual")
        self.assertEqual(h.attempts, 1)
        self.assertEqual(len(h.sent), 2)     # 有名額 + 請人工預約
        self.assertIn("請人工預約", h.sent[1][1])
        self.assertIn(PLATE, h.sent[1][2])

        h.run(b, 5)                          # 下一輪：還沒到重試時間，什麼都不做
        self.assertEqual((h.attempts, len(h.sent)), (1, 2))

        h.now += timedelta(minutes=SETTINGS["retry_minutes"])
        h.run(b, 5)                          # 到重試時間：再試一次但不重複通知
        self.assertEqual((h.attempts, len(h.sent)), (2, 2))

        h.now += timedelta(minutes=SETTINGS["reminder_minutes"])
        h.run(b, 5)                          # 到提醒時間：才再通知
        self.assertEqual((h.attempts, len(h.sent)), (3, 4))

    def test_reminder_before_retry_keeps_the_failure_reason(self):
        h = Harness(self, outcomes=[agent.Outcome("captcha", "網站要求人工驗證（reCAPTCHA）")])
        b = make_booking()
        settings = dict(SETTINGS, retry_minutes=60, reminder_minutes=5)
        h.run(b, 5, settings)
        h.now += timedelta(minutes=5)
        h.run(b, 5, settings)                # 該提醒但還沒到重試時間
        self.assertEqual(h.attempts, 1)
        self.assertEqual(h.state(b), "needs_manual")
        self.assertIn("reCAPTCHA", h.status["bookings"][b.alias]["detail"])
        self.assertIn("稍後會再試", h.sent[-1][2])

    def test_last_seat_taken_by_manual_booking_closes_it(self):
        h = Harness(self, outcomes=[agent.Outcome("captcha", "x")])
        b = make_booking()
        h.run(b, 1)
        self.assertEqual(h.state(b), "needs_manual")
        h.records = [{"Model_StartDate": "x"}]   # 使用者手動搶到最後一位，名額變 0
        h.run(b, 0)
        self.assertEqual(h.state(b), "booked")
        self.assertIn("預約成功", h.sent[-1][1])

    def test_full_again_resets_the_episode(self):
        h = Harness(self, outcomes=[agent.Outcome("captcha", "x"), agent.Outcome("captcha", "x")])
        b = make_booking()
        h.run(b, 1)
        h.run(b, 0)
        self.assertEqual(h.state(b), "full")
        h.run(b, 1)                          # 又有名額：馬上通知、馬上再試
        self.assertEqual((h.attempts, len(h.sent)), (2, 4))

    def test_seat_taken_before_submit(self):
        h = Harness(self, outcomes=[agent.Outcome("full")])
        b = make_booking()
        h.run(b, 1)
        self.assertEqual(h.state(b), "full")
        self.assertIn("被搶走", h.sent[-1][1])

    def test_rejected_because_of_other_registration(self):
        message = f"網站回應 HTTP 400：車號 [{PLATE}] 已於 2026/11/23 登記預約"
        h = Harness(self, outcomes=[agent.Outcome("rejected", agent._scrub(message, make_booking()))])
        b = make_booking()
        h.run(b, 2)
        self.assertEqual(h.state(b), "needs_manual")
        detail = h.status["bookings"][b.alias]["detail"]
        self.assertIn("先取消舊登記", detail)
        self.assertIn("orderrecord", detail)
        self.assertNotIn(PLATE, detail)

    def test_already_registered_same_day_counts_as_booked(self):
        h = Harness(self, outcomes=[agent.Outcome("rejected", "網站回應 HTTP 400：車號 [車牌] 已於 11/21 登記預約")],
                    records=[None, {"Model_StartDate": "x"}])
        b = make_booking()
        h.run(b, 2)
        self.assertEqual(h.state(b), "booked")

    def test_unknown_after_submit_checks_record(self):
        h = Harness(self, outcomes=[agent.Outcome("unknown", "送出後網站沒有回應")], records=[None])
        b = make_booking()
        with mock.patch.object(agent.asyncio, "sleep", mock.AsyncMock()):
            h.run(b, 2)
        self.assertEqual(h.state(b), "needs_manual")
        self.assertIn("請人工預約", h.sent[-1][1])

    def test_auto_submit_off_only_notifies(self):
        h = Harness(self)
        b = make_booking(auto_submit=False)
        h.run(b, 2)
        self.assertEqual((h.state(b), h.attempts, len(h.sent)), ("needs_manual", 0, 1))
        self.assertIn("手動預約", h.sent[0][2])

    def test_missing_profile_does_not_submit(self):
        h = Harness(self)
        b = make_booking(name="", plate="")
        h.run(b, 2)
        self.assertEqual((h.state(b), h.attempts), ("needs_manual", 0))
        self.assertIn("BOOKING_1", h.status["bookings"][b.alias]["detail"])

    def test_past_deadline_expires_and_stops(self):
        h = Harness(self)
        b = make_booking(date=agent.now_tw().date() + timedelta(days=1))
        h.run(b, 2)
        self.assertEqual(h.state(b), "expired")
        self.assertEqual(agent.active_bookings([b], h.status), [])

    def test_changing_the_booking_restarts_it(self):
        h = Harness(self, outcomes=[agent.Outcome("ok")], records=[None, {"Model_StartDate": "x"}])
        b = make_booking()
        h.run(b, 5)
        self.assertEqual(agent.active_bookings([b], h.status), [])
        changed = make_booking(rev=2)
        self.assertEqual(agent.active_bookings([changed], h.status), [changed])

    def test_status_never_contains_personal_data(self):
        message = f"網站回應 HTTP 400：{NAME} 車號 [{PLATE}] 已於 2026/11/23 登記預約"
        h = Harness(self, outcomes=[agent.Outcome("rejected", agent._scrub(message, make_booking()))])
        b = make_booking()
        h.run(b, 2)
        dumped = json.dumps(h.status, ensure_ascii=False)
        self.assertNotIn(PLATE, dumped)
        self.assertNotIn(NAME, dumped)


class HealthTest(unittest.TestCase):
    def test_repeated_api_failures_alert_once(self):
        h = Harness(self)
        b = make_booking()
        health = {"fails": 0}
        with mock.patch.object(agent, "fetch_left", side_effect=agent.requests.ConnectionError()):
            for _ in range(agent.HEALTH_FAIL_ROUNDS + 3):
                asyncio.run(agent.check_round([b], h.status, SETTINGS, health))
        self.assertEqual(len(h.sent), 1)
        self.assertIn("查詢失敗", h.sent[0][1])

    def test_pause_is_not_a_failure(self):
        h = Harness(self)
        b = make_booking()
        health = {"fails": 0}
        with mock.patch.object(agent, "fetch_left", return_value=None):
            asyncio.run(agent.check_round([b], h.status, SETTINGS, health))
        self.assertEqual((health["fails"], h.sent), (0, []))


if __name__ == "__main__":
    unittest.main()
