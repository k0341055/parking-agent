# 停車場預約偵測 Agent

自動偵測 [pcc.youparking.com.tw](https://pcc.youparking.com.tw/parkingreserve/#/) 的指定日期是否開放預約，  
一旦出現可預約按鈕，立即自動填單送出，並透過 **LINE** 和 **Gmail** 雙重通知。

本機不需要一直開著，完全透過免費雲端服務運作。

---

## 專案架構圖

```mermaid
flowchart TD
    subgraph TRIGGER["⏰ 觸發層（cron-job.org）"]
        CRON["🕐 排程：*/5 * * * *\n時區：Asia/Taipei\nPOST repository_dispatch\nbody: {event_type: trigger}"]
    end

    subgraph ACTIONS["⚙️ GitHub Actions（執行層）"]
        SECRETS["🔒 GitHub Secrets\nLINE_CHANNEL_ACCESS_TOKEN　LINE_USER_ID\nGMAIL_SENDER　GMAIL_PASSWORD　GMAIL_RECIPIENTS\nBOOKER_NAME　BOOKER_PLATE"]
        subgraph JOB["ubuntu-latest runner"]
            J1["① Checkout code"] --> J2["② Setup Python 3.11"]
            J2 --> J3["③ Cache Playwright chromium"]
            J3 --> J4["④ Install playwright + requests"]
            J4 --> J5["⑤ Run parking_agent_v2.py\nCHECK_ROUNDS=5"]
        end
        SECRETS -. "注入環境變數" .-> JOB
    end

    subgraph PROGRAM["🐍 程式執行層（5 輪 × ~60s = 實質每分鐘偵測）"]
        P1["🌐 開啟瀏覽器\n隨機 User-Agent / viewport"] --> GOTO{"頁面導航\n是否成功？"}
        GOTO -->|"逾時 / 重新導向"| NAVFAIL["⚠️ log warning\n關閉瀏覽器\n不通知"]
        NAVFAIL --> RETRY["⏳ 等 ~60s\n進入下一輪"]
        RETRY --> P1
        GOTO -->|"成功"| P2["☑️ 勾選同意條款"]
        P2 --> P3["▶️ 點擊前往預約"]
        P3 --> P4["🔍 找目標日期列"]
        P4 --> CHK{"狀態判斷"}
        CHK -->|"已滿"| RETRY
        CHK -->|"可預約"| N1["📣 通知 1\n停車位可以預約！\n自動填單進行中..."]
        N1 --> FORM["📝 填入：停放天數 / 姓名 / 車牌\n按送出 → submitted = True\n關閉跳出視窗"]
        FORM --> RES{"您已完成線上\n預約登記？"}
        RES -->|"否"| NG["⚠️ 通知 2\n請手動確認"]
        RES -->|"送出後逾時\nsubmitted=True"| POSTTIMEOUT["⚠️ 通知 2\n送出後逾時\n請手動確認"]
        RES -->|"是"| VERIFY["🔍 查詢預約記錄\n輸入車牌 → 查詢\n確認日期出現在結果中"]
        VERIFY -->|"找到記錄"| OK["✅ 通知 2\n預約成功！"]
        VERIFY -->|"找不到記錄"| NG2["⚠️ 通知 2\n表單顯示完成\n但記錄未找到\n請手動確認"]
    end

    subgraph NOTIFY["📬 通知層"]
        LINE["LINE Messaging API\nPush Message → LINE_USER_ID"]
        GMAIL["Gmail SMTP\n→ GMAIL_RECIPIENTS\n（逗號分隔，支援多人）"]
    end

    CRON -->|"每 5 分鐘\nrepository_dispatch"| ACTIONS
    J5 --> P1
    N1 --> NOTIFY
    OK --> NOTIFY
    NG --> NOTIFY
    NG2 --> NOTIFY
    POSTTIMEOUT --> NOTIFY
```

---

## 檔案結構

```
.
├── .github/
│   └── workflows/
│       └── parking.yml         # GitHub Actions 排程與執行設定
├── parking_book/
│   ├── parking_agent_v2.py     # 主程式（正式執行）
│   ├── parking_agent.ipynb     # 互動式偵錯 notebook
│   ├── parking_agent.py        # 舊版備用
│   └── .env.example            # 環境變數範本（本機測試用）
└── README.md
```

---

## 快速部署

### 1. Fork 此 repo

### 2. 修改目標日期與停放天數

編輯 [parking_book/parking_agent_v2.py](parking_book/parking_agent_v2.py)：

```python
TARGET_DATE  = "05-23"   # 頁面格式 "2026-05-23 (六)"，填月-日即可
PARKING_DAYS = int(os.environ.get("PARKING_DAYS", "5"))   # 預設 5 天
```

### 3. 設定 GitHub Secrets

**Settings → Secrets and variables → Actions → New repository secret**

| Secret 名稱 | 說明 |
|-------------|------|
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Messaging API Channel Access Token |
| `LINE_USER_ID` | 接收通知的 LINE User ID（U 開頭） |
| `GMAIL_SENDER` | 寄件 Gmail |
| `GMAIL_PASSWORD` | Gmail 應用程式密碼 |
| `GMAIL_RECIPIENTS` | 收件人，多人逗號分隔：`a@gmail.com,b@gmail.com` |
| `BOOKER_NAME` | 預約人姓名 |
| `BOOKER_PLATE` | 車牌號碼 |

### 4. 設定 cron-job.org（讓 GitHub Actions 每分鐘觸發）

> GitHub Actions 的 cron 最短只能每 5 分鐘且有延遲，改用 cron-job.org 主動呼叫更穩定。

1. 前往 [cron-job.org](https://cron-job.org) 免費註冊
2. 建立新工作，填入以下設定：

| 欄位 | 值 |
|------|-----|
| **URL** | `https://api.github.com/repos/你的帳號/parking-agent/dispatches` |
| **排程** | `*/5 * * * *` |
| **時區** | `Asia/Taipei` |
| **Request method** | `POST` |
| **Request body** | `{"event_type": "trigger"}` |
| **Headers** | `Authorization: Bearer <你的 GitHub PAT>` |
| **Headers** | `Content-Type: application/json` |

3. GitHub PAT 申請：**Settings → Developer settings → Personal access tokens → Tokens (classic)**  
   勾選 `repo` 和 `workflow` 權限

> ⚠️ PAT 只填在 cron-job.org 的設定頁面，**絕對不能寫進程式碼或 README**

### 5. 確認 Actions 已啟用

repo → **Actions** → 確認 workflow 已 Enable

---

## 本機偵錯

```bash
pip install playwright requests python-dotenv
playwright install chromium

cp parking_book/.env.example parking_book/.env
# 編輯 .env 填入真實值

cd parking_book
python parking_agent_v2.py
```

開啟 `parking_agent.ipynb` 可逐步執行，觀察瀏覽器畫面與每個 selector 的命中狀況。

---

## LINE Messaging API 申請

1. [LINE Developers Console](https://developers.line.biz/console/) → 建立 Messaging API Channel
2. Channel → Messaging API → Issue **Channel access token**（長期）
3. 掃 QR code 加入官方帳號，傳一則訊息
4. 頁面底部 **Your user ID** = `LINE_USER_ID`

> 免費方案每月 200 則 Push Message

---

## Gmail 應用程式密碼

1. Google 帳號 → 安全性 → 應用程式密碼（需先開啟兩步驟驗證）
2. 新增 → 取得 16 碼密碼 → 填入 `GMAIL_PASSWORD` Secret

---

## 逾時與錯誤處理

| 發生時機 | 行為 | 是否通知 |
|---------|------|---------|
| 頁面導航逾時 / 重新導向 | log warning，關閉瀏覽器，下一輪重試 | ❌ 不通知 |
| 填單過程逾時（送出前） | log warning，下一輪重試 | ❌ 不通知 |
| 送出後逾時（狀態不明） | log error，停止輪詢 | ✅ 通知（請手動確認） |
| 未偵測到「您已完成線上預約登記」| log warning，停止輪詢 | ✅ 通知（請手動確認） |
| 偵測到完成訊息 → 查詢記錄找到日期 | log info，停止輪詢 | ✅ 通知預約成功 |
| 偵測到完成訊息 → 查詢記錄找不到 | log warning，停止輪詢 | ✅ 通知（請手動確認） |

---

## 注意事項

- `parking_agent.ipynb` 含本機偵錯用明文設定，**請勿上傳 GitHub**
- `.env` 已在 `.gitignore`，不會被 git 追蹤
- cron-job.org 的 GitHub PAT **只放在 cron-job.org 設定頁面**，不放任何檔案
- GitHub Actions cron 排程有 5～30 分鐘延遲，這是使用 cron-job.org 觸發的原因
