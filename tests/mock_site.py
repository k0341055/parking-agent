"""
本機模擬預約網站：前端用網站真正的 JS（從官網下載一次後快取），API 由這裡回應。
不會把任何東西送到真實網站，可以放心測「送出」。

reCAPTCHA 只在這個模擬環境換成假的腳本：官方腳本在 localhost 上不能用，
真實網站的驗證不受影響（token 由伺服器檢查）。
"""

import json
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

UPSTREAM = "https://pcc.youparking.com.tw"
CACHE_DIR = Path(__file__).parent / ".site_cache"
RECAPTCHA_URL = b"https://www.google.com/recaptcha/api.js?render="
RECAPTCHA_STUB = b"window.grecaptcha = { execute: async () => 'test-token', ready: f => f() };"


def _static(path: str) -> bytes:
    """/parkingreserve/... 的靜態檔：第一次從官網下載（公開的 GET），之後用快取。"""
    name = path.strip("/").replace("/", "__") or "index"
    cached = CACHE_DIR / name
    if not cached.exists():
        CACHE_DIR.mkdir(exist_ok=True)
        resp = requests.get(UPSTREAM + path, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        cached.write_bytes(resp.content)
    return cached.read_bytes()


class MockSite:
    def __init__(self):
        self.left = {}                # {"2026-11-21": 0}
        self.pause = False
        self.order_status = 200       # CreateOrder 的回應碼
        self.order_body = ""
        self.record_on_success = True
        self.orders = []              # 收到的 CreateOrder 內容
        self.records = []             # GetOrderRecord 的回應
        self.server = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self):
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body, content_type="application/json; charset=utf-8"):
                if not isinstance(body, bytes):
                    body = (json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urlparse(self.path)
                path, query = parsed.path, parse_qs(parsed.query)
                if path == "/test-recaptcha.js":
                    return self._send(200, RECAPTCHA_STUB, "text/javascript")
                if path.startswith("/api/ParkingOnlyReserved/"):
                    return self._send(200, site.api(path.rsplit("/", 1)[1], query))
                if path.startswith("/parkingreserve"):
                    file_path = "/parkingreserve/" if path in ("/parkingreserve", "/parkingreserve/") else path
                    try:
                        body = _static(file_path)
                    except requests.RequestException:
                        return self._send(404, b"not found", "text/plain")
                    if file_path.endswith(".js"):
                        body = body.replace(RECAPTCHA_URL, b"/test-recaptcha.js?render=")
                    types = {".js": "text/javascript", ".css": "text/css", ".png": "image/png", ".jpg": "image/jpeg"}
                    content_type = types.get(Path(file_path).suffix, "text/html; charset=utf-8")
                    return self._send(200, body, content_type)
                return self._send(404, b"not found", "text/plain")

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
                if self.path.endswith("/CreateOrder"):
                    site.orders.append(data)
                    if site.order_status == 200 and site.record_on_success:
                        site.records.append({
                            "Model_ReservedOrderID": len(site.orders),
                            "Model_SpaceName": "P4停車場",
                            "Model_StartDate": data.get("Model_StartDate", "") + "T00:00:00",
                            "Model_EndDate": data.get("Model_EndDate", "") + "T00:00:00",
                            "Model_InsertDate": "2026-11-03T00:00:00",
                        })
                    return self._send(site.order_status, site.order_body or {})
                return self._send(404, b"not found", "text/plain")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()

    def api(self, name, query):
        reserved = {
            "ParkingReserved_ID": 1, "ParkingReserved_SiteInfoCode": 84416, "ParkingReserved_CustomName": "桃園機場第二航廈P4停車場",
            "ParkingReserved_Content": "<div><p>測試須知</p></div>", "ParkingReserved_MinReserveDays": 3,
            "ParkingReserved_MaxReserveDays": 60, "ParkingReserved_Tel": "03-3932415",
        }
        if name == "GetParkingReserved":
            return [reserved]
        if name == "GetReservedSpace":
            return {
                "ParkingReserved": reserved, "Pause": self.pause,
                "List_ParkingReservedLeft": [{
                    "ParkingReservedSpace_ID": int(query.get("LinkID", ["1"])[0]) if query.get("LinkID") != ["4"] else 9,
                    "ParkingReservedSpace_Name": "P4停車場", "ParkingReservedSpace_Tel": "03-3932415",
                    "ParkingReservedSpace_Address": "測試地址",
                }],
            }
        if name == "GetReservedSpaceLeft":
            if self.pause:
                return {"Pause": True}
            return {
                "ParkingReserved": reserved,
                "ParkingReservedSpace": {"ParkingReservedSpace_Name": "P4停車場"},
                "List_ParkingReservedLeft": [
                    {"ID": i, "Model_Date": f"{day}T00:00:00Z", "Model_Left": left}
                    for i, (day, left) in enumerate(sorted(self.left.items()))
                ],
                "ListParkingReservedSpacePart": [{
                    "ParkingReservedSpacePart_Date": "2026-01-01T00:00:00", "ParkingReservedSpacePart_WorkDayAmount": 300,
                    "ParkingReservedSpacePart_WeekendAmount": 300, "ParkingReservedSpacePart_HolidayAmount": 400,
                }],
                "List_ParkingReservedCalendar": [], "List_Calendar": [],
            }
        if name == "GetOrderRecord":
            return self.records
        return {}


def future_day(offset: int) -> str:
    return (date.today() + timedelta(days=offset)).isoformat()
