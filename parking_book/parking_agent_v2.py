"""
停車場預約 Agent v2
偵測 https://pcc.youparking.com.tw/parkingreserve/#/
當 TARGET_DATE 出現可預約按鈕時：
  1. 發送「可以預約」通知
  2. 自動填入資料並送出
  3. 發送「預約成功/失敗」通知

安裝：
    pip install playwright requests
    playwright install chromium

執行：
    python parking_agent_v2.py
"""

import asyncio
import json
import logging
import os
import random
import re
import smtplib
from datetime import datetime
from email.mime.text import MIMEText

import requests
from playwright.async_api import async_playwright, TimeoutError as AsyncPlaywrightTimeoutError

# ─────────────────────────────────────────────
# 設定區
# ─────────────────────────────────────────────

TARGET_DATE   = "05-23"   # 頁面日期格式 "2026-05-23 (六)"，模糊比對
PARKING_DAYS  = int(os.environ.get("PARKING_DAYS", "5"))   # 停放天數

# GitHub Actions 模式：每次 workflow 執行幾輪（每輪間隔 ~60 秒）
ROUNDS = int(os.environ.get("CHECK_ROUNDS", "1"))

# 通知憑證（從環境變數讀取，不寫在程式碼裡）
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_USER_ID              = os.environ["LINE_USER_ID"]

GMAIL_SENDER     = os.environ["GMAIL_SENDER"]
GMAIL_PASSWORD   = os.environ["GMAIL_PASSWORD"]
GMAIL_RECIPIENTS = [
    addr.strip()
    for addr in os.environ.get("GMAIL_RECIPIENTS", os.environ["GMAIL_SENDER"]).split(",")
    if addr.strip()
]

# 個人資料（從環境變數讀取，不寫在程式碼裡）
BOOKER_NAME  = os.environ["BOOKER_NAME"]    # 姓名
BOOKER_PLATE = os.environ["BOOKER_PLATE"]   # 車牌號碼

# ─────────────────────────────────────────────
# 反封鎖：隨機組合 User-Agent
# ─────────────────────────────────────────────

_UA_OS = [
    "Windows NT 10.0; Win64; x64",
    "Windows NT 11.0; Win64; x64",
    "Macintosh; Intel Mac OS X 10_15_7",
    "Macintosh; Intel Mac OS X 13_4",
    "Macintosh; Intel Mac OS X 14_0",
    "X11; Linux x86_64",
    "X11; Ubuntu; Linux x86_64",
]

_UA_CHROME_VERSIONS = list(range(118, 126))   # 118~125 皆為真實存在版本
_UA_FIREFOX_VERSIONS = list(range(118, 127))  # 118~126
_UA_SAFARI_VERSIONS = [
    ("605.1.15", "17.0"),
    ("605.1.15", "17.2"),
    ("605.1.15", "17.4.1"),
    ("605.1.15", "17.5"),
]
_UA_WEBKIT_BUILD = list(range(530, 538))      # WebKit 次版本號微幅隨機

def _random_user_agent() -> str:
    browser = random.choices(["chrome", "firefox", "safari"], weights=[65, 25, 10])[0]
    os_str  = random.choice(_UA_OS)

    if browser == "chrome":
        major   = random.choice(_UA_CHROME_VERSIONS)
        minor   = random.randint(0, 9)
        webkit  = f"537.{random.choice(_UA_WEBKIT_BUILD)}"
        return (
            f"Mozilla/5.0 ({os_str}) "
            f"AppleWebKit/{webkit} (KHTML, like Gecko) "
            f"Chrome/{major}.0.{random.randint(5000,7000)}.{minor} "
            f"Safari/{webkit}"
        )
    elif browser == "firefox":
        major = random.choice(_UA_FIREFOX_VERSIONS)
        minor = random.randint(0, 3)
        return (
            f"Mozilla/5.0 ({os_str}; rv:{major}.{minor}) "
            f"Gecko/20100101 Firefox/{major}.{minor}"
        )
    else:  # safari（只對 Mac 有意義）
        mac_os = random.choice([s for s in _UA_OS if "Macintosh" in s])
        webkit_ver, safari_ver = random.choice(_UA_SAFARI_VERSIONS)
        return (
            f"Mozilla/5.0 ({mac_os}) "
            f"AppleWebKit/{webkit_ver} (KHTML, like Gecko) "
            f"Version/{safari_ver} Safari/{webkit_ver}"
        )

