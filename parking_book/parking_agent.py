"""
停車場預約 AI Agent
目標：定時偵測 https://pcc.youparking.com.tw/parkingreserve/#/reservedlist/1
      當 5/23 可以預約時，透過 LINE Messaging API 或 Gmail 通知

安裝依賴：
    pip install playwright apscheduler requests
    playwright install chromium

注意：LINE Notify 已於 2025/3/31 停止服務，改用 LINE Messaging API
"""

import json
import logging
import os
import smtplib
from datetime import datetime
from email.mime.text import MIMEText

import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ─────────────────────────────────────────────
# 設定區（請填入你的資訊）
# ─────────────────────────────────────────────

TARGET_DATE = "5/23"          # 要偵測的日期字串（依頁面顯示格式調整）
CHECK_INTERVAL_MINUTES = 5    # 幾分鐘檢查一次

# ── LINE Messaging API ──────────────────────────────────────────────────────
# ⚠️  LINE Notify 已於 2025/3/31 終止，請改用 Messaging API
# 申請步驟：
#   1. 前往 https://developers.line.biz/console/ 建立 Provider & Channel（Messaging API）
#   2. Channel 頁面 → Messaging API → Issue channel access token（長期）
#   3. 讓自己掃 QR code 加入官方帳號，傳一則訊息給它
#   4. 在 LINE Developers Console → Messaging API 頁面下方
#      「Your user ID」欄位直接取得自己的 User ID（以大寫 U 開頭）
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_USER_ID = os.environ["LINE_USER_ID"]

# ── Gmail（選填，SMTP 方式）──────────────────────────────────────────────────
GMAIL_SENDER    = os.environ["GMAIL_SENDER"]
GMAIL_PASSWORD  = os.environ["GMAIL_PASSWORD"]
GMAIL_RECIPIENT = os.environ.get("GMAIL_RECIPIENT", os.environ["GMAIL_SENDER"])

# ── 自動預約 ────────────────────────────────────────────────────────────────
# True = 偵測到後直接幫你按預約；False = 只通知，讓你自己手動去預約
AUTO_BOOK = False

# ─────────────────────────────────────────────
# Logger 設定
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
# 通知模組
# ─────────────────────────────────────────────

def notify_line(message: str) -> bool:
    """
    透過 LINE Messaging API 發送 Push Message 給指定 User ID。
    文件：https://developers.line.biz/en/docs/messaging-api/sending-messages/
    """
    if not LINE_CHANNEL_ACCESS_TOKEN:
        log.warning("LINE Messaging API token 未設定，跳過 LINE 通知")
        return False
    if not LINE_USER_ID:
        log.warning("LINE User ID 未設定，跳過 LINE 通知")
        return False

    try:
        payload = {
            "to": LINE_USER_ID,
            "messages": [
                {
                    "type": "text",
                    "text": message,
                }
            ],
        }
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            },
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=10,
        )
        if resp.status_code == 200:
            log.info("LINE 通知發送成功")
            return True
        else:
            log.error(f"LINE 通知失敗：{resp.status_code} {resp.text}")
            return False
    except Exception as e:
        log.error(f"LINE 通知例外：{e}")
        return False


def notify_gmail(subject: str, body: str) -> bool:
    """透過 Gmail SMTP 發送 Email"""
    if not GMAIL_SENDER or GMAIL_SENDER == "your_email@gmail.com":
        log.warning("Gmail 未設定，跳過 Email 通知")
        return False
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
    """同時觸發所有通知管道"""
    notify_line(f"🚗 停車預約通知\n\n{message}")
    notify_gmail(
        subject="🚗 停車場可以預約了！",
        body=(
            f"{message}\n\n"
            f"請前往：https://pcc.youparking.com.tw/parkingreserve/#/reservedlist/1"
        ),
    )

# ─────────────────────────────────────────────
# 瀏覽器自動化核心
# ─────────────────────────────────────────────

