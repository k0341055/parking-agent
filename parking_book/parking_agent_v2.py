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
import smtplib
from datetime import datetime
from email.mime.text import MIMEText

import os

import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from playwright.async_api import async_playwright, TimeoutError as AsyncPlaywrightTimeoutError

# ─────────────────────────────────────────────
# 設定區（憑證從環境變數讀取，本機測試請建立 .env 並搭配 python-dotenv）
# ─────────────────────────────────────────────

TARGET_DATE = "05-23"           # 頁面日期格式為 "2026-05-23 (六)"，模糊比對
CHECK_INTERVAL_MINUTES = 5

LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_USER_ID              = os.environ["LINE_USER_ID"]

GMAIL_SENDER    = os.environ["GMAIL_SENDER"]
GMAIL_PASSWORD  = os.environ["GMAIL_PASSWORD"]
GMAIL_RECIPIENT = os.environ.get("GMAIL_RECIPIENT", os.environ["GMAIL_SENDER"])

# ─────────────────────────────────────────────
# Logger
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("parking_agent.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
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
# 核心檢查（已驗證 selector）
# ─────────────────────────────────────────────

async def check_and_book() -> bool:
    log.info(f"開始檢查 {TARGET_DATE} 是否可預約...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=300)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        try:
            await page.goto(
                "https://pcc.youparking.com.tw/parkingreserve/#/",
                wait_until="networkidle",
                timeout=30_000,
            )
            await page.wait_for_timeout(1500)

            await page.get_by_role("link", name="前往").first.click()
            await page.wait_for_timeout(1000)

            await page.locator(".v-input--selection-controls__ripple").click()
            await page.wait_for_timeout(500)

            await page.get_by_role("button", name="前往預約").click()
            await page.wait_for_timeout(2000)
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
                log.info(f"❌ {TARGET_DATE} 已滿，下次再試")
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
            try:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                await page.screenshot(path=f"screenshot_{ts}.png")
            except Exception:
                pass
            await browser.close()

# ─────────────────────────────────────────────
# 排程器
# ─────────────────────────────────────────────

_notified = False


async def scheduled_job():
    global _notified
    if _notified:
        log.info("已發送過通知，跳過（重啟可重置）")
        return
    success = await check_and_book()
    if success:
        _notified = True


async def main():
    log.info("=" * 50)
    log.info(f"停車場預約 Agent 啟動 | 目標：{TARGET_DATE} | 間隔：{CHECK_INTERVAL_MINUTES} 分鐘")
    log.info("=" * 50)

    await scheduled_job()
    if _notified:
        return

    scheduler = AsyncIOScheduler(timezone="Asia/Taipei")
    scheduler.add_job(scheduled_job, "interval", minutes=CHECK_INTERVAL_MINUTES)
    scheduler.start()
    log.info("排程器已啟動，按 Ctrl+C 結束")

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        log.info("使用者中止，程式結束")
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
