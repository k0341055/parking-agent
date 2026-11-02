"""
UI 網頁的瀏覽器測試：GitHub API 全部換成假的，確認個資只進 Secrets、不進 commit，一鍵填單能填好網站表單。
需要多裝 pynacl（驗證加密的 Secret 解得開）。

    pip install pynacl
    python -m unittest tests.test_ui
"""

import base64
import functools
import json
import os
import sys
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from nacl.public import PrivateKey, SealedBox
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from mock_site import MockSite, future_day  # noqa: E402

NAME, PLATE, EMAIL = "王小明", "AB-1234", "someone@example.com"
LINE_ID = "U" + "0123456789abcdef" * 2
SHOTS = os.environ.get("SCREENSHOT_DIR")


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class FakeGitHub:
    """夠用的 GitHub API：contents、secrets、workflow。"""

    def __init__(self):
        self.key = PrivateKey.generate()
        self.files = {}
        self.commits = []
        self.secrets = {}
        self.dispatches = []
        self.workflow_state = "active"
        self.sha = 0

    def put_file(self, path, data):
        self.sha += 1
        self.files[path] = (json.dumps(data, ensure_ascii=False, indent=2), str(self.sha))

    def config(self):
        return json.loads(self.files["watch_config.json"][0])

    def secret(self, name):
        return SealedBox(self.key).decrypt(base64.b64decode(self.secrets[name])).decode()

    def handle(self, route):
        request = route.request
        path = urlparse(request.url).path
        method = request.method
        cors = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*"}

        def reply(status=200, body=None):
            route.fulfill(status=status, headers={**cors, "Content-Type": "application/json"}, body=json.dumps(body if body is not None else {}))

        if method == "OPTIONS":
            return reply(204)
        prefix = "/repos/k0341055/parking-agent"
        if not path.startswith(prefix):
            return reply(404, {"message": "not found"})
        path = path[len(prefix):]
        if path.startswith("/contents/"):
            name = path[len("/contents/"):]
            if method == "GET":
                if name not in self.files:
                    return reply(404, {"message": "Not Found"})
                text, sha = self.files[name]
                return reply(200, {"content": base64.b64encode(text.encode()).decode(), "sha": sha})
            body = request.post_data_json
            if name in self.files and body.get("sha") != self.files[name][1]:
                return reply(409, {"message": "sha mismatch"})
            self.sha += 1
            self.files[name] = (base64.b64decode(body["content"]).decode(), str(self.sha))
            self.commits.append(body["message"] + "\n" + self.files[name][0] + json.dumps(body.get("committer")))
            return reply(200, {"content": {"sha": str(self.sha)}})
        if path == "/actions/secrets/public-key":
            return reply(200, {"key_id": "k1", "key": base64.b64encode(bytes(self.key.public_key)).decode()})
        if path == "/actions/secrets":
            return reply(200, {"secrets": [{"name": n} for n in self.secrets]})
        if path.startswith("/actions/secrets/"):
            name = path.rsplit("/", 1)[1]
            if method == "PUT":
                self.secrets[name] = request.post_data_json["encrypted_value"]
                return reply(201)
            self.secrets.pop(name, None)
            return reply(204)
        if path.endswith("/enable"):
            self.workflow_state = "active"
            return reply(204)
        if path.endswith("/dispatches"):
            self.dispatches.append(request.post_data_json)
            return reply(204)
        if path.endswith("/runs"):
            return reply(200, {"workflow_runs": [{"created_at": "2026-11-03T00:05:00Z", "status": "completed", "conclusion": "success", "html_url": "https://example.com/run"}]})
        if path.startswith("/actions/workflows/"):
            return reply(200, {"state": self.workflow_state})
        return reply(404, {"message": "not found"})


class UiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handler = functools.partial(QuietHandler, directory=str(ROOT / "docs"))
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.http.server_address[1]}/"
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.site = MockSite().start()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.http.shutdown()
        cls.site.stop()

    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.put_file("watch_config.json", {
            "settings": {"enabled": True, "run_minutes": 5, "interval_min": 15, "interval_max": 25, "retry_minutes": 5, "reminder_minutes": 60},
            "recipients": {"Recipient_1": {}},
            "bookings": [{"id": "Booking_1", "site": "p4", "date": future_day(18), "days": 7, "recipients": ["Recipient_1"], "auto_submit": True, "enabled": True, "rev": 1}],
        })
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900}, locale="zh-TW")
        self.addCleanup(self.context.close)
        self.context.route("https://api.github.com/**", self.gh.handle)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("dialog", lambda d: d.accept())
        self.addCleanup(lambda: self.assertEqual(self.errors, []))

    def open(self, token=True, fragment=""):
        if token:
            self.page.add_init_script("localStorage.setItem('pka-github-token', 'test-token')")
        self.page.goto(self.url + fragment)
        self.page.locator(".card").first.wait_for()

    def toast(self, text):
        self.page.locator("#toast", has_text=text).wait_for(timeout=5000)

    def saved(self, predicate):
        for _ in range(60):
            if predicate():
                return
            self.page.wait_for_timeout(100)
        self.fail("設定沒有存進 GitHub")

    def add_booking(self, day=None, days="7", name=NAME, plate=PLATE):
        self.page.fill("#fDate", day or future_day(25))
        self.page.fill("#fDays", days)
        self.page.fill("#fName", name)
        self.page.fill("#fPlate", plate)
        self.page.click("#fSave")

    def test_read_only_without_token(self):
        self.open(token=False)
        self.assertIn("readonly", self.page.locator("body").get_attribute("class"))
        self.assertFalse(self.page.locator("#bookingForm").is_visible())
        self.assertTrue(self.page.locator("#readonlyNote").is_visible())
        self.assertIn("7 天", self.page.locator("#card-Booking_1").inner_text())

    def test_add_recipient_stays_out_of_git(self):
        self.open()
        self.page.click("#addRcpt")
        self.page.fill("#rEmail", EMAIL)
        self.page.fill("#rLine", LINE_ID)
        self.page.click("#rSave")
        self.toast("已儲存 Recipient_2")
        self.saved(lambda: "Recipient_2" in self.gh.config()["recipients"])
        self.assertEqual(self.gh.secret("RECIPIENT_2"), f"{EMAIL},{LINE_ID}")
        self.assertEqual(self.gh.config()["recipients"]["Recipient_2"], {"email": True, "line": True})
        history = "".join(self.gh.commits)
        self.assertNotIn(EMAIL, history)
        self.assertNotIn(LINE_ID, history)
        self.assertNotIn(EMAIL, self.page.content())

    def test_recipient_validation(self):
        self.open()
        self.page.click("#addRcpt")
        self.page.click("#rSave")
        self.toast("至少填一個")
        self.page.fill("#rLine", "not-a-line-id")
        self.page.click("#rSave")
        self.toast("LINE User ID")
        self.assertEqual(self.gh.secrets, {})

    def test_add_booking_stays_out_of_git(self):
        self.open()
        day = future_day(25)
        self.add_booking(day=day, plate="ab-1234")
        self.saved(lambda: len(self.gh.config()["bookings"]) == 2)
        added = self.gh.config()["bookings"][1]
        self.assertEqual((added["id"], added["site"], added["date"], added["days"]), ("Booking_2", "p4", day, 7))
        self.assertEqual(added["recipients"], ["Recipient_1"])
        self.assertEqual(json.loads(self.gh.secret("BOOKING_2")), {"name": NAME, "plate": PLATE})
        history = "".join(self.gh.commits)
        self.assertNotIn(PLATE, history)
        self.assertNotIn(NAME, history)
        self.assertIn(PLATE, self.page.locator("#card-Booking_2").inner_text())

    def test_booking_validation_matches_site_rules(self):
        self.open()
        for kwargs, message in (
            (dict(plate="AB1234"), "車牌格式"),
            (dict(name=""), "請填姓名"),
            (dict(days="2"), "天數"),
            (dict(day=future_day(1)), "2 天後"),
        ):
            self.add_booking(**kwargs)
            self.toast(message)
        self.assertEqual(self.gh.secrets, {})
        self.assertEqual(len(self.gh.config()["bookings"]), 1)

    def test_edit_keeps_secret_when_left_blank_and_restarts_watch(self):
        self.gh.secrets["BOOKING_1"] = "sealed-before"
        self.open()
        self.page.click("#card-Booking_1 [data-act=edit]")
        self.page.fill("#fDays", "9")
        self.page.click("#fSave")
        self.toast("已更新 Booking_1")
        self.saved(lambda: self.gh.config()["bookings"][0]["days"] == 9)
        self.assertEqual(self.gh.secrets["BOOKING_1"], "sealed-before")
        self.assertEqual(self.gh.config()["bookings"][0]["rev"], 2)

    def test_delete_removes_secret_and_local_copy(self):
        self.open()
        self.add_booking()
        self.saved(lambda: len(self.gh.config()["bookings"]) == 2)
        self.page.click("#card-Booking_2 [data-act=delete]")
        self.saved(lambda: len(self.gh.config()["bookings"]) == 1)
        self.assertNotIn("BOOKING_2", self.gh.secrets)
        self.assertNotIn("Booking_2", self.page.evaluate("localStorage.getItem('pka-profiles')"))

    def test_status_is_shown_and_notification_link_opens_quick_panel(self):
        b = self.gh.config()["bookings"][0]
        self.gh.put_file("status.json", {"bookings": {"Booking_1": {
            "key": f"p4|{b['date']}|7|1", "state": "needs_manual", "detail": "網站要求人工驗證（reCAPTCHA）", "since": "2026-11-03T00:00:10+08:00",
        }}, "last_test": {"at": "2026-11-03T00:00:00+08:00", "ok": True, "detail": "已填單未送出"}})
        self.open(fragment="#quick=Booking_1")
        card = self.page.locator("#card-Booking_1")
        self.assertIn("請人工預約", card.inner_text())
        self.assertIn("reCAPTCHA", card.inner_text())
        self.assertTrue(card.locator(".quick").is_visible())
        self.assertEqual(self.page.locator("#sumManual").inner_text(), "1")
        self.assertIn("通過", self.page.locator("#testInfo").inner_text())
        href = card.locator(".quick a.primary").get_attribute("href")
        self.assertEqual(href, "https://pcc.youparking.com.tw/parkingreserve/#/reservedindex/1")

    def test_stale_status_is_ignored_after_change(self):
        self.gh.put_file("status.json", {"bookings": {"Booking_1": {"key": "p4|2020-01-01|7|1", "state": "booked", "detail": ""}}})
        self.open()
        self.assertIn("等待下次檢查", self.page.locator("#card-Booking_1").inner_text())
        self.assertEqual(self.page.locator("#sumBooked").inner_text(), "0")

    def test_action_buttons_and_disabled_workflow(self):
        self.gh.workflow_state = "disabled_manually"
        self.open()
        self.assertTrue(self.page.locator("#wfBanner").is_visible())
        self.page.click("#wfEnable")
        self.toast("已啟用")
        self.assertFalse(self.page.locator("#wfBanner").is_visible())
        self.page.click("#runDry")
        self.toast("測試填單已送出")
        self.assertEqual(self.gh.dispatches, [{"ref": "main", "inputs": {"dry_run": "1"}}])

    def test_settings_saved_and_clamped(self):
        self.open()
        self.page.fill("#setRun", "99")
        self.page.locator("#setRun").blur()
        self.saved(lambda: self.gh.config()["settings"]["run_minutes"] == 12)

    def test_bookmarklet_fills_the_real_site_form_and_stops_before_submit(self):
        day = future_day(18)
        self.site.left = {future_day(10): 0, day: 5}
        self.site.orders.clear()
        self.site.order_status = 200
        self.open()
        self.add_booking(day=day, days="5")   # Booking_2
        self.saved(lambda: len(self.gh.config()["bookings"]) == 2)
        self.page.click("#card-Booking_2 [data-act=quick]")
        href = self.page.locator("#card-Booking_2 [data-bm]").get_attribute("href")
        self.assertTrue(href.startswith("javascript:"))
        script = unquote(href[len("javascript:"):]).replace('"host":"pcc.youparking.com.tw"', '"host":"127.0.0.1"')

        site_page = self.context.new_page()
        site_page.goto(self.site.url + "/parkingreserve/#/")
        site_page.wait_for_load_state("networkidle")
        site_page.evaluate(script)
        submit = site_page.get_by_role("button", name="送出")
        submit.wait_for()
        site_page.wait_for_function("() => [...document.querySelectorAll('.v-dialog--active input')].some(i => i.value === 'AB-1234')")
        self.assertEqual(self.site.orders, [], "一鍵填單不應該自己送出")
        self.assertEqual(site_page.locator(".v-dialog--active .error--text").count(), 0)

        # 人按下送出後，網站收到的是填好的資料（代表 Vue 的表單狀態真的更新了）
        site_page.wait_for_timeout(300)
        submit.click()
        for _ in range(50):
            if self.site.orders:
                break
            site_page.wait_for_timeout(100)
        self.assertEqual(len(self.site.orders), 1)
        order = self.site.orders[0]
        self.assertEqual((order["Model_StartDate"], int(order["Model_ReservedDay"])), (day, 5))
        self.assertEqual((order["Model_Contact"], order["Model_CarShow"]), (NAME, PLATE))
        site_page.close()

    def test_phone_layout_has_no_horizontal_scroll(self):
        self.page.set_viewport_size({"width": 375, "height": 812})
        self.open()
        self.add_booking()
        self.saved(lambda: len(self.gh.config()["bookings"]) == 2)
        self.page.click("#card-Booking_2 [data-act=quick]")
        overflow = self.page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        self.assertLessEqual(overflow, 0)
        if SHOTS:
            self.page.screenshot(path=f"{SHOTS}/ui_phone.png", full_page=True)

    def test_screenshots(self):
        if not SHOTS:
            self.skipTest("沒有設定 SCREENSHOT_DIR")
        b = self.gh.config()["bookings"][0]
        self.gh.secrets["BOOKING_1"] = "sealed"
        self.gh.secrets["GMAIL_RECIPIENTS"] = "sealed"
        self.gh.secrets["LINE_USER_ID"] = "sealed"
        self.gh.put_file("status.json", {"bookings": {"Booking_1": {
            "key": f"p4|{b['date']}|7|1", "state": "needs_manual", "detail": "網站要求人工驗證（reCAPTCHA）", "since": "2026-11-03T00:00:10+08:00",
        }}})
        self.open()
        self.add_booking(day=future_day(25), days="5")
        self.saved(lambda: len(self.gh.config()["bookings"]) == 2)
        self.page.click("#card-Booking_2 [data-act=quick]")
        self.page.wait_for_timeout(400)
        self.page.screenshot(path=f"{SHOTS}/ui_desktop.png", full_page=True)
        self.page.emulate_media(color_scheme="dark")
        self.page.screenshot(path=f"{SHOTS}/ui_dark.png", full_page=True)


if __name__ == "__main__":
    unittest.main()
