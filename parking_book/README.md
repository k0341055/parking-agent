# 停車場預約 AI Agent

自動偵測 https://pcc.youparking.com.tw/parkingreserve/#/reservedlist/1
當指定日期（預設 5/23）可以預約時，透過 LINE Messaging API 或 Gmail 通知你。

> ⚠️ LINE Notify 已於 2025/3/31 終止服務，本專案改用 LINE Messaging API

---

## 快速開始

### 1. 安裝環境

```bash
pip install playwright apscheduler requests
playwright install chromium
```

### 2. 設定 `parking_agent.py` 頂部的設定區

```python
TARGET_DATE = "5/23"
CHECK_INTERVAL_MINUTES = 5

LINE_CHANNEL_ACCESS_TOKEN = "xxx"   # Messaging API Channel Access Token
LINE_USER_ID = "Uxxxxxxxx"          # 你自己的 LINE User ID

GMAIL_SENDER    = "xxx@gmail.com"
GMAIL_PASSWORD  = "xxxx xxxx"       # Gmail 應用程式密碼
GMAIL_RECIPIENT = "xxx@gmail.com"

AUTO_BOOK = False
```

### 3. 執行

```bash
python parking_agent.py
```

---

## 申請 LINE Messaging API（替代已終止的 LINE Notify）

1. 前往 https://developers.line.biz/console/ 登入
2. 建立 Provider（如果沒有的話）
3. 建立新 Channel → 選「Messaging API」
4. 進入 Channel → **Messaging API** 頁籤 → 拉到底部 → **Issue** Channel access token（長期）→ 複製
5. 用 LINE 掃 QR code 加入你的官方帳號（同頁面有 QR code）
6. 在同一頁面最下方找到 **「Your user ID」** → 這就是 `LINE_USER_ID`（格式：`U` + 32 碼英數字）
7. 將兩個值填入程式設定區

### 免費額度
每月 200 則 Push Message 免費（每 5 分鐘跑一次，一天最多 288 次，遠低於限制）

---

## 申請 Gmail 應用程式密碼

1. Google 帳號 → 安全性
2. 搜尋「應用程式密碼」（需先開啟兩步驟驗證）
3. 新增，名稱自定 → 取得 16 碼密碼 → 填入 `GMAIL_PASSWORD`

---

## Selector 找不到元素時

用錄製模式實際操作一遍，自動生成 selector：

```bash
playwright codegen https://pcc.youparking.com.tw/parkingreserve/#/reservedlist/1
```

把錄到的 selector 替換到 `_find_agree_checkbox`、`_find_next_button`、`_check_date_available` 裡。

debug 時改成有頭模式：

```python
browser = p.chromium.launch(headless=False)
```

每次執行都會存截圖（`screenshot_*.png`），可以看到程式停在哪一步。

---

## 部署到雲端（不用自己電腦一直開著）

建立 `.github/workflows/parking_check.yml`：

```yaml
name: Parking Agent
on:
  schedule:
    - cron: '*/10 1-15 * * *'   # UTC 時間，對應台灣 9am ~ 11pm，每 10 分鐘
  workflow_dispatch:             # 也可手動觸發

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install playwright apscheduler requests
      - run: playwright install --with-deps chromium
      - run: python parking_agent.py
        env:
          LINE_CHANNEL_ACCESS_TOKEN: ${{ secrets.LINE_CHANNEL_ACCESS_TOKEN }}
          LINE_USER_ID: ${{ secrets.LINE_USER_ID }}
          GMAIL_PASSWORD: ${{ secrets.GMAIL_PASSWORD }}
```

然後在 GitHub repo 的 **Settings → Secrets and variables → Actions** 加入對應的 Secrets。

程式讀取環境變數只需在設定區改為：

```python
import os
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")
```

---

## 檔案說明

| 檔案 | 說明 |
|------|------|
| `parking_agent.py` | 主程式 |
| `parking_agent.log` | 執行 log（自動產生） |
| `screenshot_*.png` | 每次執行截圖（自動產生，用於 debug） |
