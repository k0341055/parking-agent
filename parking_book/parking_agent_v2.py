"""
停車場預約 Agent v2
監看 https://pcc.youparking.com.tw/parkingreserve/ ，預約清單與收件人來自 UI 網頁（docs/index.html）：

  1. 用網站的查詢 API 輪詢名額（不開瀏覽器，幾百毫秒一次）
  2. 目標日期有名額：同時發「有名額」通知並開瀏覽器自動填單送出
  3. 發「預約成功／請人工預約」通知，通知內附車牌、姓名、天數和預約連結，沒搶到也能馬上手動完成

網站要求 reCAPTCHA 人工驗證（HTTP 428）時程式不處理驗證，直接通知人工預約。

設定：watch_config.json（UI 寫入，公開，不含個資）
狀態：status.json（這支程式寫入，公開，不含個資）
個資：GitHub Secrets BOOKING_N（{"name", "plate"}）、RECIPIENT_N（信箱、LINE User ID）

安裝：
    pip install -r requirements.txt
    playwright install chromium

執行：
    python parking_book/parking_agent_v2.py              # 監看 run_minutes 分鐘
    DRY_RUN=1 python parking_book/parking_agent_v2.py    # 測試填單，不送出
    SEND_TEST=1 python parking_book/parking_agent_v2.py  # 寄測試通知
"""

import asyncio
import base64
import json
import logging
import os
import random
import re
import smtplib
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─────────────────────────────────────────────
# 設定區
# ─────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", ROOT / "watch_config.json"))
STATUS_FILE = Path(os.environ.get("STATUS_FILE", ROOT / "status.json"))

BASE_URL = os.environ.get("PARKING_BASE_URL", "https://pcc.youparking.com.tw").rstrip("/")
API_URL = f"{BASE_URL}/api/ParkingOnlyReserved"
HEADLESS = os.environ.get("HEADLESS", "1") != "0"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
GITHUB_BRANCH = os.environ.get("GITHUB_REF_NAME", "main")
UI_URL = "https://{}.github.io/{}/".format(*(GITHUB_REPOSITORY or "k0341055/parking-agent").split("/", 1))

TZ = ZoneInfo("Asia/Taipei")

# 網站的停車場（與 docs/index.html 的 SITES 一致）
SITES = {
    "p4": {"name": "P4 停車場（第二航廈）", "link": 1, "space": 1, "site_code": 84416},
    "riyueting": {"name": "日月亭平安（機場外圍）", "link": 4, "space": 9, "site_code": 15272193788},
}

# 網站規則：進場前 2～20 天可登記、每天 00:00 開放新日期、停放 3～60 天（API 的 ParkingReserved_* 欄位）
EARLIEST_DAYS = 2
LATEST_DAYS = 20
MIN_DAYS = 3
MAX_DAYS = 60
PLATE_RE = re.compile(r"(軍[A-Z]-[0-9]{5})|([A-Z0-9]{1,4}-[A-Z0-9]{1,4})")

# 與 docs/index.html 的 DEFAULT_SETTINGS / RANGES 一致
DEFAULT_SETTINGS = {
    "enabled": True,
    "run_minutes": 5,
    "interval_min": 15,
    "interval_max": 25,
    "retry_minutes": 5,
    "reminder_minutes": 60,
}
_SETTING_RANGES = {
    "run_minutes": (1, 12),
    "interval_min": (5, 120),
    "interval_max": (5, 120),
    "retry_minutes": (1, 60),
    "reminder_minutes": (5, 1440),
}
MAX_ALIASES = 10                 # workflow 逐一傳入 Secret BOOKING_1..10、RECIPIENT_1..10
CONFIG_RELOAD_SECONDS = 60       # 執行中多久重新讀一次 UI 的設定
BURST_INTERVAL_SECONDS = 3       # 午夜開放時的輪詢間隔
HEALTH_FAIL_ROUNDS = 8           # API 連續失敗幾輪才通知
HEALTH_ALERT_HOURS = 6           # 異常通知的冷卻時間

_BOOKING_RE = re.compile(r"Booking_([1-9]\d*)")
_RECIPIENT_RE = re.compile(r"Recipient_([1-9]\d*)")
_LINE_ID_RE = re.compile(r"U[0-9a-f]{32}")
_ALREADY_RE = re.compile(r"已於.*?登記", re.DOTALL)
_RE_GO = re.compile(r"前往(登記|預約)")
_RE_ROW_BTN = re.compile(r"登記|預約")

# 通知憑證（從環境變數讀取，不寫在程式碼裡）
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GMAIL_SENDER = os.environ.get("GMAIL_SENDER", "")
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")

http = requests.Session()
http.headers["User-Agent"] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# ─────────────────────────────────────────────
# Logger
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)


def now_tw() -> datetime:
    return datetime.now(TZ)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse_ts(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value).astimezone(TZ)
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────
# 設定檔與 Secrets
# ─────────────────────────────────────────────