def _random_viewport():
    presets = [
        {"width": 1920, "height": 1080},
        {"width": 1440, "height": 900},
        {"width": 1366, "height": 768},
        {"width": 1280, "height": 800},
        {"width": 1536, "height": 864},
        {"width": 1600, "height": 900},
        {"width": 2560, "height": 1440},
    ]
    return random.choice(presets)

def _jitter(base_ms: int, pct: float = 0.3) -> int:
    delta = int(base_ms * pct)
    return base_ms + random.randint(-delta, delta)

def _mask_email(email: str) -> str:
    """遮蔽 email 用於 log，格式：k***@gmail.com"""
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    return f"{local[0]}***@{domain}"

# ─────────────────────────────────────────────
# Logger
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 通知
# ─────────────────────────────────────────────

def notify_line(message: str) -> bool:
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            },
            data=json.dumps(
                {"to": LINE_USER_ID, "messages": [{"type": "text", "text": message}]},
                ensure_ascii=False,
            ).encode("utf-8"),
            timeout=10,
        )
        if resp.status_code == 200:
            log.info("LINE 通知發送成功")
            return True
        log.error(f"LINE 通知失敗：{resp.status_code} {resp.text}")
        return False
    except Exception as e:
        log.error(f"LINE 通知例外：{e}")
        return False


def notify_gmail(subject: str, body: str) -> bool:
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_SENDER
        msg["To"]      = ", ".join(GMAIL_RECIPIENTS)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(GMAIL_SENDER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_SENDER, GMAIL_RECIPIENTS, msg.as_string())
        masked = [_mask_email(r) for r in GMAIL_RECIPIENTS]
        log.info(f"Gmail 通知發送成功 → {masked}")
        return True
    except Exception as e:
        log.error(f"Gmail 通知例外：{e}")
        return False


def notify_available():
    """通知 1：偵測到可預約，自動預約進行中"""
    msg = (
        f"【{TARGET_DATE} 停車位可以預約！】\n"
        f"偵測時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"正在自動填單送出，請稍候..."
    )
    notify_line(f"🚗 停車預約通知\n\n{msg}")
    notify_gmail(subject="🚗 停車場可以預約了！正在自動預約...", body=msg)