def check_and_book() -> bool:
    """
    開啟停車預約頁面，執行以下步驟：
    1. 勾選同意條款
    2. 點擊「下一步」
    3. 偵測 TARGET_DATE 是否可預約
    4. 如果可以且 AUTO_BOOK=True，自動按下預約

    回傳 True 表示成功找到可預約日期並發出通知
    """
    log.info(f"開始檢查 {TARGET_DATE} 是否可預約...")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,   # 改成 False 可以看到瀏覽器畫面（debug 用）
            slow_mo=500,     # 操作間隔 ms，避免觸發反爬機制
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        try:
            # ── 步驟 1：開啟頁面，等待 JS 渲染完成 ──
            log.info("載入頁面...")
            page.goto(
                "https://pcc.youparking.com.tw/parkingreserve/#/reservedlist/1",
                wait_until="networkidle",
                timeout=30_000,
            )
            page.wait_for_timeout(2000)  # 額外等待 Vue 渲染

            # ── 步驟 2：勾選同意條款 ──
            log.info("尋找同意條款 checkbox...")
            agree_checkbox = _find_agree_checkbox(page)

            if agree_checkbox:
                if not agree_checkbox.is_checked():
                    agree_checkbox.click()
                    log.info("已勾選同意條款")
                    page.wait_for_timeout(500)
                else:
                    log.info("同意條款已勾選")
            else:
                log.warning("找不到同意條款 checkbox，可能頁面結構不同，嘗試繼續...")

            # ── 步驟 3：點擊「下一步」 ──
            log.info("尋找並點擊「下一步」按鈕...")
            next_btn = _find_next_button(page)

            if next_btn:
                next_btn.click()
                log.info("已點擊「下一步」")
                page.wait_for_timeout(2000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except PlaywrightTimeoutError:
                    pass  # SPA 不一定有網路請求，忽略 timeout
            else:
                log.warning("找不到「下一步」按鈕，嘗試直接偵測日期...")

            # ── 步驟 4：偵測目標日期是否可預約 ──
            log.info(f"偵測 {TARGET_DATE} 是否可選...")
            available, date_element = _check_date_available(page, TARGET_DATE)

            if available:
                log.info(f"✅ {TARGET_DATE} 可以預約！")
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                msg = (
                    f"【{TARGET_DATE} 停車位可以預約了！】\n"
                    f"偵測時間：{now_str}\n"
                    f"請立即前往預約！"
                )

                if AUTO_BOOK and date_element:
                    log.info("嘗試自動預約...")
                    booked = _auto_book(page, date_element)
                    if booked:
                        msg += "\n✅ 已自動完成預約！"
                    else:
                        msg += "\n⚠️ 自動預約失敗，請手動操作"

                send_notifications(msg)
                return True

            else:
                log.info(f"❌ {TARGET_DATE} 目前無法預約，下次再試")
                return False

        except PlaywrightTimeoutError as e:
            log.error(f"頁面操作逾時：{e}")
            return False
        except Exception as e:
            log.error(f"執行時發生例外：{e}", exc_info=True)
            return False
        finally:
            # 截圖留存（方便 debug）
            try:
                screenshot_path = f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                page.screenshot(path=screenshot_path)
                log.info(f"截圖已儲存：{screenshot_path}")
            except Exception:
                pass
            browser.close()


# ─────────────────────────────────────────────
# 元素定位輔助函式
# ─────────────────────────────────────────────

def _find_agree_checkbox(page):
    """
    嘗試多種 selector 找到同意條款的 checkbox。
    若失敗，請用 `playwright codegen <URL>` 錄製實際 selector 填入。
    """
    selectors = [
        "input[type='checkbox']",
        "input[type='checkbox'][name*='agree']",
        ".agree input",
        "label:has-text('同意') input",
        "label:has-text('我已閱讀') input",
        "label:has-text('條款') input",
        "[class*='agree'] input",
        "[class*='consent'] input",
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=2000):
                log.debug(f"checkbox selector 命中：{sel}")
                return el
        except Exception:
            continue
    return None


def _find_next_button(page):
    """
    嘗試多種 selector 找到「下一步」按鈕。
    """
    selectors = [
        "button:has-text('下一步')",
        "button:has-text('下一頁')",
        "button:has-text('確認')",
        "button:has-text('Next')",
        "a:has-text('下一步')",
        "[class*='next']",
        "[class*='submit']",
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=2000):
                log.debug(f"next button selector 命中：{sel}")
                return el
        except Exception:
            continue
    return None


def _check_date_available(page, target_date: str):
    """
    偵測目標日期是否可以點擊（非 disabled）。
    回傳 (is_available: bool, element)
    """
    selectors = [
        f"td:has-text('{target_date}')",
        f"div:has-text('{target_date}')",
        f"span:has-text('{target_date}')",
        f"button:has-text('{target_date}')",
        f"[class*='date']:has-text('{target_date}')",
        f"[class*='day']:has-text('{target_date}')",
        f"[class*='calendar']:has-text('{target_date}')",
    ]

    for sel in selectors:
        try:
            elements = page.locator(sel)
            if elements.count() == 0:
                continue

            el = elements.first
            is_disabled = (
                el.get_attribute("disabled") is not None
                or el.evaluate("el => el.classList.contains('disabled')")
                or el.evaluate("el => el.classList.contains('unavailable')")
                or el.evaluate("el => el.classList.contains('sold-out')")
                or el.evaluate("el => el.classList.contains('full')")
            )

            if not is_disabled:
                log.debug(f"日期 selector 命中：{sel}，狀態：可預約")
                return True, el
            else:
                log.debug(f"日期 selector 命中：{sel}，狀態：已滿/不可用")
                return False, el

        except Exception as e:
            log.debug(f"selector {sel} 例外：{e}")
            continue

    log.warning(f"找不到 {target_date} 的日期元素，可能頁面結構不同")
    return False, None


def _auto_book(page, date_element) -> bool:
    """
    自動點擊日期並完成預約流程。
    ⚠️ 高度依賴頁面實際結構，建議先用 headless=False 測試確認。
    """
    try:
        date_element.click()
        page.wait_for_timeout(1500)

        confirm_selectors = [
            "button:has-text('預約')",
            "button:has-text('確認預約')",
            "button:has-text('送出')",
            "button:has-text('提交')",
            "button[type='submit']",
        ]
        for sel in confirm_selectors:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible(timeout=2000):
                    btn.click()
                    page.wait_for_timeout(2000)
                    log.info("已點擊預約確認按鈕")
                    return True
            except Exception:
                continue

        log.warning("找不到預約確認按鈕，自動預約未完成")
        return False

    except Exception as e:
        log.error(f"自動預約時發生例外：{e}")
        return False


# ─────────────────────────────────────────────
# 排程器
# ─────────────────────────────────────────────

# 通知後停止排程，避免重複發通知
_notified = False


def scheduled_job():
    global _notified
    if _notified:
        log.info("已發送過通知，跳過本次檢查（重啟程式可重置）")
        return

    success = check_and_book()
    if success:
        _notified = True
        log.info("通知已發送，後續將不再重複通知")


def main():
    log.info("=" * 50)
    log.info("停車場預約 Agent 啟動")
    log.info(f"目標日期：{TARGET_DATE}")
    log.info(f"檢查間隔：每 {CHECK_INTERVAL_MINUTES} 分鐘")
    log.info(f"自動預約：{'開啟' if AUTO_BOOK else '關閉'}")
    log.info("=" * 50)

    # 啟動時先執行一次
    scheduled_job()

    if _notified:
        log.info("啟動時即偵測到可預約，程式結束")
        return

    scheduler = BlockingScheduler(timezone="Asia/Taipei")
    scheduler.add_job(
        scheduled_job,
        trigger="interval",
        minutes=CHECK_INTERVAL_MINUTES,
        next_run_time=datetime.now(),
    )

    log.info("排程器已啟動，按 Ctrl+C 結束")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        log.info("使用者中止，程式結束")


if __name__ == "__main__":
    main()