@dataclass
class Booking:
    alias: str
    site: str
    date: date
    days: int
    recipients: list = field(default_factory=list)
    auto_submit: bool = True
    enabled: bool = True
    rev: int = 0
    name: str = ""
    plate: str = ""

    @property
    def n(self) -> int:
        return int(_BOOKING_RE.fullmatch(self.alias).group(1))

    @property
    def key(self) -> str:
        """這些值變了就視為新的預約，舊狀態作廢。"""
        return f"{self.site}|{self.date.isoformat()}|{self.days}|{self.rev}"

    @property
    def end_date(self) -> date:
        return self.date + timedelta(days=self.days - 1)

    @property
    def open_date(self) -> date:
        return self.date - timedelta(days=LATEST_DAYS)

    @property
    def has_profile(self) -> bool:
        return bool(self.name and self.plate)

    @property
    def index_url(self) -> str:
        return f"{BASE_URL}/parkingreserve/#/reservedindex/{SITES[self.site]['link']}"

    @property
    def records_url(self) -> str:
        return f"{BASE_URL}/parkingreserve/#/orderrecord/{SITES[self.site]['link']}"


@dataclass
class Contact:
    emails: list = field(default_factory=list)
    line_ids: list = field(default_factory=list)


def parse_settings(data: dict) -> dict:
    raw = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    settings = dict(DEFAULT_SETTINGS)
    if isinstance(raw.get("enabled"), bool):
        settings["enabled"] = raw["enabled"]
    for key, (low, high) in _SETTING_RANGES.items():
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            settings[key] = max(low, min(high, value))
    settings["interval_max"] = max(settings["interval_min"], settings["interval_max"])
    return settings


def load_profile(n: int) -> tuple[str, str]:
    """BOOKING_N → (姓名, 車牌)。沒設定或格式不對回傳空字串。"""
    raw = os.environ.get(f"BOOKING_{n}", "").strip()
    if not raw:
        return "", ""
    try:
        data = json.loads(raw)
        name = str(data.get("name", "")).strip()
        plate = str(data.get("plate", "")).strip().upper()
    except (json.JSONDecodeError, AttributeError):
        log.warning(f"Secret BOOKING_{n} 不是有效的 JSON，請在 UI 重新儲存")
        return "", ""
    if not name or not PLATE_RE.fullmatch(plate):
        log.warning(f"Secret BOOKING_{n} 的姓名或車牌格式不對，請在 UI 重新儲存")
        return "", ""
    return name, plate


def parse_bookings(data: dict) -> list:
    bookings = []
    seen = set()
    for raw in data.get("bookings") or []:
        if not isinstance(raw, dict):
            continue
        alias = raw.get("id")
        site = raw.get("site")
        if not isinstance(alias, str) or not _BOOKING_RE.fullmatch(alias) or alias in seen:
            log.warning(f"略過無效的預約代稱：{alias!r}")
            continue
        if site not in SITES:
            log.warning(f"{alias} 的停車場不存在：{site!r}")
            continue
        try:
            day = date.fromisoformat(raw.get("date", ""))
            days = int(raw.get("days"))
        except (TypeError, ValueError):
            log.warning(f"{alias} 的日期或天數格式不對")
            continue
        if not MIN_DAYS <= days <= MAX_DAYS:
            log.warning(f"{alias} 的天數超出 {MIN_DAYS}～{MAX_DAYS}")
            continue
        seen.add(alias)
        recipients = [a for a in raw.get("recipients") or [] if isinstance(a, str) and _RECIPIENT_RE.fullmatch(a)]
        booking = Booking(
            alias=alias, site=site, date=day, days=days, recipients=recipients,
            auto_submit=raw.get("auto_submit") is not False,
            enabled=raw.get("enabled") is not False,
            rev=raw.get("rev") if isinstance(raw.get("rev"), int) else 0,
        )
        booking.name, booking.plate = load_profile(booking.n)
        bookings.append(booking)
    return bookings


def load_contact(n: int) -> Contact:
    """Recipient_N → Secret RECIPIENT_N（信箱、LINE User ID 逗號分隔）；沒有則用舊設定 GMAIL_RECIPIENTS 第 N 個、LINE_USER_ID（僅 N=1）。"""
    raw = os.environ.get(f"RECIPIENT_{n}", "").strip()
    if raw:
        contact = Contact()
        for token in (t.strip() for t in raw.split(",")):
            if "@" in token:
                contact.emails.append(token)
            elif _LINE_ID_RE.fullmatch(token):
                contact.line_ids.append(token)
        return contact
    legacy = [a.strip() for a in os.environ.get("GMAIL_RECIPIENTS", "").split(",") if a.strip()]
    contact = Contact(emails=legacy[n - 1:n])
    line_id = os.environ.get("LINE_USER_ID", "").strip()
    if n == 1 and line_id:
        contact.line_ids.append(line_id)
    return contact


