"""
停車場預約 Agent v2
偵測 https://pcc.youparking.com.tw/parkingreserve/#/
當 TARGET_DATE 出現可預約按鈕時，透過 LINE / Gmail 通知

安裝：
    pip install playwright apscheduler requests
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

TARGET_DATE = "05-23"

# GitHub Actions 模式：一次執行幾輪（每輪間隔 ~60 秒）
# 本機單次執行時設為 1
ROUNDS = int(os.environ.get("CHECK_ROUNDS", "1"))

LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_USER_ID              = os.environ["LINE_USER_ID"]

GMAIL_SENDER    = os.environ["GMAIL_SENDER"]
GMAIL_PASSWORD  = os.environ["GMAIL_PASSWORD"]
GMAIL_RECIPIENT = os.environ.get("GMAIL_RECIPIENT", os.environ["GMAIL_SENDER"])

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
    """回傳接近真實使用者的隨機解析度"""
    presets = [
        {"width": 1920, "height": 1080},
        {"width": 1440, "height": 900},
        {"width": 1366, "height": 768},
        {"width": 1280, "height": 800},
        {"width": 1536, "height": 864},
    ]
    return random.choice(presets)

def _jitter(base_ms: int, pct: float = 0.3) -> int:
    """在 base_ms ± pct 範圍內加入隨機抖動，模擬真人操作速度"""
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
        msg["To"]      = GMAIL_RECIPIENT
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(GMAIL_SENDER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_SENDER, GMAIL_RECIPIENT, msg.as_string())
        log.info("Gmail 通知發送成功")
        return True
    except Exception as e:
        log.error(f"Gmail 通知例外：{e}")
        return False


def send_notifications(message: str):
    notify_line(f"🚗 停車預約通知\n\n{message}")
    notify_gmail(
        subject="🚗 停車場可以預約了！",
        body=f"{message}\n\n請前往：https://pcc.youparking.com.tw/parkingreserve/#/",
    )

# ─────────────────────────────────────────────
# 核心檢查
# ─────────────────────────────────────────────

async def check_and_book() -> bool:
    ua       = random.choice(_USER_AGENTS)
    viewport = _random_viewport()
    log.info(f"開始檢查 {TARGET_DATE} | UA: ...{ua[-40:]} | viewport: {viewport['width']}x{viewport['height']}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            slow_mo=_jitter(400),   # 操作間隔隨機化
        )
        context = await browser.new_context(
            user_agent=ua,
            viewport=viewport,
            locale="zh-TW",
            timezone_id="Asia/Taipei",
        )
        page = await context.new_page()
        try:
            await page.goto(
                "https://pcc.youparking.com.tw/parkingreserve/#/",
                wait_until="networkidle",
                timeout=30_000,
            )
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

            target_row = page.locator(f"tr:has(td:has-text('{TARGET_DATE}'))").first
            if await target_row.count() == 0:
                log.warning(f"找不到 {TARGET_DATE} 的列，頁面可能尚未開放該日期")
                return False

            is_full     = await target_row.locator(":has-text('已滿')").count() > 0
            is_bookable = await target_row.locator("button, a").filter(has_text="預約").count() > 0

            if is_full:
                log.info(f"❌ {TARGET_DATE} 已滿")
                return False
            elif is_bookable:
                log.info(f"✅ {TARGET_DATE} 可以預約！")
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                send_notifications(
                    f"【{TARGET_DATE} 停車位可以預約了！】\n"
                    f"偵測時間：{now_str}\n"
                    f"請立即前往預約！"
                )
                return True
            else:
                log.warning(f"⚠️ {TARGET_DATE} 狀態未知，請手動確認")
                return False

        except AsyncPlaywrightTimeoutError as e:
            log.error(f"頁面操作逾時：{e}")
            return False
        except Exception as e:
            log.error(f"執行時發生例外：{e}", exc_info=True)
            return False
        finally:
            await browser.close()


# ─────────────────────────────────────────────
# 主程式：支援多輪模式（GitHub Actions 用）
# ─────────────────────────────────────────────

async def main():
    log.info(f"停車場預約 Agent | 目標：{TARGET_DATE} | 執行輪數：{ROUNDS}")

    for round_num in range(1, ROUNDS + 1):
        if ROUNDS > 1:
            log.info(f"── 第 {round_num}/{ROUNDS} 輪 ──")

        found = await check_and_book()
        if found:
            log.info("已通知，結束所有輪次")
            return

        if round_num < ROUNDS:
            wait_sec = _jitter(60_000, pct=0.15) // 1000   # ~60 秒 ± 15%
            log.info(f"等待 {wait_sec} 秒後進行下一輪...")
            await asyncio.sleep(wait_sec)

    log.info("所有輪次完成")


if __name__ == "__main__":
    asyncio.run(main())