def notify_booked_success():
    """通知 2：預約成功"""
    msg = (
        f"【{TARGET_DATE} 預約成功！】\n"
        f"停放天數：{PARKING_DAYS} 天\n"
        f"完成時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    notify_line(f"✅ 停車預約成功！\n\n{msg}")
    notify_gmail(subject="✅ 停車預約成功！", body=msg)


def notify_booked_failed(reason: str):
    """通知 2（失敗）：自動預約失敗，請手動操作"""
    msg = (
        f"【{TARGET_DATE} 自動預約失敗】\n"
        f"原因：{reason}\n"
        f"請立即手動前往：https://pcc.youparking.com.tw/parkingreserve/#/"
    )
    notify_line(f"⚠️ 自動預約失敗，請手動操作！\n\n{msg}")
    notify_gmail(subject="⚠️ 停車自動預約失敗，請手動操作！", body=msg)


def notify_already_booked_confirmed():
    """通知：偵測到「已登記預約」提示，查詢記錄確認存在"""
    msg = (
        f"【{TARGET_DATE} 已有預約記錄！】\n"
        f"送出時提示已登記，查詢記錄確認存在。\n"
        f"確認時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    notify_line(f"✅ 停車預約已確認存在！\n\n{msg}")
    notify_gmail(subject="✅ 停車預約已確認存在！", body=msg)

# ─────────────────────────────────────────────
# 預約記錄驗證
# ─────────────────────────────────────────────

async def verify_booking(page) -> bool:
    """
    預約完成後，前往查詢記錄確認是否真的成功。
    回傳 True = 找到記錄；False = 找不到或導航失敗。
    用 goto 重進首頁，避免 click 導航在 dialog 後頁面狀態不穩定。
    """
    try:
        log.info("開始驗證預約記錄...")

        # 直接重進首頁（比 click link 更可靠）
        await page.goto(
            "https://pcc.youparking.com.tw/parkingreserve/#/",
            wait_until="networkidle",
            timeout=20_000,
        )
        await page.wait_for_timeout(_jitter(800))

        # 進入選單頁（點第一個「前往」）
        await page.get_by_role("link", name="前往").first.click()
        await page.wait_for_timeout(_jitter(600))

        # 找「預約記錄」那一列的「前往」
        record_row = page.locator("tr, li, div").filter(has_text="預約記錄").first
        if await record_row.count() == 0:
            log.warning("⚠️ 找不到「預約記錄」入口")
            return False
        await record_row.get_by_role("link", name="前往").click()
        await page.wait_for_timeout(_jitter(800))
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except AsyncPlaywrightTimeoutError:
            pass

        # 輸入車牌查詢
        plate_field = page.get_by_role("textbox", name="車號 (例: AA-1234)")
        await plate_field.click()
        await plate_field.fill(BOOKER_PLATE)
        await page.get_by_role("button", name="查 詢").click()
        await page.wait_for_timeout(_jitter(1500))

        # 確認結果含目標日期（格式轉換：05-23 → 05/23）
        page_text = await page.inner_text("body")
        date_fragment = TARGET_DATE.replace("-", "/")
        if date_fragment in page_text:
            log.info(f"✅ 預約記錄確認：找到 {date_fragment}")
            return True
        log.warning(f"⚠️ 查詢記錄中未找到 {date_fragment}，頁面片段：{page_text[:200]!r}")
        return False
    except Exception as e:
        log.error(f"驗證預約記錄例外（導航失敗）：{e}", exc_info=True)
        return False


# ─────────────────────────────────────────────
# 核心檢查與自動預約
# ─────────────────────────────────────────────

async def check_and_book() -> bool:
    """
    回傳 True 表示已處理完畢（不論成功或失敗），停止後續輪次。
    回傳 False 表示尚未開放或導航失敗，繼續下一輪。
    """
    ua       = _random_user_agent()
    viewport = _random_viewport()
    log.info(f"開始檢查 {TARGET_DATE} | UA: ...{ua[-40:]} | {viewport['width']}x{viewport['height']}")

    # 旗標：是否已按下「送出」
    # True  → 之後逾時狀態不明，需通知
    # False → 送出前發生逾時（如重新導向），僅 log，不通知，下一輪重試
    submitted = False

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            slow_mo=_jitter(80),
        )
        context = await browser.new_context(
            user_agent=ua,
            viewport=viewport,
            locale="zh-TW",
            timezone_id="Asia/Taipei",
        )
        page = await context.new_page()
        try:
            # ── 進入預約列表（導航失敗直接關閉瀏覽器重試，不通知）──
            try:
                await page.goto(
                    "https://pcc.youparking.com.tw/parkingreserve/#/",
                    wait_until="networkidle",
                    timeout=30_000,
                )
            except AsyncPlaywrightTimeoutError:
                log.warning("導航逾時或被重新導向，關閉瀏覽器，本輪跳過（不通知）")
                return False

            await page.wait_for_timeout(_jitter(800))

            await page.get_by_role("link", name="前往").first.click()
            await page.wait_for_timeout(_jitter(500))
            await page.locator(".v-input--selection-controls__ripple").click()
            await page.wait_for_timeout(_jitter(300))
            await page.get_by_role("button", name="前往預約").click()
            await page.wait_for_timeout(_jitter(1000))
            try:
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except AsyncPlaywrightTimeoutError:
                pass

            # ── 找目標日期列 ──
            target_row = page.locator(f"tr:has(td:has-text('{TARGET_DATE}'))").first
            if await target_row.count() == 0:
                log.warning(f"找不到 {TARGET_DATE} 的列，頁面可能尚未開放")
                return False

            is_full     = await target_row.locator(":has-text('已滿')").count() > 0
            is_bookable = await target_row.locator("button, a").filter(has_text="預約").count() > 0

            if is_full:
                log.info(f"❌ {TARGET_DATE} 已滿")
                return False

            if not is_bookable:
                log.warning(f"⚠️ {TARGET_DATE} 狀態未知")
                return False
            
            # ── 可預約：發送通知 1 ──
            log.info(f"✅ {TARGET_DATE} 可以預約！開始自動填單...")
            notify_available()

            # ── 點擊「預約」按鈕 ──
            book_btn = target_row.locator("button, a").filter(has_text="預約").first
            await book_btn.click()
            await page.wait_for_timeout(_jitter(800))

            # ── 填入停放天數 ──
            days_field = page.get_by_role("textbox", name="停放天數")
            await days_field.click()
            await days_field.fill(str(PARKING_DAYS))
            await page.wait_for_timeout(_jitter(200))

            # ── 填入姓名 ──
            name_field = page.get_by_role("textbox", name="姓名")
            await name_field.click()
            await name_field.fill(BOOKER_NAME)
            await page.wait_for_timeout(_jitter(200))

            # ── 填入車牌號碼 ──
            plate_field = page.get_by_role("textbox", name="車牌號碼 (例: AA-1234)")
            await plate_field.fill(BOOKER_PLATE)
            await page.wait_for_timeout(_jitter(200))

            # ── 點擊「送出」（送出後設旗標，之後的逾時才需通知）──
            await page.get_by_role("button", name="送出").click()
            submitted = True
            log.info("已點擊送出，等待結果...")

            # ── 主動等待 dialog 內容出現（避免 Vue 非同步渲染導致讀到空內容）──
            _expected_patterns = ["您已完成線上預約登記", "已登記預約", "登記預約"]
            for _kw in _expected_patterns:
                try:
                    await page.wait_for_selector(f"text={_kw}", timeout=8_000)
                    log.info(f"偵測到跳出訊息關鍵字：{_kw}")
                    break
                except AsyncPlaywrightTimeoutError:
                    pass

            # ── 先讀取跳出視窗內容，再關閉 ──
            page_text = await page.inner_text("body")

            try:
                close_btn = page.get_by_role("button").nth(2)
                if await close_btn.count() > 0 and await close_btn.is_visible(timeout=3000):
                    await close_btn.click()
                    await page.wait_for_timeout(_jitter(1000))
                    log.info("已關閉跳出視窗")
            except Exception:
                pass  # 視窗不存在或已自動關閉，略過

            # ── 判斷結果：三種情況 ──
            # 情況 A：當次預約完成
            if "您已完成線上預約登記" in page_text:
                log.info("表單顯示完成，開始查詢記錄雙重確認...")
                verified = await verify_booking(page)
                if verified:
                    log.info("🎉 預約成功並已確認記錄！")
                    notify_booked_success()
                else:
                    log.warning("⚠️ 表單顯示完成，但查詢記錄未找到")
                    notify_booked_failed("預約頁面顯示完成，但查詢記錄未找到，請手動確認")

            # 情況 B：車牌已有舊的登記預約（非當次）
            elif re.search(
                rf"車號\s*\[{re.escape(BOOKER_PLATE)}\].*?已於.*?登記預約",
                page_text,
                re.DOTALL,
            ):
                log.info("偵測到已有登記預約提示，開始查詢記錄確認...")
                verified = await verify_booking(page)
                if verified:
                    log.info("✅ 已有預約記錄並確認存在！")
                    notify_already_booked_confirmed()
                else:
                    log.warning("⚠️ 顯示已登記，但查詢記錄未找到")
                    notify_booked_failed("送出時提示已登記，但查詢記錄未找到，請手動確認")

            # 情況 C：未知結果 → 截圖供 debug，繼續下一輪重試（不通知、不停止）
            else:
                screenshot_path = f"/tmp/parking_unknown_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                try:
                    await page.screenshot(path=screenshot_path, full_page=True)
                    log.warning(f"⚠️ 未知結果截圖已存：{screenshot_path}")
                except Exception:
                    pass
                log.warning(f"⚠️ 送出後未偵測到完成訊息，本輪跳過繼續重試，頁面片段：{page_text[:200]!r}")
                return False   # 繼續下一輪重試，不停止

            return True   # 情況 A / B 已處理完畢，停止輪詢

        except AsyncPlaywrightTimeoutError as e:
            if submitted:
                # 送出後逾時：狀態不明，需通知
                log.error(f"送出後頁面逾時，狀態不明：{e}")
                notify_booked_failed("送出後頁面逾時，請手動確認是否預約成功")
                return True
            else:
                # 送出前逾時（如重新導向）：僅 log，不通知，下一輪重試
                log.warning(f"送出前頁面逾時（可能被重新導向），關閉瀏覽器重試，本輪跳過（不通知）：{e}")
                return False
        except Exception as e:
            log.error(f"執行時發生例外：{e}", exc_info=True)
            return False
        finally:
            await browser.close()


# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────

async def main():
    log.info(f"停車場預約 Agent | 目標：{TARGET_DATE} | 停放天數：{PARKING_DAYS} | 輪數：{ROUNDS}")

    for round_num in range(1, ROUNDS + 1):
        if ROUNDS > 1:
            log.info(f"── 第 {round_num}/{ROUNDS} 輪 ──")

        done = await check_and_book()
        if done:
            log.info("已處理完畢，結束所有輪次")
            return

        if round_num < ROUNDS:
            wait_sec = _jitter(22_000, pct=0.15) // 1000
            log.info(f"等待 {wait_sec} 秒後進行下一輪...")
            await asyncio.sleep(wait_sec)

    log.info("所有輪次完成")


if __name__ == "__main__":
    asyncio.run(main())