def known_recipients() -> list:
    """所有已設定聯絡方式的 Recipient_N。"""
    aliases = []
    for n in range(1, MAX_ALIASES + 1):
        contact = load_contact(n)
        if contact.emails or contact.line_ids:
            aliases.append(f"Recipient_{n}")
    return aliases


def _mask_secrets_in_actions_log():
    """公開 repo 的 Actions log 任何人都看得到；JSON 內的個別欄位 GitHub 不會自動遮蔽，這裡逐一註冊。"""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    values = set()
    for n in range(1, MAX_ALIASES + 1):
        values.update(load_profile(n))
        contact = load_contact(n)
        values.update(contact.emails + contact.line_ids)
    for value in values:
        if value:
            print(f"::add-mask::{value}", flush=True)


def load_config() -> tuple[dict, list]:
    data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return parse_settings(data), parse_bookings(data)


def reload_remote_config() -> tuple[dict, list] | None:
    """執行中重新讀取 UI 改過的設定；失敗就沿用目前的設定。"""
    if not (GITHUB_TOKEN and GITHUB_REPOSITORY):
        return None
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/{CONFIG_FILE.name}",
            params={"ref": GITHUB_BRANCH},
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.raw+json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        log.warning(f"重新讀取設定失敗，沿用目前設定：{type(e).__name__}")
        return None
    return parse_settings(data), parse_bookings(data)


# ─────────────────────────────────────────────
# 狀態（status.json）
# ─────────────────────────────────────────────

_status_sha = None


def load_status() -> dict:
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data.get("bookings"), dict):
        data["bookings"] = {}
    return data


def _status_entry(status: dict, booking: Booking) -> dict:
    entry = status["bookings"].get(booking.alias)
    if not isinstance(entry, dict) or entry.get("key") != booking.key:
        entry = {"key": booking.key, "state": "new", "detail": "", "attempts": 0}
        status["bookings"][booking.alias] = entry
    return entry


def set_state(status: dict, booking: Booking, state: str, detail: str = ""):
    entry = _status_entry(status, booking)
    if entry.get("state") == state and entry.get("detail", "") == detail:
        return
    log.info(f"{booking.alias}：{entry.get('state')} → {state} {detail}")
    entry.update(state=state, detail=detail, since=_iso(now_tw()))
    status["dirty"] = True


def save_status(status: dict, active_aliases=None):
    """只有狀態改變才寫入，避免每輪都產生 commit。"""
    if not status.pop("dirty", False):
        return
    global _status_sha
    if active_aliases is not None:
        status["bookings"] = {a: e for a, e in status["bookings"].items() if a in active_aliases}
    status["updated"] = _iso(now_tw())
    text = json.dumps(status, ensure_ascii=False, indent=2) + "\n"
    STATUS_FILE.write_text(text, encoding="utf-8")
    if not (GITHUB_TOKEN and GITHUB_REPOSITORY):
        return
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/{STATUS_FILE.name}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    for _ in range(3):
        try:
            if _status_sha is None:
                resp = requests.get(url, params={"ref": GITHUB_BRANCH}, headers=headers, timeout=10)
                _status_sha = resp.json().get("sha") if resp.ok else None
            body = {
                "message": "status: 更新預約狀態",
                "content": base64.b64encode(text.encode()).decode(),
                "branch": GITHUB_BRANCH,
            }
            if _status_sha:
                body["sha"] = _status_sha
            resp = requests.put(url, headers=headers, json=body, timeout=15)
            if resp.ok:
                _status_sha = resp.json()["content"]["sha"]
                return
            if resp.status_code in (409, 422):
                _status_sha = None
                continue
            log.warning(f"寫入 status.json 失敗：HTTP {resp.status_code}")
            return
        except (requests.RequestException, ValueError) as e:
            log.warning(f"寫入 status.json 例外：{type(e).__name__}")
            return


# ─────────────────────────────────────────────
# 網站查詢 API
# ─────────────────────────────────────────────

def fetch_left(site: str) -> dict | None:
    """{日期: 剩餘名額}；網站暫停登記（系統整理中）回傳 None。格式不對會丟例外，算進異常次數。"""
    resp = http.get(f"{API_URL}/GetReservedSpaceLeft", params={"SpaceID": SITES[site]["space"]}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("Pause"):
        return None
    return {row["Model_Date"][:10]: int(row["Model_Left"]) for row in data["List_ParkingReservedLeft"]}


def find_record(booking: Booking) -> dict | None:
    """查詢這台車在目標日期的登記記錄。使用者手動預約成功時也查得到。"""
    resp = http.get(
        f"{API_URL}/GetOrderRecord",
        params={"Model_CarNo": booking.plate, "Model_SiteCode": SITES[booking.site]["site_code"]},
        timeout=10,
    )
    resp.raise_for_status()
    for row in resp.json() or []:
        if (row.get("Model_StartDate") or "")[:10] == booking.date.isoformat():
            return row
    return None


async def find_record_safe(booking: Booking, tries: int = 1) -> dict | None:
    for attempt in range(tries):
        if attempt:
            await asyncio.sleep(2)
        try:
            record = await asyncio.to_thread(find_record, booking)
        except (requests.RequestException, ValueError, KeyError, AttributeError, TypeError) as e:
            log.warning(f"{booking.alias} 查詢記錄失敗：{type(e).__name__}")
            record = None
        if record:
            return record
    return None


# ─────────────────────────────────────────────
# 通知
# ─────────────────────────────────────────────

def _push_line(line_id: str, messages: list) -> bool:
    resp = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
        data=json.dumps({"to": line_id, "messages": messages}, ensure_ascii=False).encode("utf-8"),
        timeout=10,
    )
    if resp.status_code != 200:
        log.error(f"LINE 通知失敗：HTTP {resp.status_code}")
    return resp.status_code == 200


