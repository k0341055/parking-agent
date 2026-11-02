"""
用瀏覽器跑完整流程：網站的真實前端 + 本機假 API（見 mock_site.py），不會送到真實網站。
第一次執行要連網下載網站的靜態檔；網站改版導致流程壞掉時，這裡會失敗。

    python -m unittest tests.test_e2e
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "parking_book"))
sys.path.insert(0, str(ROOT / "tests"))

from mock_site import MockSite, future_day  # noqa: E402

site = MockSite().start()
os.environ["PARKING_BASE_URL"] = site.url
import parking_agent_v2 as agent  # noqa: E402

NAME, PLATE = "王小明", "AB-1234"


def make_booking(day, **kw):
    from datetime import date
    defaults = dict(alias="Booking_1", site="p4", date=date.fromisoformat(day), days=7,
                    recipients=["Recipient_1"], name=NAME, plate=PLATE, rev=1)
    defaults.update(kw)
    return agent.Booking(**defaults)


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.day = future_day(18)
        site.left = {future_day(10): 0, self.day: 5, future_day(19): 0}
        site.pause = False
        site.order_status, site.order_body = 200, ""
        site.record_on_success = True
        site.orders.clear()
        site.records.clear()

    def test_submit_sends_the_right_fields(self):
        outcome = asyncio.run(agent.attempt_booking(make_booking(self.day)))
        self.assertEqual(outcome.kind, "ok", outcome.detail)
        self.assertEqual(len(site.orders), 1)
        order = site.orders[0]
        self.assertEqual(order["Model_StartDate"], self.day)
        self.assertEqual(int(order["Model_ReservedDay"]), 7)
        self.assertEqual(order["Model_Contact"], NAME)
        self.assertEqual(order["Model_CarShow"], PLATE)
        self.assertEqual(order["ReCAPTCHAToken"], "test-token")

    def test_captcha_required_stops_and_reports(self):
        site.order_status = 428
        outcome = asyncio.run(agent.attempt_booking(make_booking(self.day)))
        self.assertEqual(outcome.kind, "captcha")
        self.assertEqual(len(site.orders), 1)

    def test_rejection_message_is_scrubbed(self):
        site.order_status = 400
        site.order_body = f"車號 [{PLATE}] 已於 2026/11/23 登記預約"
        outcome = asyncio.run(agent.attempt_booking(make_booking(self.day)))
        self.assertEqual(outcome.kind, "rejected")
        self.assertIn("已於", outcome.detail)
        self.assertNotIn(PLATE, outcome.detail)

    def test_full_row_is_not_submitted(self):
        site.left[self.day] = 0
        outcome = asyncio.run(agent.attempt_booking(make_booking(self.day)))
        self.assertEqual(outcome.kind, "full")
        self.assertEqual(site.orders, [])

    def test_dry_run_never_submits(self):
        outcome = asyncio.run(agent.attempt_booking(make_booking(self.day), dry_run=True))
        self.assertEqual(outcome.kind, "dry_ok", outcome.detail)
        self.assertEqual(site.orders, [])

    def test_site_form_rejects_bad_plate_before_sending(self):
        outcome = asyncio.run(agent.attempt_booking(make_booking(self.day, plate="ABC1234")))
        self.assertEqual(outcome.kind, "failed")
        self.assertIn("車號規格錯誤", outcome.detail)
        self.assertEqual(site.orders, [])

    def test_pause_dialog_is_reported(self):
        site.pause = True
        outcome = asyncio.run(agent.attempt_booking(make_booking(self.day)))
        self.assertEqual(outcome.kind, "failed")
        self.assertIn("整理中", outcome.detail)

    def test_full_round_with_real_api_calls(self):
        """從查名額、通知、送出到查記錄結案，一輪走完。"""
        sent = []
        status = {"bookings": {}}
        b = make_booking(self.day)
        with mock.patch.object(agent, "_send_to_recipient", lambda a, s, body, bk: sent.append(s) or True):
            asyncio.run(agent.check_round([b], status, dict(agent.DEFAULT_SETTINGS), {"fails": 0}))
        self.assertEqual(status["bookings"][b.alias]["state"], "booked")
        self.assertEqual(status["bookings"][b.alias]["detail"], "記錄已確認")
        self.assertEqual(len(site.orders), 1)
        self.assertEqual(len(sent), 2)


if __name__ == "__main__":
    unittest.main()
