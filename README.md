# Alpha Oracle Pro v15.5

加密貨幣自動化訊號機器人，跑在 GitHub Actions + Telegram 通知。

## ✨ v15.5 重點：即時 Tick 監控

**v15.4 致命問題**：SL/TP 必須等 K 線資料才檢查，加上 cron 5 分鐘跑一次，等於有 4 分鐘 dead time，價格早就破 SL 還在「等待 TP1」。

**v15.5 修法**：
1. `quick_tick_check`：純看當前 price vs SL/TP，不等 K 線。SL 一觸到立刻觸發
2. 每次 `run_scan` 入口先做 tick check（最高優先）
3. `intensive_monitor` 從 10 秒改成 3 秒一輪
4. 配套 `websocket_monitor.py`：在本機跑可達 **< 1 秒延遲**

## 📦 三階段訊號系統（v15.4 沿用）

```
階段 1 「📡 Setup 形成」  score ≥ 55，提早 30~60 分通知
階段 2 「⚠️ 進場區接近」  距 OB/FVG < 0.3%，準備限價單
階段 3 「🟢 Trigger 觸發」  score ≥ 72，自動進場
```

## 🚀 快速開始

### 1. 部署到 GitHub（cron 模式）

```bash
git clone <your-repo>
cd alpha-oracle-bot

# 在 GitHub Repo Settings → Secrets 加入：
# - TG_TOKEN（Telegram Bot Token）
# - CHAT_ID（你的 Chat ID）

# Push 後 GitHub Actions 會自動每分鐘跑
git push origin main
```

### 2. 本機加跑 WebSocket（真即時，推薦）

```bash
# Mac/Linux 上
git clone <your-repo>
cd alpha-oracle-bot
pip install -r requirements.txt  # 或 pip install requests websockets tradingview-ta

export TG_TOKEN=xxx
export CHAT_ID=xxx

# 持續跑（建議用 tmux/screen/launchd 開機自動執行）
python3 websocket_monitor.py
```

### 3. 手動指令（任何時候）

```bash
python3 main.py stats      # 持倉狀態
python3 main.py daily      # 今日報表
python3 main.py monthly    # 月報
python3 main.py learning   # 學習統計
python3 main.py tick       # 純 tick check（最快，給高頻 cron）
python3 main.py monitor    # 只跑監控不掃新訊號
```

## 📋 9 大評分指標

| 指標 | 分數 | 性質 |
|---|---:|---|
| 趨勢（Supertrend） | 30 | 確認型 |
| RSI 位置 | 25 | 確認型 |
| 訂單塊 OB | 20 | 前瞻型 |
| 公允價值缺口 FVG | 15 | 前瞻型 |
| 支撐阻力 SNR | 5 | 前瞻型 |
| Price Action | 5 | 前瞻型 |
| 流動性掃蕩 | 5 | 前瞻型 |
| 動能比 | 5 | 確認型 |
| MTF 共振 | ±15 | 確認型 |
| 量能 | ±8 | 確認型 |
| EMA 排列 | ±5 | 確認型 |

合格門檻 72 分，A+ 90+，A 80~89，B+ 70~79。

## 🛡️ 8 大風控

1. **score_threshold** 評分門檻
2. **min_rr_ratio** R:R 最低 1.5
3. **cooldown_hours** coin+side 冷卻（v15.4 改進）
4. **circuit_breaker** 連 3 敗熔斷 24h
5. **atr_max_pct** 波動過濾 3.5%
6. **blackout_windows** 風險時段（資金費率/FOMC）
7. **max_concurrent_positions** 同時持倉 ≤ 2
8. **daily_loss_limit_usd** 當日損失 ≥ $50 紅線

## 🧠 機器學習

- **KNN 找最相似 10 筆歷史交易**：低勝率組合自動降權
- **桶統計**：按分數/RSI/資金費率/幣種/方向累積勝率
- 累積越多資料越精準（min 5 筆同類）

## 🔍 覆盤（每次出場自動發）

6 大主因分析：趨勢反轉、RSI 動能瓦解、流動性掃蕩、波動激增、反向動能、OB 跌破。
含教訓提示 + 同類歷史勝率統計。

## 📂 檔案結構

```
alpha-oracle-bot/
├── .github/workflows/
│   └── alpha_oracle.yml      # GitHub Actions 每分鐘 cron
├── .gitignore
├── README.md
├── config.json               # 設定檔（會 merge 進 DEFAULT_CONFIG）
├── main.py                   # 主程式（v15.5）
├── websocket_monitor.py      # 即時 WebSocket 監控（本機跑）
├── backtest.py               # 回測腳本
│
├── active_signals.json       # 活躍持倉（runtime）
├── setups.json               # Setup watch（runtime）
├── signal_cooldown.json      # 冷卻記錄（runtime）
├── system_state.json         # 系統狀態（runtime）
├── learning_state.json       # 學習資料（runtime）
└── trade_history.json        # 交易歷史（runtime）
```

## ⚡ 延遲對比

| 模式 | SL 觸發延遲 | 何時用 |
|---|---|---|
| v15.4 (K 線 only) | 0~10 分鐘 ❌ | 已淘汰 |
| v15.5 cron + tick check | 0~75 秒 ✅ | 預設 |
| v15.5 + WebSocket | **< 1 秒** 🚀 | 本機掛機 |

## 🐛 常見問題

**Q: SL 早該觸發但沒平倉？**
A: v15.5 已修。請確認 main.py 是最新版，且 GitHub Actions deployment 是綠色 ✅。

**Q: 訊號太少 / 太少？**
A: 調整 `score_threshold`（預設 72）。降到 65 訊號變多但勝率可能降。

**Q: 想要更早收到通知？**
A: 啟用 `setup_watch`（預設已開），會在 Trigger 前 30~60 分鐘先發 Setup 形成警告。

**Q: 不想被心跳訊息洗版？**
A: `config.json` 改 `"heartbeat": { "enabled": false }`。

## 📜 版本歷史

- **v15.5** 即時 tick check + WebSocket 監控
- **v15.4** 三階段訊號 + 逆勢模式 + coin+side 冷卻
- **v15.3** record_trade 重複 bug 修復 + hard_filters 預設值統一
- **v15.2** TP 順序、即時價合併 K 線、TG 重試
- **v15.1** 雙來源價格驗證、KNN 學習

## License

私人使用。請勿散布。