def notify_line(line_id: str, text: str, booking: Booking | None = None) -> bool:
    """附 booking 時多送一張按鈕卡片（開啟預約頁、一鍵複製車牌／姓名／天數）；舊版 LINE 不支援時退回純文字。"""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        log.warning("沒有 LINE_CHANNEL_ACCESS_TOKEN，略過 LINE 通知")
        return False
    messages = [{"type": "text", "text": text[:4900]}]
    try:
        if booking and booking.has_profile:
            card = {
                "type": "template",
                "altText": "快速預約：開啟預約頁、複製車牌與姓名",
                "template": {
                    "type": "buttons",
                    "text": f"{booking.date.isoformat()} 起 {booking.days} 天\n{booking.plate}｜{booking.name}"[:160],
                    "actions": [
                        {"type": "uri", "label": "開啟預約頁", "uri": booking.index_url},
                        {"type": "clipboard", "label": "複製車牌", "clipboardText": booking.plate},
                        {"type": "clipboard", "label": "複製姓名", "clipboardText": booking.name},
                        {"type": "clipboard", "label": "複製天數", "clipboardText": str(booking.days)},
                    ],
                },
            }
            if _push_line(line_id, messages + [card]):
                return True
        return _push_line(line_id, messages)
    except requests.RequestException as e:
        log.error(f"LINE 通知例外：{type(e).__name__}")
        return False


def notify_gmail(address: str, subject: str, body: str) -> bool:
    if not (GMAIL_SENDER and GMAIL_PASSWORD):
        log.warning("沒有 GMAIL_SENDER / GMAIL_PASSWORD，略過 Gmail 通知")
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = GMAIL_SENDER
        msg["To"] = address
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(GMAIL_SENDER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_SENDER, [address], msg.as_string())
        return True
    except (smtplib.SMTPException, OSError) as e:
        log.error(f"Gmail 通知例外：{type(e).__name__}")
        return False


def _send_to_recipient(alias: str, subject: str, body: str, booking: Booking | None) -> bool:
    contact = load_contact(int(_RECIPIENT_RE.fullmatch(alias).group(1)))
    results = []
    for address in contact.emails:
        results.append(notify_gmail(address, subject, body))
    for line_id in contact.line_ids:
        results.append(notify_line(line_id, f"{subject}\n\n{body}", booking))
    if not results:
        log.warning(f"{alias} 沒有設定信箱或 LINE，收不到通知")
    else:
        log.info(f"{alias} 通知：成功 {sum(results)}／{len(results)}")
    return any(results)


async def send_all(aliases: list, subject: str, body: str, booking: Booking | None = None) -> bool:
    """同時通知所有收件人；通知不能拖慢搶位，一律丟到背景執行緒。"""
    results = await asyncio.gather(*(
        asyncio.to_thread(_send_to_recipient, alias, subject, body, booking) for alias in aliases
    ))
    return any(results)


def _kit(booking: Booking) -> str:
    """人工預約需要的所有資料，放在每一則「有名額／失敗」通知裡。"""
    lines = [
        f"停車場：{SITES[booking.site]['name']}",
        f"進場日：{booking.date.isoformat()}",
        f"天數：{booking.days}（至 {booking.end_date.isoformat()}）",
    ]
    if booking.has_profile:
        lines += [f"車牌：{booking.plate}", f"姓名：{booking.name}"]
    lines += [f"手動預約：{booking.index_url}", f"複製與一鍵填單：{UI_URL}#quick={booking.alias}"]
    return "\n".join(lines)


def _short(booking: Booking) -> str:
    return f"{booking.date.month}/{booking.date.day}"


def msg_available(booking: Booking, left: int, attempting: bool, will_retry: bool = False) -> tuple[str, str]:
    if attempting:
        action = "正在自動填單送出，結果稍後通知。"
    elif will_retry:
        action = "上次自動預約沒成功，稍後會再試，也可以現在手動預約。"
    else:
        action = "這筆沒有自動送出，請手動預約。"
    return f"🚗 {_short(booking)} 有名額了（剩 {left} 位）", f"{action}\n\n{_kit(booking)}"


