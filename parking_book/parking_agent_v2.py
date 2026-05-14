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
# 反封鎖：隨機 User-Agent 池
# ─────────────────────────────────────────────

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

def _random_viewport():
    presets = [
        {"width": 1920, "height": 1080},
        {"width": 1440, "height": 900},
        {"width": 1366, "height": 768},
        {"width": 1280, "height": 800},
        {"width": 1536, "height": 864},
    ]
    return random.choice(presets)

def _jitter(base_ms: int, pct: float = 0.3) -> int:
    delta = int(base_ms * pct)
    return base_ms + random.randint(-delta, delta)

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
        log.info(f"Gmail 通知發送成功 → {GMAIL_RECIPIENTS}")
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

# ─────────────────────────────────────────────
# 預約記錄驗證
# ─────────────────────────────────────────────

async def verify_booking(page) -> bool:
    """
    預約完成後，前往查詢記錄確認是否真的成功。
    回傳 True = 找到記錄；False = 找不到或例外。
    """
    try:
        log.info("開始驗證預約記錄...")

        # 回到平台首頁
        await page.get_by_role("link", name="預約停車平台").click()
        await page.wait_for_timeout(_jitter(1500))
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except AsyncPlaywrightTimeoutError:
            pass

        # 進入選單頁（點第一個「前往」）
        await page.get_by_role("link", name="前往").first.click()
        await page.wait_for_timeout(_jitter(1000))

        # 找「預約記錄」那一列的「前往」
        record_row = page.locator("tr, li, div").filter(has_text="預約記錄").first
        await record_row.get_by_role("link", name="前往").click()
        await page.wait_for_timeout(_jitter(1500))
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except AsyncPlaywrightTimeoutError:
            pass

        # 輸入車牌查詢
        plate_field = page.get_by_role("textbox", name="車號 (例: AA-1234)")
        await plate_field.click()
        await plate_field.fill(BOOKER_PLATE)
        await page.get_by_role("button", name="查 詢").click()
        await page.wait_for_timeout(_jitter(2000))

        # 確認結果含目標日期（格式轉換：05-23 → 05/23）
        page_text = await page.inner_text("body")
        date_fragment = TARGET_DATE.replace("-", "/")
        if date_fragment in page_text:
            log.info(f"✅ 預約記錄確認：找到 {date_fragment}")
            return True
        log.warning(f"⚠️ 查詢記錄中未找到 {date_fragment}")
        return False
    except Exception as e:
        log.error(f"驗證預約記錄例外：{e}", exc_info=True)
        return False


# ─────────────────────────────────────────────
# 核心檢查與自動預約
# ─────────────────────────────────────────────

async def check_and_book() -> bool:
    """
    回傳 True 表示已處理完畢（不論成功或失敗），停止後續輪次。
    回傳 False 表示尚未開放或導航失敗，繼續下一輪。
    """
    ua       = random.choice(_USER_AGENTS)
    viewport = _random_viewport()
    log.info(f"開始檢查 {TARGET_DATE} | UA: ...{ua[-40:]} | {viewport['width']}x{viewport['height']}")

    # 旗標：是否已按下「送出」
    # True  → 之後逾時狀態不明，需通知
    # False → 送出前發生逾時（如重新導向），僅 log，不通知，下一輪重試
    submitted = False

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            slow_mo=_jitter(400),
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

            await page.wait_for_timeout(_jitter(1500))

            await page.get_by_role("link", name="前往").first.click()
            await page.wait_for_timeout(_jitter(1000))
            await page.locator(".v-input--selection-controls__ripple").click()
            await page.wait_for_timeout(_jitter(600))
            await page.get_by_role("button", name="前往預約").click()
            await page.wait_for_timeout(_jitter(2000))
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
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
            await page.wait_for_timeout(_jitter(1500))

            # ── 填入停放天數 ──
            days_field = page.get_by_role("textbox", name="停放天數")
            await days_field.click()
            await days_field.fill(str(PARKING_DAYS))
            await page.wait_for_timeout(_jitter(500))

            # ── 填入姓名 ──
            name_field = page.get_by_role("textbox", name="姓名")
            await name_field.click()
            await name_field.fill(BOOKER_NAME)
            await page.wait_for_timeout(_jitter(500))

            # ── 填入車牌號碼 ──
            plate_field = page.get_by_role("textbox", name="車牌號碼 (例: AA-1234)")
            await plate_field.fill(BOOKER_PLATE)
            await page.wait_for_timeout(_jitter(500))

            # ── 點擊「送出」（送出後設旗標，之後的逾時才需通知）──
            await page.get_by_role("button", name="送出").click()
            submitted = True
            log.info("已點擊送出，等待結果...")
            await page.wait_for_timeout(_jitter(3000))
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except AsyncPlaywrightTimeoutError:
                pass

            # ── 關閉送出後跳出的確認/結果視窗 ──
            try:
                close_btn = page.get_by_role("button").nth(2)
                if await close_btn.count() > 0 and await close_btn.is_visible(timeout=3000):
                    await close_btn.click()
                    await page.wait_for_timeout(_jitter(1000))
                    log.info("已關閉跳出視窗")
            except Exception:
                pass  # 視窗不存在或已自動關閉，略過

            # ── 判斷是否成功（精確比對完成訊息，再查詢記錄雙重確認）──
            page_text = await page.inner_text("body")
            if "您已完成線上預約登記" in page_text:
                log.info("表單顯示完成，開始查詢記錄雙重確認...")
                verified = await verify_booking(page)
                if verified:
                    log.info("🎉 預約成功並已確認記錄！")
                    notify_booked_success()
                else:
                    log.warning("⚠️ 表單顯示完成，但查詢記錄未找到")
                    notify_booked_failed("預約頁面顯示完成，但查詢記錄未找到，請手動確認")
            else:
                log.warning("⚠️ 送出後未偵測到完成訊息，可能表單未成功送出")
                notify_booked_failed("送出後未偵測到明確完成訊息，請手動確認")

            return True   # 不論成功失敗都停止輪詢

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
            wait_sec = _jitter(60_000, pct=0.15) // 1000
            log.info(f"等待 {wait_sec} 秒後進行下一輪...")
            await asyncio.sleep(wait_sec)

    log.info("所有輪次完成")


if __name__ == "__main__":
    asyncio.run(main())