def msg_booked(booking: Booking, verified: bool, already: bool = False) -> tuple[str, str]:
    lines = [
        f"停車場：{SITES[booking.site]['name']}",
        f"使用區間：{booking.date.isoformat()} ～ {booking.end_date.isoformat()}",
    ]
    if already:
        lines.insert(0, "查詢記錄時發現這台車已有這天的登記，停止監看。")
    elif verified:
        lines.insert(0, "已送出，查詢記錄也確認有這筆登記。")
    else:
        lines.insert(0, f"網站回應登記成功，但查詢記錄還沒看到，請確認：{booking.records_url}")
    lines.append(f"完成時間：{now_tw().strftime('%Y-%m-%d %H:%M:%S')}")
    return f"✅ {_short(booking)} 停車預約成功", "\n".join(lines)


def msg_manual(booking: Booking, reason: str) -> tuple[str, str]:
    return f"⚠️ {_short(booking)} 自動預約沒成功，請人工預約", f"原因：{reason}\n\n{_kit(booking)}"


def msg_gone(booking: Booking) -> tuple[str, str]:
    return f"😢 {_short(booking)} 名額被搶走了", f"送出前名額已滿，繼續監看，有名額會再通知。\n\n{_kit(booking)}"


def msg_expired(booking: Booking) -> tuple[str, str]:
    return f"⌛ {_short(booking)} 已過登記期限", f"網站只能登記 {EARLIEST_DAYS} 天後的日期，這筆已停止監看。\n\n{_kit(booking)}"


# ─────────────────────────────────────────────
# 自動填單（瀏覽器）
# ─────────────────────────────────────────────

@dataclass
class Outcome:
    kind: str   # ok / captcha / rejected / full / failed / unknown / dry_ok
    detail: str = ""


def _scrub(text: str, booking: Booking) -> str:
    """網站訊息可能包含車牌或姓名；寫進 log 或 status.json 前要拿掉（兩者都公開）。"""
    for value, mask in ((booking.plate, "[車牌]"), (booking.name, "[姓名]")):
        if value:
            text = text.replace(value, mask)
    return text


async def attempt_booking(booking: Booking, *, dry_run: bool = False, on_date: str | None = None) -> Outcome:
    """開瀏覽器走完網站流程並送出。dry_run 只填到表單，不按送出。"""
    target = on_date or booking.date.isoformat()
    step = "啟動瀏覽器"
    submitted = False
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeout, async_playwright
    except ImportError:
        return Outcome("failed", "沒有安裝 playwright")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS)
            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                        f"Chrome/{browser.version} Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 860},
                    locale="zh-TW",
                    timezone_id="Asia/Taipei",
                )
                page = await context.new_page()
                saw_request = asyncio.Event()
                responses: asyncio.Queue = asyncio.Queue()
                page.on("request", lambda r: saw_request.set() if "CreateOrder" in r.url else None)
                page.on("response", lambda r: responses.put_nowait(r) if "CreateOrder" in r.url else None)

                # ── 須知頁：可直接進入，日期列表則一定要從這裡點過去 ──
                step = "開啟須知頁"
                await page.goto(booking.index_url, wait_until="domcontentloaded", timeout=30_000)
                go = page.get_by_role("button", name=_RE_GO).first
                await go.wait_for(timeout=20_000)
                if await page.get_by_text("系統登記整理中").is_visible():
                    return Outcome("failed", "網站系統整理中")

                step = "勾選同意"
                checkbox = page.locator("input[role=checkbox]").first
                if await checkbox.get_attribute("aria-checked") != "true":
                    await page.locator(".v-input--selection-controls__ripple").first.click()

                step = "進入日期列表"
                await go.click()
                row = page.locator(f"tbody tr:has(td:has-text('{target}'))").first
                await row.wait_for(timeout=15_000)
                book_btn = row.get_by_role("button", name=_RE_ROW_BTN)
                if await book_btn.count() == 0:
                    return Outcome("full")

                step = "開啟表單"
                await book_btn.click()

                step = "填寫表單"
                days_field = page.get_by_role("textbox", name="停放天數")
                await days_field.wait_for(timeout=10_000)
                await days_field.fill(str(booking.days))
                await page.get_by_role("textbox", name="姓名").fill(booking.name)
                await page.get_by_role("textbox", name=re.compile("車牌號碼")).fill(booking.plate)
                await page.wait_for_timeout(200)   # 等表單驗證狀態更新
                errors = await page.locator(".v-dialog--active .error--text .v-messages__message").all_inner_texts()
                if errors:
                    return Outcome("failed", "表單驗證失敗：" + "、".join(e.strip() for e in errors))
                if dry_run:
                    period = await page.locator(".v-dialog--active").inner_text()
                    found = re.search(r"\d{4}/\d{2}/\d{2} - \d{4}/\d{2}/\d{2}", period)
                    return Outcome("dry_ok", f"已填單未送出，使用區間 {found.group(0) if found else '?'}")

                step = "等待 reCAPTCHA 載入"
                await page.wait_for_function("() => window.grecaptcha && window.grecaptcha.execute", timeout=15_000)

                step = "送出"
                submit = page.get_by_role("button", name="送出")
                await submit.click()
                submitted = True
                log.info(f"{booking.alias} 已按送出")
                try:
                    await asyncio.wait_for(saw_request.wait(), timeout=8)
                except asyncio.TimeoutError:
                    # 驗證狀態還沒更新時第一次點擊不會發出請求（沒有請求＝不會重複登記）
                    log.info(f"{booking.alias} 沒有發出請求，再按一次送出")
                    await submit.click()
                    try:
                        await asyncio.wait_for(saw_request.wait(), timeout=8)
                    except asyncio.TimeoutError:
                        return Outcome("failed", "按下送出後網站沒有發出請求")

                try:
                    resp = await asyncio.wait_for(responses.get(), timeout=20)
                except asyncio.TimeoutError:
                    return Outcome("unknown", "送出後網站沒有回應")
                body = _scrub((await resp.text())[:200], booking)
                log.info(f"{booking.alias} 送出結果：HTTP {resp.status} {body}")
                if resp.status == 200:
                    return Outcome("ok")
                if resp.status == 428:
                    return Outcome("captcha", "網站要求人工驗證（reCAPTCHA）")
                return Outcome("rejected", f"網站回應 HTTP {resp.status}：{body}")
            finally:
                await browser.close()
    except PlaywrightTimeout:
        if submitted:
            return Outcome("unknown", "送出後頁面逾時")
        return Outcome("failed", f"「{step}」逾時，網站可能改版或太慢")
    except Exception as e:
        log.error(f"{booking.alias}「{step}」例外：{type(e).__name__}")
        if submitted:
            return Outcome("unknown", f"送出後發生錯誤（{type(e).__name__}）")
        return Outcome("failed", f"「{step}」發生錯誤（{type(e).__name__}）")


# ─────────────────────────────────────────────
# 監看
# ─────────────────────────────────────────────

def active_bookings(bookings: list, status: dict) -> list:
    """還需要監看的預約：有開啟、沒分給任何人也照常處理（結果看 UI），已成功或過期的不再處理。"""
    active = []
    for booking in bookings:
        if not booking.enabled:
            continue
        if _status_entry(status, booking).get("state") in ("booked", "expired"):
            continue
        active.append(booking)
    return active


async def _handle_available(booking: Booking, left: int, status: dict, settings: dict):
    entry = _status_entry(status, booking)
    now = now_tw()

    # 已有記錄（包含收到通知後手動預約成功）就結案，不再送出
    if booking.has_profile and await find_record_safe(booking):
        set_state(status, booking, "booked", "查詢記錄確認已有登記")
        await send_all(booking.recipients, *msg_booked(booking, verified=True, already=True), booking)
        return

    last_alert = _parse_ts(entry.get("last_alert"))
    last_attempt = _parse_ts(entry.get("last_attempt"))
    new_episode = entry.get("state") not in ("available", "needs_manual")
    alert_due = new_episode or not last_alert or now - last_alert >= timedelta(minutes=settings["reminder_minutes"])
    auto = booking.auto_submit and booking.has_profile
    attempt_due = auto and (
        new_episode or not last_attempt or now - last_attempt >= timedelta(minutes=settings["retry_minutes"])
    )
    if not alert_due and not attempt_due:
        return

    log.info(f"{booking.alias}：{booking.date} 有名額（剩 {left} 位）")
    last_reason = entry.get("detail", "") if entry.get("state") == "needs_manual" else ""
    set_state(status, booking, "available", f"剩 {left} 位")
    alert = None
    if alert_due:
        message = msg_available(booking, left, attempt_due, will_retry=auto)
        alert = asyncio.create_task(send_all(booking.recipients, *message, booking))

    outcome = None
    if attempt_due:
        entry["last_attempt"] = _iso(now)
        entry["attempts"] = entry.get("attempts", 0) + 1
        status["dirty"] = True
        outcome = await attempt_booking(booking)
        log.info(f"{booking.alias} 自動預約結果：{outcome.kind} {outcome.detail}")

    if alert and await alert:
        entry["last_alert"] = _iso(now)
        status["dirty"] = True

    if outcome is None:
        if auto:
            reason = last_reason   # 還沒到重試時間，上次失敗的原因仍然有效
        elif booking.has_profile:
            reason = "這筆關閉了自動送出"
        else:
            reason = f"找不到預約資料（Secret BOOKING_{booking.n}），請在 UI 重新儲存"
        set_state(status, booking, "needs_manual", reason)
        return

    if outcome.kind == "ok":
        record = await find_record_safe(booking, tries=3)
        set_state(status, booking, "booked", "記錄已確認" if record else "網站回應成功，記錄尚未查到")
        await send_all(booking.recipients, *msg_booked(booking, verified=bool(record)), booking)
        return

    if outcome.kind == "full":
        set_state(status, booking, "full", "名額被搶走，繼續監看")
        await send_all(booking.recipients, *msg_gone(booking), booking)
        return

    # 被拒絕說「已登記」或送出後狀態不明：查記錄才知道是不是其實成功了
    already = outcome.kind == "rejected" and _ALREADY_RE.search(outcome.detail)
    if (already or outcome.kind == "unknown") and await find_record_safe(booking, tries=3):
        set_state(status, booking, "booked", "查詢記錄確認已有登記")
        await send_all(booking.recipients, *msg_booked(booking, verified=True, already=bool(already)), booking)
        return

    reason = outcome.detail
    if already:
        # 網站規定同一台車要先取消舊登記才能重新申請；哪一筆要留由人決定，程式不取消
        reason = f"這台車已有其他登記（{outcome.detail}），網站規定要先取消舊登記才能重新申請：{booking.records_url}"
    set_state(status, booking, "needs_manual", reason)
    if alert_due:
        await send_all(booking.recipients, *msg_manual(booking, reason), booking)


async def check_booking(booking: Booking, lefts: dict | None, status: dict, settings: dict):
    today = now_tw().date()
    if booking.date < today + timedelta(days=EARLIEST_DAYS):
        set_state(status, booking, "expired", "已過登記期限")
        await send_all(booking.recipients, *msg_expired(booking), booking)
        return
    if lefts is None:
        return  # 網站暫停登記
    left = lefts.get(booking.date.isoformat())
    if left is None:
        set_state(status, booking, "waiting", f"{booking.open_date.isoformat()} 00:00 開放登記")
    elif left <= 0:
        # 剛才還有名額：可能是收到通知後手動搶到最後一位，查一次記錄才知道
        was_open = _status_entry(status, booking).get("state") in ("available", "needs_manual")
        if was_open and booking.has_profile and await find_record_safe(booking):
            set_state(status, booking, "booked", "查詢記錄確認已有登記")
            await send_all(booking.recipients, *msg_booked(booking, verified=True, already=True), booking)
        else:
            set_state(status, booking, "full")
    else:
        await _handle_available(booking, left, status, settings)


def _seat_summary(booking: Booking, results: dict) -> str:
    """每輪的 log：看得出哪一輪開始有名額。repo 是公開的，這裡不放姓名和車牌。"""
    where = f"{booking.alias} {SITES[booking.site]['name']} {booking.date}"
    if booking.site not in results:
        return f"{where}：查詢失敗"
    lefts = results[booking.site]
    if lefts is None:
        return f"{where}：網站暫停登記"
    left = lefts.get(booking.date.isoformat())
    if left is None:
        return f"{where}：不在開放日期內"
    return f"{where}：有名額，剩 {left} 位" if left > 0 else f"{where}：已滿"


async def check_round(bookings: list, status: dict, settings: dict, health: dict, round_num: int = 0):
    """檢查一輪；同一個停車場只查一次 API。"""
    sites = {b.site for b in bookings}
    results = {}
    for site in sites:
        try:
            results[site] = await asyncio.to_thread(fetch_left, site)
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            log.warning(f"{site} 查詢名額失敗：{type(e).__name__}")
    if len(results) < len(sites):
        health["fails"] = health.get("fails", 0) + 1
    else:
        health["fails"] = 0
    if health["fails"] >= HEALTH_FAIL_ROUNDS:
        await _alert_health(bookings, status)

    for booking in bookings:
        log.info(f"第 {round_num} 輪｜{_seat_summary(booking, results)}")
        if booking.site in results:
            await check_booking(booking, results[booking.site], status, settings)


async def _alert_health(bookings: list, status: dict):
    last = _parse_ts(status.get("health_alert_at"))
    if last and now_tw() - last < timedelta(hours=HEALTH_ALERT_HOURS):
        return
    aliases = sorted({a for b in bookings for a in b.recipients})
    subject = "⚠️ 停車監控連續查詢失敗"
    body = (
        f"連續 {HEALTH_FAIL_ROUNDS} 輪查不到名額，網站可能維護中或已改版，自動預約暫時沒有作用。\n"
        f"請自行確認：{BASE_URL}/parkingreserve/"
    )
    if await send_all(aliases, subject, body):
        status["health_alert_at"] = _iso(now_tw())
        status["dirty"] = True


def in_midnight_burst(now: datetime, bookings: list) -> bool:
    """網站每天 00:00 開放新日期。目標日期今晚開放時，撐過午夜並縮短輪詢間隔（23:55～00:03）。"""
    minutes = now.hour * 60 + now.minute
    if minutes >= 23 * 60 + 55:
        opening = now.date() + timedelta(days=1)
    elif minutes < 3:
        opening = now.date()
    else:
        return False
    return any(b.open_date == opening for b in bookings)


def wait_seconds(now: datetime, settings: dict, burst: bool) -> float:
    if burst and (now.hour == 0 or (now.minute == 59 and now.second >= 45)):
        return BURST_INTERVAL_SECONDS
    return random.uniform(settings["interval_min"], settings["interval_max"])


async def monitor(settings: dict, bookings: list, status: dict):
    started = now_tw()
    reloaded = started
    health = {"fails": 0}
    round_num = 0
    while True:
        active = active_bookings(bookings, status)
        if not settings["enabled"] or not active:
            log.info("監控關閉或沒有需要監看的預約，結束")
            break
        round_num += 1
        await check_round(active, status, settings, health, round_num)
        save_status(status, {b.alias for b in bookings})

        now = now_tw()
        burst = in_midnight_burst(now, active_bookings(bookings, status))
        if now - started >= timedelta(minutes=settings["run_minutes"]) and not burst:
            break
        await asyncio.sleep(wait_seconds(now, settings, burst))

        if (now_tw() - reloaded).total_seconds() >= CONFIG_RELOAD_SECONDS:
            reloaded = now_tw()
            fresh = await asyncio.to_thread(reload_remote_config)
            if fresh:
                settings, bookings = fresh
    log.info(f"本次共檢查 {round_num} 輪")


# ─────────────────────────────────────────────
# 測試模式
# ─────────────────────────────────────────────

async def run_dry_test(bookings: list, status: dict) -> bool:
    """實際走一次網站流程到填好表單，不送出。P4 常常全滿，沒名額時改用流程相同的日月亭。"""
    enabled = [b for b in bookings if b.enabled]
    sample = enabled[0] if enabled else Booking(alias="Booking_1", site="riyueting", date=date.today(), days=MIN_DAYS)
    booking = Booking(
        alias=sample.alias, site=sample.site, date=sample.date, days=sample.days,
        name=sample.name or "測試", plate=sample.plate or "AA-1234",
    )
    outcome = Outcome("failed", "兩個停車場目前都沒有名額，無法測試填單")
    for site in dict.fromkeys([booking.site, "riyueting"]):
        lefts = await asyncio.to_thread(fetch_left, site)
        open_dates = [day for day, left in sorted((lefts or {}).items()) if left > 0]
        if not open_dates:
            continue
        booking.site = site
        chosen = booking.date.isoformat() if booking.date.isoformat() in open_dates else open_dates[0]
        outcome = await attempt_booking(booking, dry_run=True, on_date=chosen)
        outcome.detail = f"{SITES[site]['name']} {chosen}：{outcome.detail}"
        break
    ok = outcome.kind == "dry_ok"
    log.info(f"測試填單：{'通過' if ok else '失敗'}｜{_scrub(outcome.detail, booking)}")
    status["last_test"] = {"at": _iso(now_tw()), "ok": ok, "detail": _scrub(outcome.detail, booking)}
    status["dirty"] = True
    return ok


async def run_send_test() -> bool:
    aliases = known_recipients()
    if not aliases:
        log.error("沒有任何收件人：請在 UI 新增收件人")
        return False
    sample = Booking(
        alias="Booking_1", site="p4", date=now_tw().date() + timedelta(days=LATEST_DAYS), days=5,
        name="王小明", plate="AA-1234",
    )
    subject = "🔔 停車預約測試通知"
    body = "這是測試通知，下面的車牌和姓名是範例。收到有名額通知時長這樣：\n\n" + _kit(sample)
    ok = await send_all(aliases, subject, body, sample)
    log.info(f"測試通知：{'已送出' if ok else '全部失敗'}（{'、'.join(aliases)}）")
    return ok


# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────

def precheck() -> bool:
    """workflow 用：沒有要監看的預約就不用安裝瀏覽器。"""
    settings, bookings = load_config()
    if os.environ.get("DRY_RUN") or os.environ.get("SEND_TEST"):
        return True
    return settings["enabled"] and bool(active_bookings(bookings, load_status()))


async def main() -> int:
    _mask_secrets_in_actions_log()
    settings, bookings = load_config()
    status = load_status()
    log.info(
        f"停車場預約 Agent | {len(bookings)} 筆預約 | 監看 {settings['run_minutes']} 分鐘 | "
        f"間隔 {settings['interval_min']}～{settings['interval_max']} 秒"
    )

    if os.environ.get("SEND_TEST"):
        return 0 if await run_send_test() else 1
    if os.environ.get("DRY_RUN"):
        ok = await run_dry_test(bookings, status)
        save_status(status)
        return 0 if ok else 1

    await monitor(settings, bookings, status)
    save_status(status, {b.alias for b in bookings})
    return 0


if __name__ == "__main__":
    if "--precheck" in sys.argv:
        active = precheck()
        print(f"active={'true' if active else 'false'}")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write(f"active={'true' if active else 'false'}\n")
        sys.exit(0)
    sys.exit(asyncio.run(main()))
