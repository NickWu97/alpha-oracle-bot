#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro v14.7 — 精準價格偵測 + 防洗版（繁體中文）
══════════════════════════════════════════════════════════════════════
✨ v14.7 修復 / 新增：
  🪙 即時價合併進 K 線：OKX ticker tick 比 K 線快，融合後抓得更早
  🛡️ 持倉更新 15 分鐘 throttle：1 分鐘 cron 不再每分鐘洗版
     ↳ 修復 v14.6 之後 TP/SL 達標通知被持倉更新洗到上面去看不到的 bug
  🔄 send_tg 加重試：429 限速等 retry_after / 5xx 用 exponential backoff
     ↳ 訊息送達率從 ~95% → ~99.9%

✨ v14.6：
  ⚡ 主掃描改 1 分鐘 cron（合併 Pro + Monitor 成單一 job）
  🛡️ 早期退出：全部幣都冷卻 / 持倉時跳過重 API，只跑監控（5 秒搞定）
  🛡️ 嚴格每日風控三紅線：
     ① 同時持倉數上限（預設 2 個）
     ② 當日累計 PnL < -5% 停止開新單到隔天
     ③ 一天最多 6 筆訊號

✨ v14.5：
  💵 資金 / 槓桿 / 損益試算：依 $100 資金、$20 風險自動算槓桿與各 TP 美元損益
  🔍 覆盤訊息保證送達：資料不足 / 例外時也送 fallback，不再靜默失敗
  🧱 OB 失效退場現在也會送覆盤分析

✨ v14.4：
  🪙 VWAP 量加權均價：背後評分加 ±3（不顯示給用戶，避免訊息太亂）
  🛡️ 預設關閉「評分細項顯示」：進場通知更乾淨（show_score_breakdown=False）
  🔧 修復 TP 順序 bug：原 dynamic TP 校正會讓 TP3 ≤ TP2（DOGE/NEAR 案例）
  🎯 改成預設「固定 R:R」(1.5R/3R/5R)：可預期、永遠單調遞增
     ↳ 想用動態 TP 改 config: fixed_rr_mode=false（已修好 collapse bug）

✨ v14.3：
  🩺 健康監控：24h 沒送 TG / 連 5 次失敗 → 自動發警報（6h 不重複）
  🔬 指標審查（/audit）：神級 vs 一般、MTF 順勢 vs 中性、量能、市況、方向
     ↳ 用實際勝率驗證每個指標是否真的有效（✅⚠️❌ 三級判讀）
  📦 工作流合併：alpha_oracle.yml 一檔包 Pro + Monitor 兩個 Job

✨ v14.2（進階智能 5 項）：
  📊 訊號方向統計 + 自動偏向高勝率方向（/direction 命令）
  🎯 神級訊號特別標記：95+ 分用 🎯🎯🎯 醒目標題
  🪜 EMA 多週期排列：20/50/200 完美排列 +5、逆 200 -5
  🔥 過熱保護：某幣連 3 勝後暫停一輪避免過度依賴
  🧱 OB 失效退場：進場依據的訂單塊收盤被破 → 主動提前退場

✨ v14.1（高勝率精選 + 風控強化）：
  📊 進場通知顯示「為什麼 N 分」分數細項拆解
  💼 倉位大小建議：依分數推薦 0.5x / 1.0x / 1.5x
  ⏸️ 自動暫停爛幣：過去 7 天勝率 < 30% 自動暫停 24h
  ⚖️ R:R 最低門檻：TP1 < 1.5R 自動拒絕（避免動態 TP 校過頭）
  ❄️ 連敗冷靜期：連 2 敗後 30 分鐘不開新單

✨ v14.0（專業交易員養成）：
  🕒 多時框共振：1H + 4H Supertrend 確認，最高 +15 分（反向 -10）
  📊 量能確認：最後 K 量 vs 前 20 期均量，最高 +8 分（沒量 -10 直接淘汰）
  🌐 市場狀態識別：ADX 趨勢/震盪/過渡，震盪市門檻自動 +5
  🎯 動態 TP：固定 R 倍 TP 落在強 S/R 前方時自動校正
  📰 新聞事件過濾：NFP / CPI 自動規則 + 自訂事件清單
  🌀 進場時機：偵測回測影線 K 加 +3 分
  🧬 KNN 學習：每筆訊號向量化，找最相似 10 筆歷史交易看勝率
  📈 日報 / 月報：/daily 與 /monthly 命令，含各幣種績效、連勝連敗
  📁 backtest.py：獨立回測腳本（讀歷史 K 線重跑策略）
  📡 websocket_monitor.py：常駐 WS 監控（部署 Railway/Fly.io）

✨ v13.1 既有：
  ⚡ monitor 模式 + 高頻 cron workflow（30 秒一次）
  ⚡ monitor 模式：輕量、只追既有訊號，不生成新訊號
     ↳ 用法：python main.py monitor [polls] [interval]
     ↳ 搭配 alpha-oracle-monitor.yml 每 3 分鐘 cron + 一次 3 輪 = ~30 秒檢查一次
     ↳ TP/SL 通知延遲從 15 分鐘壓到 ~30 秒
  📁 新檔 alpha-oracle-monitor.yml：高頻監控專用 workflow

✨ v13.0（會自我成長）：
  🔍 覆盤分析：SL / BE / LOCK 後自動分析「為什麼結算」並送 Telegram
     ↳ 6 大歸因：趨勢反轉 / RSI 崩盤 / 流動性掃蕩 / 波動激增 / 反向動能 / OB 跌破
     ↳ 附「下次該怎麼判斷」+ 同類設定歷史勝率
  🧠 學習機制：每筆交易結算後更新桶（分數/RSI/資金費率/時段/幣種）
     ↳ 評分時自動套用調整：高勝率組合 +1~+2、低勝率組合 -2~-3，上限 ±10
     ↳ /learning 命令查看機器人學了什麼
  📈 12 種幣別：BTC/ETH/SOL/BNB/XRP/DOGE/ADA/AVAX/LINK/DOT/TON/NEAR
     ↳ 可在 config.json 的 coins 自訂

✨ v12.2 既有：
  📜 歷史 K 線補抓：抓 last_checked_ts 之後所有 K 線依序處理
  🔒 同幣種未平倉嚴格擋
  📦 fetch_candles_full：每輪共用 30 秒快取

✨ v12.1（平倉精度）：
  🪡 插針觸發：K 線高低點觸到平倉價即視為平倉
  🔁 TP/SL 順序處理：TP1 → TP2 → TP3 → SL（SL 用更新後的值）
  🔒 BE 保本顯示：到達 TP1 後若 SL 觸發，獨立顯示「🔒 保本出場」`0R`
  🔐 LOCK 鎖利顯示：到達 TP2 後若 SL 觸發，獨立顯示「🔐 鎖利出場」`+1.5R`
  🪡 通知標記插針觸發來源（K 線插針觸及目標價）

✨ v12.0 新增（高優先級風控）：
  🆕 TradingView 第二價格來源 → OKX/TV 偏離超過閾值自動跳過
  🆕 連續虧損熔斷：連 3 敗暫停 4h、連 5 敗硬熔斷 24h
  🆕 關鍵時段過濾：資金費率結算 / 美股開盤等高波動時段自動避開
  🆕 config.json 熱更新與驗證：無需重新部署即可調整參數
  🆕 系統狀態持久化（system_state.json）：熔斷狀態跨 Actions 不漏
  🆕 同幣種未平倉不重複開倉

✨ v11.0 既有重點：
  ✅ 修復所有 Markdown 鏈接化的語法錯誤
  ✅ 完整 SMC（OB）/ ICT（FVG、流動性掃蕩）/ SNR / 價格行為 / 盤口動能
  ✅ 評分 100 分制（趨勢30+RSI25+OB20+FVG15+SNR5+PA5+流動性5+動能5）
  ✅ 止盈倍率 1.5R / 3.0R / 5.0R
  ✅ 時間台灣 UTC+8 / 訊號冷卻持久化 / TP·SL 線層回覆
══════════════════════════════════════════════════════════════════════
"""
import requests
import os
import json
import logging
import time
import sys
import uuid
from datetime import datetime, timezone, timedelta


# ═════════════════════════════════════════════════════════
# 🇹🇼 台灣時間工具
# ═════════════════════════════════════════════════════════
TW_TZ = timezone(timedelta(hours=8))


def tw_now() -> datetime:
    """獲取台灣時間 datetime 物件"""
    return datetime.now(TW_TZ)


def tw_ts() -> str:
    """台灣時間時間戳字串（給通知顯示用）"""
    return tw_now().strftime("%Y-%m-%d %H:%M:%S 台灣時間")


# ═════════════════════════════════════════════════════════
# 🔧 環境變數安全解析
# ═════════════════════════════════════════════════════════
def _get_env(key: str, default: str = "") -> str:
    val = os.getenv(key)
    return val.strip() if val and val.strip() else default


def _get_env_int(key: str, default: int) -> int:
    val = os.getenv(key)
    try:
        return int(val.strip()) if val and val.strip() else default
    except Exception:
        return default


# ═════════════════════════════════════════════════════════
# 1. 基礎配置
# ═════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    stream=sys.stdout,
)

TG_TOKEN = _get_env("TG_TOKEN")
CHAT_ID = _get_env("CHAT_ID")

ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
    "BNB-USDT-SWAP", "XRP-USDT-SWAP", "DOGE-USDT-SWAP",
    "ADA-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP",
    "DOT-USDT-SWAP", "TON-USDT-SWAP", "NEAR-USDT-SWAP",
]
MAX_SIGNALS = _get_env_int("MAX_SIGNALS", 3)
SCORE_THRESHOLD = _get_env_int("SETUP_SCORE_THRESHOLD", 68)

SIGNAL_EXPIRE_HOURS = 24
COOLDOWN_HOURS = 2

ACTIVE_SIGNALS_FILE = "active_signals.json"
TRADE_HISTORY_FILE = "trade_history.json"
COOLDOWN_FILE = "signal_cooldown.json"
CONFIG_FILE = "config.json"
SYSTEM_STATE_FILE = "system_state.json"
LEARNING_FILE = "learning_state.json"

# 記憶體快取（同一輪執行內共用，跨輪不持久）
_price_cache: dict = {}

# ═════════════════════════════════════════════════════════
# 1.5 預設配置（config.json 不存在時的 fallback）
# ═════════════════════════════════════════════════════════
DEFAULT_CONFIG: dict = {
    "coins": ALL_COINS,                # 可在 config.json 自訂
    "max_signals": 3,
    "score_threshold": 68,
    "cooldown_hours": 2,
    "signal_expire_hours": 24,
    "atr_max_pct": 0.04,
    "post_mortem": {
        "enabled": True,
        "loss_only": False,            # False = SL/BE/LOCK 都做覆盤；True = 只 SL
    },
    "learning": {
        "enabled": True,
        "knn_enabled": True,           # 進階 KNN 學習（找最相似歷史交易）
        "min_samples": 5,
        "max_score_adjust": 10,
    },
    "news_blackouts": [
        # 用戶可自訂事件，例如：
        # {"start": "2026-05-07T20:30:00+08:00", "end": "2026-05-07T22:30:00+08:00", "reason": "FOMC 會議"}
    ],
    "auto_news_blackout": {
        "nfp": True,                   # 每月第一週五 21:25–22:30 (TW)
        "cpi": True,                   # 每月 10–16 日 21:25–22:30 (TW)
    },
    # ── v14.1 新增：高勝率篩選 + 風控強化 ──
    "show_score_breakdown": False,     # 進場通知顯示分數細項拆解（預設關閉，太雜亂）
    "fixed_rr_mode": True,             # 固定 1.5R/3R/5R（預設）；關閉改用動態 TP 校正
    # 💵 倉位 / 槓桿 / 損益試算
    "capital_management": {
        "enabled": True,
        "capital_per_trade_usd": 100,  # 標準單筆資金
        "max_loss_usd": 20,            # 最大可接受虧損（含手續費的緩衝）
        "max_leverage": 50,            # 上限槓桿（避免極短 SL 算出超高槓桿）
        "min_leverage": 2,             # 下限（避免極遠 SL 算出 0.x 槓桿）
    },
    # 🛡️ v14.6 嚴格風控：每日上限 + 同時持倉數
    "daily_limits": {
        "enabled": True,
        "max_concurrent_positions": 2,  # 同時最多 N 個倉位
        "daily_loss_limit_pct": 5.0,    # 當日累計 PnL < -N% 停止開新單
        "max_daily_signals": 6,         # 一天最多開 N 筆新訊號
    },
    "coin_auto_pause": {               # 自動暫停爛幣
        "enabled": True,
        "days": 7,                     # 過去 N 天
        "min_trades": 5,               # 至少 N 筆才判定
        "max_winrate": 0.30,           # 勝率低於此值就暫停
        "pause_hours": 24,
    },
    "position_sizing": {               # 倉位大小建議
        "enabled": True,
        "tiers": [
            {"min_score": 95, "multiplier": 1.5, "label": "🔥 強訊號加大倉"},
            {"min_score": 85, "multiplier": 1.0, "label": "標準倉"},
            {"min_score": 80, "multiplier": 0.5, "label": "謹慎小倉"},
            {"min_score": 0,  "multiplier": 0.5, "label": "標準倉"},
        ],
    },
    "min_rr_ratio": 1.5,               # TP1 至少要有 1.5R 才接，否則拒絕
    "cooling_off": {                   # 連敗冷靜期
        "enabled": True,
        "loss_threshold": 2,           # 連 N 敗後啟動冷靜
        "period_minutes": 30,          # 冷靜 N 分鐘
    },
    # ── v14.2 新增：方向偏好 / 神級訊號 / EMA / 過熱 / OB 失效 ──
    "direction_bias": {                # 自動偏好高勝率方向
        "enabled": True,
        "min_diff_pct": 15,            # LONG/SHORT 勝率差 ≥ N% 才啟動偏好
        "min_samples": 10,             # 至少各 N 筆才信
        "bonus": 3,                    # 順勢方向加分
        "penalty": 3,                  # 逆勢方向扣分
    },
    "god_signal": {                    # 神級訊號特別標記
        "enabled": True,
        "min_score": 95,
    },
    "ema_alignment": {                 # 多時框 EMA 排列
        "enabled": True,
        "periods": [20, 50, 200],
        "perfect_bonus": 5,            # 完美排列加分
        "partial_bonus": 3,            # 部分排列加分
        "against_penalty": 5,          # 逆 EMA200 扣分
    },
    "overheating": {                   # 過度集中保護
        "enabled": True,
        "win_streak_threshold": 3,     # 連 N 勝後啟動保護
        "cooldown_hours": 4,
    },
    "ob_invalidation": {               # OB / FVG 失效退場
        "enabled": True,
        "break_buffer_pct": 0.2,       # 收盤跌破 OB 邊緣 N% → 視為失效
    },           # ATR/Price 超過此值視為震盪過大
    "price_verification": {
        "enabled": True,
        "max_deviation_pct": 0.5,  # OKX 與 TradingView 偏離 > 0.5% 跳過
        "block_on_unverified": False,  # TV 抓不到時是否一律跳過（False=放行）
    },
    "circuit_breaker": {
        "enabled": True,
        "soft_threshold": 3,       # 連 3 敗 → 軟熔斷
        "soft_pause_hours": 4,
        "hard_threshold": 5,       # 連 5 敗 → 硬熔斷
        "hard_pause_hours": 24,
    },
    # 台灣時間時段（HH:MM），結束時間為「不含」
    "blackout_windows_tw": [
        {"start": "07:50", "end": "08:10", "reason": "資金費率結算（00 UTC）"},
        {"start": "15:50", "end": "16:10", "reason": "資金費率結算（08 UTC）"},
        {"start": "23:50", "end": "00:10", "reason": "資金費率結算（16 UTC）"},
        {"start": "21:25", "end": "21:45", "reason": "美股開盤波動"},
        {"start": "02:00", "end": "02:30", "reason": "FOMC 公布時段"},
    ],
}


# ═════════════════════════════════════════════════════════
# 2. 通知系統
# ═════════════════════════════════════════════════════════
def send_tg(
    msg: str,
    parse_mode: str = "Markdown",
    reply_markup: dict | None = None,
    reply_to_message_id: int | None = None,
    max_retries: int = 3,
) -> int | None:
    """📤 發送 Telegram 通知 → 回傳 message_id（失敗回 None）

    v14.7 加入重試：429 (rate limit) 等 retry_after，5xx 用 exponential backoff
    """
    if not TG_TOKEN or not CHAT_ID:
        logging.warning("⚠️ TG_TOKEN 或 CHAT_ID 未設定，略過發送")
        return None

    payload = {
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True

    last_err = ""
    for attempt in range(max_retries):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json=payload,
                timeout=8,
            )
            if r.status_code == 200:
                # 🩺 健康監控：紀錄最後一次成功送 TG 的時間
                try:
                    _state = _load_json(SYSTEM_STATE_FILE, {})
                    _state["last_tg_sent"] = time.time()
                    _save_json(SYSTEM_STATE_FILE, _state)
                except Exception:
                    pass
                return r.json().get("result", {}).get("message_id")

            # 429 = 限速，按 Telegram 給的 retry_after 等
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("parameters", {}).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                wait = min(wait + 0.5, 15)
                logging.warning(f"⏳ TG 429 限速，等 {wait:.1f}s 重試")
                time.sleep(wait)
                last_err = "429 rate limit"
                continue

            # 5xx = 伺服器錯，exponential backoff
            if r.status_code >= 500:
                wait = 2 ** attempt
                logging.warning(f"⏳ TG {r.status_code} 伺服器錯，{wait}s 後重試")
                time.sleep(wait)
                last_err = f"server {r.status_code}"
                continue

            # 4xx 其他錯不重試（通常是 payload 問題）
            logging.error(f"❌ TG API {r.status_code}: {r.text[:200]}")
            return None

        except Exception as e:
            last_err = str(e)
            wait = 2 ** attempt
            logging.warning(
                f"⏳ TG 發送失敗（嘗試 {attempt + 1}/{max_retries}）：{e}，{wait}s 後重試"
            )
            time.sleep(wait)

    logging.error(f"❌ TG 發送失敗（{max_retries} 次重試後仍失敗）：{last_err}")
    return None


def _order_keyboard(order_id: str) -> dict:
    """🔘 生成訂單查詢按鈕（LINE 風格）"""
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"🔍 查詢訂單 {order_id[-8:]}",
                    "callback_data": f"order_{order_id}",
                }
            ]
        ]
    }


# ═════════════════════════════════════════════════════════
# 2.5 v14.1 倉位建議 + 分數細項格式化
# ═════════════════════════════════════════════════════════
def suggest_position_size(score: int, cfg: dict | None = None) -> tuple[float, str]:
    """💰 根據分數推薦倉位倍數"""
    if cfg is None:
        cfg = load_config()
    ps_cfg = cfg.get("position_sizing", {})
    if not ps_cfg.get("enabled", True):
        return 1.0, "標準倉"
    tiers = ps_cfg.get("tiers", DEFAULT_CONFIG["position_sizing"]["tiers"])
    for tier in tiers:
        if score >= tier.get("min_score", 0):
            return tier.get("multiplier", 1.0), tier.get("label", "標準倉")
    return 1.0, "標準倉"


def calc_position_sizing(
    entry: float,
    sl: float,
    tp1: float,
    tp2: float,
    tp3: float,
    side: str,
    pos_multiplier: float = 1.0,
    cfg: dict | None = None,
) -> dict | None:
    """💵 根據資金 / 風險上限算槓桿、倉位、各 TP 美元損益

    邏輯：
      effective_capital = base_capital × pos_multiplier
      effective_max_loss = base_max_loss × pos_multiplier  （風險比例不變）
      leverage = (effective_max_loss / effective_capital) ÷ SL距離
              = max_loss / capital ÷ SL距離（與 multiplier 無關）
      position_value = effective_capital × leverage
    """
    if cfg is None:
        cfg = load_config()
    cm = cfg.get("capital_management", {})
    if not cm.get("enabled", True):
        return None

    base_cap = cm.get("capital_per_trade_usd", 100)
    base_max_loss = cm.get("max_loss_usd", 20)
    max_lev = cm.get("max_leverage", 50)
    min_lev = cm.get("min_leverage", 2)

    sl_dist_pct = abs(entry - sl) / entry
    if sl_dist_pct <= 0:
        return None

    # 風險比 = 損失 / 資金（不會被 multiplier 改變）
    risk_ratio = base_max_loss / base_cap
    required_lev = risk_ratio / sl_dist_pct
    leverage = max(min_lev, min(max_lev, round(required_lev)))

    # 依 multiplier 縮放實際資金與容忍損失
    capital = base_cap * pos_multiplier
    max_loss = base_max_loss * pos_multiplier

    position_value = capital * leverage
    contracts = position_value / entry

    def _pnl(target_price):
        if side == "LONG":
            return position_value * (target_price - entry) / entry
        return position_value * (entry - target_price) / entry

    return {
        "capital_usd": round(capital, 2),
        "max_loss_usd": round(max_loss, 2),
        "leverage": int(leverage),
        "position_value_usd": round(position_value, 2),
        "contracts": round(contracts, 4),
        "sl_loss_usd": round(abs(_pnl(sl)), 2),
        "tp1_profit_usd": round(_pnl(tp1), 2),
        "tp2_profit_usd": round(_pnl(tp2), 2),
        "tp3_profit_usd": round(_pnl(tp3), 2),
        "pos_multiplier": pos_multiplier,
    }


def _format_score_breakdown(detail: dict | None) -> str:
    """📊 產生「為什麼 N 分」的分數細項拆解"""
    if not detail:
        return ""
    lines = ["", "📊 *評分細項：*"]
    base_items = [
        ("🌊 趨勢", "trend", 30),
        ("📊 RSI", "rsi", 25),
        ("🧱 OB", "ob", 20),
        ("⚡ FVG", "fvg", 15),
        ("📏 SNR", "snr", 5),
        ("📊 PA", "pa", 5),
        ("💧 流動性", "liq", 5),
        ("📈 動能", "mom", 5),
    ]
    for name, key, max_v in base_items:
        v = detail.get(key, 0)
        lines.append(f"  {name}：`{v}/{max_v}`")

    if "mtf" in detail:
        mtf_v = detail["mtf"]
        mtf_desc = detail.get("mtf_desc", "")
        sign = "+" if mtf_v >= 0 else ""
        lines.append(f"  🕒 MTF（{mtf_desc}）：`{sign}{mtf_v}`")
    if "volume" in detail:
        vol_v = detail["volume"]
        vol_r = detail.get("volume_ratio", 0)
        sign = "+" if vol_v >= 0 else ""
        lines.append(f"  📊 量能（{vol_r}×）：`{sign}{vol_v}`")
    if detail.get("pullback"):
        lines.append(f"  🌀 回測進場：`+3`")
    if "regime" in detail:
        regime_zh = {"trend": "趨勢市", "range": "震盪市", "transitional": "過渡"}.get(
            detail["regime"], detail["regime"]
        )
        lines.append(f"  🌐 市場狀態：{regime_zh}（ADX `{detail.get('adx', 0)}`）")
    if "learning_adjust" in detail and detail["learning_adjust"] != 0:
        adj = detail["learning_adjust"]
        sign = "+" if adj >= 0 else ""
        lines.append(f"  🧬 學習調整：`{sign}{adj}`")
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════
# 3. 通知格式
# ═════════════════════════════════════════════════════════
def _fmt_entry(
    coin: str,
    side: str,
    order_id: str,
    price: float,
    entry: float,
    sl: float,
    tp1: float,
    tp2: float,
    tp3: float,
    score: int,
    funding_rate: float | None = None,
    detail: dict | None = None,
) -> str:
    """📌 進場通知（含分數細項 + 倉位建議）"""
    direction = "做多" if side == "LONG" else "做空"
    emoji = "🟢" if side == "LONG" else "🔴"

    # 🎯 神級訊號特別標記
    cfg_god = load_config().get("god_signal", {})
    god_threshold = cfg_god.get("min_score", 95)
    is_god = cfg_god.get("enabled", True) and score >= god_threshold

    if is_god:
        grade = "🎯🎯🎯 *神級訊號* 🎯🎯🎯"
    elif score >= 85:
        grade = "🔥 A+ 極強"
    elif score >= 70:
        grade = "⭐ A 強力"
    else:
        grade = "✅ B+ 合格"

    tp1_pct = (tp1 - entry) / entry * 100 * (1 if side == "LONG" else -1)
    tp2_pct = (tp2 - entry) / entry * 100 * (1 if side == "LONG" else -1)
    tp3_pct = (tp3 - entry) / entry * 100 * (1 if side == "LONG" else -1)
    sl_pct = (sl - entry) / entry * 100  # 帶正負號

    funding_line = ""
    if funding_rate is not None:
        funding_line = f"💰 資金費率：`{funding_rate * 100:+.4f}%`\n"

    # 倉位建議
    cfg = load_config()
    pos_mult, pos_label = suggest_position_size(score, cfg)
    pos_line = f"💼 建議倉位：`{pos_mult}x` ({pos_label})\n"

    # 💵 倉位 / 槓桿 / 損益試算
    sizing = calc_position_sizing(entry, sl, tp1, tp2, tp3, side, pos_mult, cfg)
    sizing_block = ""
    if sizing:
        sizing_block = (
            f"\n"
            f"💵 *資金試算（資金 `${sizing['capital_usd']}` / 風險 `${sizing['max_loss_usd']}`）*\n"
            f"  槓桿：`{sizing['leverage']}x`\n"
            f"  名目倉位：`${sizing['position_value_usd']:,.0f}`\n"
            f"  數量：`{sizing['contracts']:,.4f} {coin}`\n"
            f"  🛑 止損損失：`-${sizing['sl_loss_usd']:.2f}`\n"
            f"  🥇 TP1 獲利：`+${sizing['tp1_profit_usd']:.2f}`\n"
            f"  🥈 TP2 獲利：`+${sizing['tp2_profit_usd']:.2f}`\n"
            f"  🏆 TP3 獲利：`+${sizing['tp3_profit_usd']:.2f}`\n"
        )

    # R:R 顯示（TP1 實際倍數）
    risk = abs(entry - sl)
    tp1_r = abs(tp1 - entry) / risk if risk > 0 else 0
    tp2_r = abs(tp2 - entry) / risk if risk > 0 else 0
    tp3_r = abs(tp3 - entry) / risk if risk > 0 else 0

    # 分數細項（預設關閉，太雜亂）
    breakdown = ""
    if cfg.get("show_score_breakdown", False):
        breakdown = _format_score_breakdown(detail)

    return (
        f"{emoji} *{coin} 進場提醒* {grade}\n"
        f"━━━━━━━━━━━━━━\n"
        f"🆔 訂單編號：`{order_id}`\n"
        f"⏰ 時間：{tw_ts()}\n"
        f"方向：{direction}\n"
        f"進場價：`{entry:.4f}`\n"
        f"當前價：`{price:.4f}`\n"
        f"評分：*{score} 分*\n"
        f"{pos_line}"
        f"{funding_line}"
        f"{sizing_block}"
        f"{breakdown}\n"
        f"\n"
        f"🎯 止盈目標：\n"
        f"  TP1 `{tp1:.4f}` ({tp1_pct:+.2f}% / `{tp1_r:.1f}R`)\n"
        f"  TP2 `{tp2:.4f}` ({tp2_pct:+.2f}% / `{tp2_r:.1f}R`)\n"
        f"  TP3 `{tp3:.4f}` ({tp3_pct:+.2f}% / `{tp3_r:.1f}R`)\n"
        f"\n"
        f"🛑 止損：`{sl:.4f}` ({sl_pct:+.2f}%)\n"
        f"\n"
        f"💡 到達 TP1 自動保本，到達 TP2 自動鎖利至 TP1"
    )


def _fmt_tp(
    coin: str,
    side: str,
    order_id: str,
    tp_level: str,
    price: float,
    pnl_pct: float,
    r_mult: float,
    wick_triggered: bool = False,
) -> str:
    """🎯 止盈通知"""
    direction = "做多" if side == "LONG" else "做空"
    advice = (
        "建議平倉 ⅓ 鎖定獲利"
        if tp_level == "TP1"
        else "建議再平倉 ⅓ 落袋為安"
        if tp_level == "TP2"
        else "建議全部平倉，完美收割 🏆"
    )
    wick_note = "\n🪡 _插針觸發（K 線插針觸及目標價）_" if wick_triggered else ""
    return (
        f"🎯 *{coin} {tp_level} 達標！*\n"
        f"━━━━━━━━━━━━━━\n"
        f"🆔 訂單編號：`{order_id}`\n"
        f"⏰ 時間：{tw_ts()}\n"
        f"方向：{direction}\n"
        f"觸發價：`{price:.4f}`{wick_note}\n"
        f"獲利：`{pnl_pct:+.2f}%` (`{r_mult:+.1f}R`)\n"
        f"\n"
        f"✅ 已達成 {tp_level}\n"
        f"\n"
        f"💡 {advice}"
    )


def _fmt_sl(
    coin: str,
    side: str,
    order_id: str,
    price: float,
    pnl_pct: float,
    mode: str = "LOSS",
    r_value: float = -1.0,
    wick_triggered: bool = False,
) -> str:
    """🛑 平倉通知（三模式：LOSS 止損 / BE 保本 / LOCK 鎖利）"""
    direction = "做多" if side == "LONG" else "做空"
    if mode == "BE":
        label = "🔒 保本出場"
        r_tag = "`0.0R`"
        advice = (
            "✨ TP1 已達成，止損上移至進場價\n"
            "本筆無損出場，資金完整保留\n"
            "💡 等待下一個高勝率訊號 💪"
        )
    elif mode == "LOCK":
        label = "🔐 鎖利出場"
        r_tag = f"`+{r_value:.1f}R`"
        advice = (
            "🎉 TP2 已達成，止損上移至 TP1\n"
            "趨勢回頭時鎖住 TP1 的獲利優雅退場\n"
            "💡 風控完美執行，繼續保持 ✨"
        )
    else:
        label = "❌ 止損離場"
        r_tag = "`-1.0R`"
        advice = "💡 遵守風控，勿加碼攤平。下一筆訊號會更好 🚀"

    wick_note = "\n🪡 _插針觸發（K 線插針觸及平倉價）_" if wick_triggered else ""
    return (
        f"{label} *{coin}*\n"
        f"━━━━━━━━━━━━━━\n"
        f"🆔 訂單編號：`{order_id}`\n"
        f"⏰ 時間：{tw_ts()}\n"
        f"方向：{direction}\n"
        f"觸發價：`{price:.4f}`{wick_note}\n"
        f"結果：`{pnl_pct:+.2f}%` {r_tag}\n"
        f"\n"
        f"{advice}"
    )


def _fmt_position(sig: dict, current_price: float) -> str:
    """📊 持倉進度更新"""
    coin = sig["instId"].split("-")[0]
    side = sig["side"]
    direction = "做多" if side == "LONG" else "做空"
    entry = sig["entry"]
    pnl = (
        (current_price - entry) / entry * 100
        if side == "LONG"
        else (entry - current_price) / entry * 100
    )
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"

    if sig.get("hit_tp3"):
        progress = "🏆 TP3 ✅"
    elif sig.get("hit_tp2"):
        progress = "🥇✅ → 🥈✅ → ⏳ TP3"
    elif sig.get("hit_tp1"):
        progress = "🥇✅ → ⏳ TP2"
    else:
        progress = "⏳ 等待 TP1"

    return (
        f"📊 *{coin} 持倉更新*\n"
        f"━━━━━━━━━━━━━━\n"
        f"🆔 訂單編號：`{sig.get('order_id', 'N/A')}`\n"
        f"⏰ 時間：{tw_ts()}\n"
        f"方向：{direction}\n"
        f"當前：`{current_price:.4f}` {pnl_emoji}{pnl:+.2f}%\n"
        f"進場：`{entry:.4f}`\n"
        f"\n"
        f"🎯 止盈進度：{progress}\n"
        f"  TP1 `{sig['tp1']:.4f}`{'✅' if sig.get('hit_tp1') else ''}\n"
        f"  TP2 `{sig['tp2']:.4f}`{'✅' if sig.get('hit_tp2') else ''}\n"
        f"  TP3 `{sig['tp3']:.4f}`{'✅' if sig.get('hit_tp3') else ''}\n"
        f"\n"
        f"🛑 止損：`{sig['sl']:.4f}`"
    )


# ═════════════════════════════════════════════════════════
# 4. 數據抓取
# ═════════════════════════════════════════════════════════
def fetch_price(instId: str) -> float:
    """🔍 即時價格（5 秒記憶體快取）"""
    now = time.time()
    if instId in _price_cache:
        price, t = _price_cache[instId]
        if now - t < 5:
            return price
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/market/ticker?instId={instId}",
            timeout=5,
        ).json()
        if res.get("code") == "0" and res.get("data"):
            price = float(res["data"][0]["last"])
            if price > 0:
                _price_cache[instId] = (price, now)
                return price
    except Exception as e:
        logging.warning(f"⚠️ 取得 {instId} 價格失敗：{e}")
    return _price_cache.get(instId, (0.0, 0))[0]


def fetch_candles(instId: str, tf: str = "15m", limit: int = 100) -> list | None:
    """📊 K 線（已收線）"""
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}",
            timeout=6,
        ).json()
        if res.get("code") != "0":
            return None
        data = res.get("data", [])
        if len(data) < 30:
            return None
        # OKX 第 9 欄（index 8）為 confirm，僅取已收線；OKX 預設由新到舊，反轉成由舊到新
        confirmed = [r for r in data if r[8] == "1"][::-1]
        return [
            {
                "ts": r[0],
                "o": float(r[1]),
                "h": float(r[2]),
                "l": float(r[3]),
                "c": float(r[4]),
                "v": float(r[5]),
            }
            for r in confirmed
        ]
    except Exception as e:
        logging.warning(f"⚠️ 取得 {instId} K 線失敗：{e}")
        return None


_candle_full_cache: dict = {}


def fetch_candles_full(instId: str, tf: str = "15m", limit: int = 100) -> list:
    """🪡 抓最近 N 根 K 線（含未收線）並按時間升序排序，每輪掃描共用 30 秒快取

    回傳每筆含：ts(ms 整數)、o/h/l/c/v、confirmed(bool)
    用於 _check_one 的「歷史插針補抓」：
      - 訊號自 last_checked_ts 之後的所有 K 線都會被掃過
      - 即使 cron 漏跑、訊號開了 3 小時才檢查，過去任何插針都不會漏
    """
    now = time.time()
    if instId in _candle_full_cache:
        candles, t = _candle_full_cache[instId]
        if now - t < 30:
            return candles
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}",
            timeout=8,
        ).json()
        if res.get("code") != "0":
            return _candle_full_cache.get(instId, ([], 0))[0]
        data = res.get("data", [])
        candles = [
            {
                "ts": int(r[0]),
                "o": float(r[1]),
                "h": float(r[2]),
                "l": float(r[3]),
                "c": float(r[4]),
                "v": float(r[5]),
                "confirmed": r[8] == "1",
            }
            for r in data
        ]
        candles.sort(key=lambda x: x["ts"])
        _candle_full_cache[instId] = (candles, now)
        return candles
    except Exception as e:
        logging.warning(f"⚠️ 取得 {instId} 完整 K 線失敗：{e}")
        return _candle_full_cache.get(instId, ([], 0))[0]


def fetch_recent_range(instId: str, bars: int = 2, tf: str = "15m") -> tuple[float, float] | None:
    """🪡 抓最近 N 根 K 線（含未收線）的最低 / 最高 → (low, high)

    用途：偵測插針，避免「快速戳到 SL/TP 又縮回去」逃過追蹤。
    與 fetch_candles 不同，這裡不過濾 confirm，把正在形成的 K 線也算進去。
    """
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={bars}",
            timeout=5,
        ).json()
        if res.get("code") != "0":
            return None
        data = res.get("data", [])
        if not data:
            return None
        lows = [float(r[3]) for r in data]
        highs = [float(r[2]) for r in data]
        return min(lows), max(highs)
    except Exception as e:
        logging.warning(f"⚠️ 取得 {instId} 最近區間失敗：{e}")
        return None


def fetch_funding_rate(instId: str) -> float | None:
    """💰 OKX 資金費率（永續合約）"""
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}",
            timeout=5,
        ).json()
        if res.get("code") == "0" and res.get("data"):
            return float(res["data"][0]["fundingRate"])
    except Exception as e:
        logging.warning(f"⚠️ 取得 {instId} 資金費率失敗：{e}")
    return None


# ═════════════════════════════════════════════════════════
# 4.5 TradingView 第二價格來源（風控）
# ═════════════════════════════════════════════════════════
_tv_cache: dict = {}


def fetch_price_tv(instId: str) -> float | None:
    """📡 從 TradingView 抓取即時價格（OKX 永續合約）

    回傳 None 代表抓不到（網路 / 套件未安裝 / 符號錯誤）
    """
    now = time.time()
    if instId in _tv_cache:
        price, t = _tv_cache[instId]
        if now - t < 10:
            return price

    try:
        # 套件可能未安裝（純語法檢查或本地測試）
        from tradingview_ta import TA_Handler, Interval  # type: ignore
    except ImportError:
        logging.warning("⚠️ 未安裝 tradingview_ta，跳過 TV 驗證")
        return None

    try:
        # BTC-USDT-SWAP → BTCUSDT.P（OKX 永續合約在 TradingView 的命名）
        symbol = instId.replace("-USDT-SWAP", "USDT.P").replace("-", "")
        handler = TA_Handler(
            symbol=symbol,
            exchange="OKX",
            screener="crypto",
            interval=Interval.INTERVAL_1_MINUTE,
            timeout=8,
        )
        analysis = handler.get_analysis()
        price = float(analysis.indicators.get("close", 0) or 0)
        if price > 0:
            _tv_cache[instId] = (price, now)
            return price
    except Exception as e:
        logging.warning(f"⚠️ TradingView 取得 {instId} 失敗：{e}")
    return None


def verify_price(
    instId: str,
    okx_price: float,
    max_dev_pct: float = 0.5,
    block_on_unverified: bool = False,
) -> tuple[bool, float | None, float]:
    """⚖️ 雙來源價格驗證 → (是否通過, TV 價格, 偏離百分比)

    block_on_unverified:
      True  → TV 抓不到也擋訊號（保守）
      False → TV 抓不到當作通過（避免單點失效擋掉所有訊號）
    """
    tv_price = fetch_price_tv(instId)
    if tv_price is None:
        return (not block_on_unverified, None, 0.0)
    diff_pct = abs(okx_price - tv_price) / okx_price * 100
    if diff_pct > max_dev_pct:
        logging.warning(
            f"🚨 {instId} 價格不一致：OKX={okx_price:.4f} TV={tv_price:.4f} "
            f"diff={diff_pct:.3f}% > {max_dev_pct}%"
        )
        return (False, tv_price, diff_pct)
    logging.info(
        f"✅ {instId} 價格驗證通過：OKX={okx_price:.4f} TV={tv_price:.4f} "
        f"diff={diff_pct:.3f}%"
    )
    return (True, tv_price, diff_pct)


# ═════════════════════════════════════════════════════════
# 5. 基礎技術指標
# ═════════════════════════════════════════════════════════
def calc_atr(df: list, period: int = 14) -> float:
    """ATR（簡化均值版本）"""
    if len(df) < period + 1:
        return 0.001
    trs = []
    for i in range(1, len(df)):
        hl = df[i]["h"] - df[i]["l"]
        hc = abs(df[i]["h"] - df[i - 1]["c"])
        lc = abs(df[i]["l"] - df[i - 1]["c"])
        trs.append(max(hl, hc, lc))
    if len(trs) < period:
        return 0.001
    atr = sum(trs[-period:]) / period
    return atr if atr > 0 else 0.001


def calc_supertrend(df: list, period: int = 10, mult: float = 3.0) -> int:
    """趨勢方向：1=多頭 / -1=空頭 / 0=震盪（簡化版本）"""
    if len(df) < period + 2:
        return 0
    atr = calc_atr(df, period)
    mid = sum(r["c"] for r in df[-20:]) / 20
    cur = df[-1]["c"]
    band = atr * 0.5
    if cur > mid + band:
        return 1
    if cur < mid - band:
        return -1
    return 0


def calc_rsi(df: list, period: int = 14) -> float:
    """RSI（Wilder 簡化版）"""
    if len(df) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(df)):
        ch = df[i]["c"] - df[i - 1]["c"]
        gains.append(ch if ch > 0 else 0)
        losses.append(-ch if ch < 0 else 0)
    if len(gains) < period:
        return 50.0
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))


# ═════════════════════════════════════════════════════════
# 6. SMC / ICT / SNR / 價格行為 / 流動性 / 動能
# ═════════════════════════════════════════════════════════
def find_order_block(df: list, side: str, lookback: int = 30) -> dict | None:
    """🧱 訂單塊（OB）

    看漲 OB：最近的陰線後緊接陽線突破其高點。
    看跌 OB：最近的陽線後緊接陰線跌破其低點。
    """
    n = len(df)
    if n < lookback + 5:
        return None
    start = max(0, n - lookback)
    if side == "LONG":
        for i in range(n - 4, start, -1):
            if df[i]["c"] < df[i]["o"]:  # 陰線
                for j in range(i + 1, min(i + 4, n)):
                    if df[j]["c"] > df[i]["h"]:
                        return {"low": df[i]["l"], "high": df[i]["h"]}
    else:
        for i in range(n - 4, start, -1):
            if df[i]["c"] > df[i]["o"]:  # 陽線
                for j in range(i + 1, min(i + 4, n)):
                    if df[j]["c"] < df[i]["l"]:
                        return {"low": df[i]["l"], "high": df[i]["h"]}
    return None


def find_fvg(df: list, side: str, lookback: int = 30) -> dict | None:
    """⚡ 公允價值缺口（FVG）

    看漲 FVG：K[i].low > K[i-2].high。
    看跌 FVG：K[i].high < K[i-2].low。
    """
    n = len(df)
    if n < 4:
        return None
    start = max(2, n - lookback)
    for i in range(n - 1, start, -1):
        if side == "LONG":
            if df[i]["l"] > df[i - 2]["h"]:
                return {"low": df[i - 2]["h"], "high": df[i]["l"]}
        else:
            if df[i]["h"] < df[i - 2]["l"]:
                return {"low": df[i]["h"], "high": df[i - 2]["l"]}
    return None


def calc_snr(df: list, lookback: int = 100) -> tuple[float, float]:
    """📏 動態支撐 / 阻力（近 N 根極值）"""
    seg = df[-lookback:] if len(df) >= lookback else df
    high = max(r["h"] for r in seg)
    low = min(r["l"] for r in seg)
    return low, high


def detect_price_action(df: list, side: str) -> bool:
    """📊 偵測 Pin Bar 或吞沒形態，方向需與交易方向一致"""
    if len(df) < 2:
        return False
    last, prev = df[-1], df[-2]
    body = abs(last["c"] - last["o"])
    upper = last["h"] - max(last["c"], last["o"])
    lower = min(last["c"], last["o"]) - last["l"]

    # Pin Bar（影線 ≥ 2 倍實體）
    if body > 0:
        if side == "LONG" and lower > body * 2 and lower > upper:
            return True
        if side == "SHORT" and upper > body * 2 and upper > lower:
            return True

    # 吞沒形態
    if side == "LONG":
        if (
            prev["c"] < prev["o"]
            and last["c"] > last["o"]
            and last["c"] > prev["o"]
            and last["o"] < prev["c"]
        ):
            return True
    else:
        if (
            prev["c"] > prev["o"]
            and last["c"] < last["o"]
            and last["c"] < prev["o"]
            and last["o"] > prev["c"]
        ):
            return True
    return False


def detect_liquidity_sweep(df: list, side: str, lookback: int = 20) -> bool:
    """💧 流動性掃蕩

    多頭掃蕩：最後一根創 N 期新低後快速收回（收盤回到區間中位以上）。
    空頭掃蕩：最後一根創 N 期新高後快速回落。
    """
    if len(df) < lookback + 1:
        return False
    seg = df[-(lookback + 1) : -1]
    last = df[-1]
    prev_low = min(r["l"] for r in seg)
    prev_high = max(r["h"] for r in seg)
    mid = (prev_low + prev_high) / 2

    if side == "LONG":
        return last["l"] < prev_low and last["c"] > mid
    return last["h"] > prev_high and last["c"] < mid


def calc_momentum_ratio(df: list, side: str, n: int = 5) -> bool:
    """📈 盤口動能：最近 N 根 K 線多空比例"""
    seg = df[-n:]
    bull = sum(1 for r in seg if r["c"] > r["o"])
    ratio = bull / max(1, len(seg))
    return ratio >= 0.6 if side == "LONG" else ratio <= 0.4


# ═════════════════════════════════════════════════════════
# 6.4 v14.2 EMA 多週期排列
# ═════════════════════════════════════════════════════════
def calc_ema(df: list, period: int) -> float:
    """EMA 計算（種子用 SMA）"""
    if len(df) < period:
        return df[-1]["c"] if df else 0.0
    closes = [c["c"] for c in df]
    multiplier = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * multiplier + ema * (1 - multiplier)
    return ema


def calc_ema_alignment(df: list, side: str, cfg: dict | None = None) -> tuple[int, str]:
    """🪜 EMA 多週期排列評分 → (分數 -5~+5, 描述)

    多頭完美排列：價格 > EMA20 > EMA50 > EMA200
    空頭完美排列：價格 < EMA20 < EMA50 < EMA200
    """
    if cfg is None:
        cfg = load_config()
    ec = cfg.get("ema_alignment", {})
    if not ec.get("enabled", True):
        return 0, ""
    periods = ec.get("periods", [20, 50, 200])
    if len(df) < max(periods) + 5:
        return 0, "EMA 資料不足"

    p_short, p_mid, p_long = periods[0], periods[1], periods[2]
    ema_s = calc_ema(df, p_short)
    ema_m = calc_ema(df, p_mid)
    ema_l = calc_ema(df, p_long)
    price = df[-1]["c"]

    if side == "LONG":
        if price > ema_s > ema_m > ema_l:
            return ec.get("perfect_bonus", 5), f"多頭完美排列（價>{p_short}>{p_mid}>{p_long}）"
        if price > ema_s > ema_m:
            return ec.get("partial_bonus", 3), "短中期多頭"
        if price < ema_l:
            return -ec.get("against_penalty", 5), "在 EMA200 之下，逆大趨勢"
    else:
        if price < ema_s < ema_m < ema_l:
            return ec.get("perfect_bonus", 5), f"空頭完美排列（價<{p_short}<{p_mid}<{p_long}）"
        if price < ema_s < ema_m:
            return ec.get("partial_bonus", 3), "短中期空頭"
        if price > ema_l:
            return -ec.get("against_penalty", 5), "在 EMA200 之上，逆大趨勢"
    return 0, "EMA 未明確排列"


# ═════════════════════════════════════════════════════════
# 6.5 v14 新指標：ADX / 多時框 / 量能 / 市場狀態 / 進場時機
# ═════════════════════════════════════════════════════════
def calc_adx(df: list, period: int = 14) -> float:
    """📐 ADX 趨勢強度：>25 強趨勢、<18 震盪、中間過渡"""
    if len(df) < period * 2 + 1:
        return 0.0
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(df)):
        up = df[i]["h"] - df[i - 1]["h"]
        dn = df[i - 1]["l"] - df[i]["l"]
        plus_dm.append(up if (up > dn and up > 0) else 0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0)
        tr = max(
            df[i]["h"] - df[i]["l"],
            abs(df[i]["h"] - df[i - 1]["c"]),
            abs(df[i]["l"] - df[i - 1]["c"]),
        )
        trs.append(tr)
    if len(trs) < period:
        return 0.0
    atr = sum(trs[-period:]) / period
    if atr == 0:
        return 0.0
    plus_di = 100 * sum(plus_dm[-period:]) / period / atr
    minus_di = 100 * sum(minus_dm[-period:]) / period / atr
    s = plus_di + minus_di
    if s == 0:
        return 0.0
    return 100 * abs(plus_di - minus_di) / s


def detect_market_regime(df: list) -> dict:
    """🌐 判斷市場狀態：trend / range / transitional + 是否高波動"""
    adx = calc_adx(df)
    atr = calc_atr(df)
    price = df[-1]["c"] if df else 1
    atr_pct = atr / price * 100 if price else 0
    if adx > 25:
        regime = "trend"
    elif adx < 18:
        regime = "range"
    else:
        regime = "transitional"
    return {
        "regime": regime,
        "adx": round(adx, 1),
        "atr_pct": round(atr_pct, 3),
        "volatile": atr_pct > 2.5,
    }


_mtf_cache: dict = {}


def fetch_mtf_trend(instId: str) -> dict:
    """🕒 抓 1H 與 4H 的 K 線判斷大趨勢（30 秒快取）"""
    now = time.time()
    if instId in _mtf_cache:
        data, t = _mtf_cache[instId]
        if now - t < 30:
            return data
    out = {}
    for tf in ("1H", "4H"):
        df = fetch_candles(instId, tf=tf, limit=50)
        if df:
            st = calc_supertrend(df)
            out[tf] = {
                "supertrend": st,
                "trend": "up" if st == 1 else "down" if st == -1 else "side",
                "rsi": round(calc_rsi(df), 1),
            }
        else:
            out[tf] = {"supertrend": 0, "trend": "side", "rsi": 50}
    _mtf_cache[instId] = (out, now)
    return out


def calc_mtf_alignment(mtf: dict, side: str) -> tuple[int, str]:
    """🎯 多時框共振評分（最高 +15）→ (分數, 說明)"""
    expect = 1 if side == "LONG" else -1
    h1 = mtf.get("1H", {}).get("supertrend", 0)
    h4 = mtf.get("4H", {}).get("supertrend", 0)
    score = 0
    if h1 == expect:
        score += 8
    elif h1 == -expect:
        score -= 5
    if h4 == expect:
        score += 7
    elif h4 == -expect:
        score -= 5
    score = max(-15, min(15, score))

    align_desc = []
    align_desc.append(f"1H={'順' if h1 == expect else '反' if h1 == -expect else '中'}")
    align_desc.append(f"4H={'順' if h4 == expect else '反' if h4 == -expect else '中'}")
    return score, " / ".join(align_desc)


def calc_volume_quality(df: list, lookback: int = 20) -> tuple[float, int]:
    """📊 成交量確認：最後 K 線量 vs 前 N 期均量 → (倍數, 評分 -10~+8)"""
    if len(df) < lookback + 1:
        return 1.0, 0
    seg = df[-(lookback + 1):-1]
    avg = sum(c["v"] for c in seg) / lookback
    if avg <= 0:
        return 1.0, 0
    ratio = df[-1]["v"] / avg
    if ratio >= 2.0:
        s = 8
    elif ratio >= 1.5:
        s = 5
    elif ratio >= 1.0:
        s = 2
    elif ratio >= 0.5:
        s = 0
    else:
        s = -10  # 沒量的訊號直接扣，過濾假突破
    return round(ratio, 2), s


def adjust_tp_by_sr(
    entry: float, side: str, tp_levels: list, df: list
) -> tuple[list, list]:
    """🎯 動態 TP：若固定 R 倍 TP 落在強 S/R 前方，把 TP 拉到關鍵位前

    ⚠️ 修復 v14.4 collapse bug：校正後若 TP 順序亂掉（TP3 ≤ TP2），
       自動回復原值或強制保留最小間距，確保 TP1 < TP2 < TP3（LONG）/
       TP1 > TP2 > TP3（SHORT）

    回傳：(調整後 TP 列表, 調整紀錄)
    """
    sup, res = calc_snr(df, lookback=100)
    out = list(tp_levels)
    notes = []

    if side == "LONG":
        ceiling = res * 0.998
        for i, tp in enumerate(out):
            if tp > res * 1.001 and ceiling > entry:
                notes.append(
                    f"TP{i + 1} 由 {tp:.4f} 校正到 {ceiling:.4f}（避開阻力 {res:.4f}）"
                )
                out[i] = ceiling
        # 強制 TP1 < TP2 < TP3：若校正後重疊，回復原值
        for i in range(1, len(out)):
            if out[i] <= out[i - 1]:
                if tp_levels[i] > out[i - 1]:
                    notes.append(
                        f"TP{i + 1} 校正後 ≤ TP{i}，回復原值 {tp_levels[i]:.4f}"
                    )
                    out[i] = tp_levels[i]
                else:
                    out[i] = out[i - 1] * 1.001
                    notes.append(f"TP{i + 1} 強制 +0.1% 維持順序")
    else:
        floor = sup * 1.002
        for i, tp in enumerate(out):
            if tp < sup * 0.999 and floor < entry:
                notes.append(
                    f"TP{i + 1} 由 {tp:.4f} 校正到 {floor:.4f}（避開支撐 {sup:.4f}）"
                )
                out[i] = floor
        # 強制 TP1 > TP2 > TP3
        for i in range(1, len(out)):
            if out[i] >= out[i - 1]:
                if tp_levels[i] < out[i - 1]:
                    notes.append(
                        f"TP{i + 1} 校正後 ≥ TP{i}，回復原值 {tp_levels[i]:.4f}"
                    )
                    out[i] = tp_levels[i]
                else:
                    out[i] = out[i - 1] * 0.999
                    notes.append(f"TP{i + 1} 強制 -0.1% 維持順序")

    return out, notes


def calc_vwap(df: list, lookback: int = 20) -> float:
    """🪙 VWAP 量加權均價（典型 HLC/3 × Volume / sum(V)）"""
    if not df:
        return 0.0
    seg = df[-lookback:] if len(df) >= lookback else df
    total_pv = sum((c["h"] + c["l"] + c["c"]) / 3 * c["v"] for c in seg)
    total_v = sum(c["v"] for c in seg)
    return total_pv / total_v if total_v > 0 else seg[-1]["c"]


def calc_vwap_score(df: list, side: str, lookback: int = 20) -> tuple[int, str]:
    """🪙 VWAP 偏離評分 → (-3 ~ +3, 描述)

    多單：價在 VWAP 上方 = 多頭強勢 +3
    空單：價在 VWAP 下方 = 空頭強勢 +3
    逆勢時扣分
    """
    if len(df) < 5:
        return 0, ""
    vwap = calc_vwap(df, lookback)
    price = df[-1]["c"]
    if vwap <= 0:
        return 0, ""
    dist_pct = (price - vwap) / vwap * 100

    if side == "LONG":
        if dist_pct > 0.3:
            return 3, f"價於 VWAP 上方 {dist_pct:.2f}%"
        if dist_pct > 0:
            return 1, "價略高於 VWAP"
        if dist_pct < -0.3:
            return -3, f"價於 VWAP 下方 {abs(dist_pct):.2f}% 多頭弱勢"
        return 0, "價接近 VWAP"
    else:
        if dist_pct < -0.3:
            return 3, f"價於 VWAP 下方 {abs(dist_pct):.2f}%"
        if dist_pct < 0:
            return 1, "價略低於 VWAP"
        if dist_pct > 0.3:
            return -3, f"價於 VWAP 上方 {dist_pct:.2f}% 空頭弱勢"
        return 0, "價接近 VWAP"


def detect_pullback(df: list, side: str) -> bool:
    """🌀 偵測回測進場：最後一根 K 出現方向反轉影線 + 收線回升"""
    if len(df) < 3:
        return False
    last = df[-1]
    body = abs(last["c"] - last["o"])
    if body == 0:
        return False
    upper = last["h"] - max(last["c"], last["o"])
    lower = min(last["c"], last["o"]) - last["l"]
    if side == "LONG":
        return lower > body * 1.2 and last["c"] > last["o"]
    return upper > body * 1.2 and last["c"] < last["o"]


# ═════════════════════════════════════════════════════════
# 7. 評分系統（規格 100 分制）
# ═════════════════════════════════════════════════════════
def calc_score(
    df: list,
    side: str,
    current_price: float,
    mtf: dict | None = None,
    instId: str | None = None,
) -> tuple[int, str, dict]:
    """總分 = 趨勢30 + RSI25 + OB20 + FVG15 + SNR5 + PA5 + 流動性5 + 動能5 + MTF15 + Volume8 = 最高 138
    （高於 100 是因為 v14 新增 MTF + Volume 加權，門檻仍預設 68）
    """
    detail = {}
    score = 0

    # 趨勢 (30)
    st = calc_supertrend(df)
    if (side == "LONG" and st == 1) or (side == "SHORT" and st == -1):
        score += 30
        detail["trend"] = 30
    elif st == 0:
        score += 15
        detail["trend"] = 15
    else:
        detail["trend"] = 0

    # RSI (25)
    rsi = calc_rsi(df)
    detail["rsi_value"] = round(rsi, 1)
    if side == "LONG":
        if 30 <= rsi <= 50:
            score += 25
            detail["rsi"] = 25
        elif 50 < rsi < 70:
            score += 15
            detail["rsi"] = 15
        else:
            detail["rsi"] = 0
    else:
        if 50 <= rsi <= 70:
            score += 25
            detail["rsi"] = 25
        elif 30 < rsi < 50:
            score += 15
            detail["rsi"] = 15
        else:
            detail["rsi"] = 0

    # OB (20)
    ob = find_order_block(df, side)
    if ob and ob["low"] * 0.995 <= current_price <= ob["high"] * 1.005:
        score += 20
        detail["ob"] = 20
    else:
        detail["ob"] = 0

    # FVG (15)
    fvg = find_fvg(df, side)
    if fvg and fvg["low"] * 0.997 <= current_price <= fvg["high"] * 1.003:
        score += 15
        detail["fvg"] = 15
    else:
        detail["fvg"] = 0

    # SNR (5)
    sup, res = calc_snr(df)
    if side == "LONG" and current_price <= sup * 1.01:
        score += 5
        detail["snr"] = 5
    elif side == "SHORT" and current_price >= res * 0.99:
        score += 5
        detail["snr"] = 5
    else:
        detail["snr"] = 0

    # 價格行為 (5)
    detail["pa"] = 5 if detect_price_action(df, side) else 0
    score += detail["pa"]

    # 流動性掃蕩 (5)
    detail["liq"] = 5 if detect_liquidity_sweep(df, side) else 0
    score += detail["liq"]

    # 動能 (5)
    detail["mom"] = 5 if calc_momentum_ratio(df, side) else 0
    score += detail["mom"]

    # 🎯 MTF 多時框共振 (-15 ~ +15)
    if mtf is None and instId:
        mtf = fetch_mtf_trend(instId)
    if mtf:
        mtf_score, mtf_desc = calc_mtf_alignment(mtf, side)
        score += mtf_score
        detail["mtf"] = mtf_score
        detail["mtf_desc"] = mtf_desc

    # 📊 成交量 (-10 ~ +8)
    vol_ratio, vol_score = calc_volume_quality(df)
    score += vol_score
    detail["volume"] = vol_score
    detail["volume_ratio"] = vol_ratio

    # 🪜 EMA 多週期排列 (-5 ~ +5)
    ema_score, ema_desc = calc_ema_alignment(df, side)
    score += ema_score
    detail["ema"] = ema_score
    detail["ema_desc"] = ema_desc

    # 🪙 VWAP 偏離（背後評分用，不顯示給用戶 → _ 前綴）
    vwap_score, vwap_desc = calc_vwap_score(df, side)
    score += vwap_score
    detail["_vwap"] = vwap_score
    detail["_vwap_desc"] = vwap_desc

    # 📊 方向偏好調整（從歷史學習）
    bias_dir, bias_amount, bias_note = get_direction_bias()
    if bias_dir:
        if bias_dir == side:
            score += bias_amount
            detail["direction_bias"] = bias_amount
            detail["direction_bias_note"] = bias_note
        elif bias_dir == ("SHORT" if side == "LONG" else "LONG"):
            score -= bias_amount
            detail["direction_bias"] = -bias_amount
            detail["direction_bias_note"] = bias_note

    grade = (
        "A+ 極強 🔥"
        if score >= 85
        else "A 強力 ⭐"
        if score >= 70
        else "B+ 合格 ✅"
        if score >= 68
        else "觀望 ⚪"
    )
    return score, grade, detail


# ═════════════════════════════════════════════════════════
# 8. 訊號生成
# ═════════════════════════════════════════════════════════
def generate_signal(
    instId: str,
    df: list,
    current_price: float,
    funding_rate: float | None = None,
    score_threshold: int | None = None,
    atr_max_pct: float = 0.04,
    signal_expire_hours: int = SIGNAL_EXPIRE_HOURS,
) -> dict | None:
    """🎯 生成最佳交易訊號"""
    if df is None or len(df) < 50:
        return None

    threshold = score_threshold if score_threshold is not None else SCORE_THRESHOLD

    atr = calc_atr(df)
    if atr / current_price > atr_max_pct:
        # 波動過大跳過（止損會被打飛）
        return None

    # 極端資金費率時降分過濾（多頭時資金費率太高代表多方擁擠）
    funding_penalty_long = funding_rate and funding_rate > 0.0008
    funding_penalty_short = funding_rate and funding_rate < -0.0008

    coin = instId.split("-")[0]

    # 🌐 市場狀態識別（趨勢/震盪）→ 影響門檻
    regime_info = detect_market_regime(df)
    if regime_info["regime"] == "range":
        threshold += 5  # 震盪市要求更嚴格
    if regime_info["volatile"]:
        threshold += 3  # 高波動加碼提高門檻

    # 🕒 多時框抓一次給兩個方向共用
    mtf = fetch_mtf_trend(instId)

    candidates = []
    for side in ("LONG", "SHORT"):
        score, grade, detail = calc_score(df, side, current_price, mtf=mtf)
        if side == "LONG" and funding_penalty_long:
            score -= 5
        if side == "SHORT" and funding_penalty_short:
            score -= 5

        # 註記市場狀態到 detail
        detail["regime"] = regime_info["regime"]
        detail["adx"] = regime_info["adx"]
        detail["atr_pct"] = regime_info["atr_pct"]

        # 🌀 進場時機：有回測 K 線 +3 分
        if detect_pullback(df, side):
            score += 3
            detail["pullback"] = True

        # 🧠 統計學習（桶 + KNN 雙路）
        adj_simple, notes_simple = apply_learning_adjustment(
            score, side, detail, funding_rate, coin
        )
        adj_knn, notes_knn = apply_knn_learning(
            score, side, detail, funding_rate, coin, mtf, regime_info
        )
        adjusted_score = adj_simple + (adj_knn - score)
        learning_notes = notes_simple + notes_knn
        if learning_notes:
            detail["learning_notes"] = learning_notes
            detail["learning_adjust"] = adjusted_score - score
        score = adjusted_score

        if score < threshold:
            continue

        entry = current_price
        sl_dist = atr * 1.5
        sl = entry - sl_dist if side == "LONG" else entry + sl_dist
        risk = abs(entry - sl)

        # ✅ 規格倍率：1.5R / 3.0R / 5.0R
        if side == "LONG":
            tp_levels = [entry + risk * 1.5, entry + risk * 3.0, entry + risk * 5.0]
        else:
            tp_levels = [entry - risk * 1.5, entry - risk * 3.0, entry - risk * 5.0]

        # 🎯 動態 TP 校正（預設關閉 → 固定 1.5R/3R/5R 不動）
        cfg_rr_mode = load_config()
        if not cfg_rr_mode.get("fixed_rr_mode", True):
            tp_levels, tp_notes = adjust_tp_by_sr(entry, side, tp_levels, df)
            if tp_notes:
                detail["tp_adjust_notes"] = tp_notes

        # ⚖️ R:R 最低門檻 — TP1 至少要有 N R，否則拒絕（加 0.02 容差避免浮點誤差）
        cfg_rr = load_config()
        min_rr = cfg_rr.get("min_rr_ratio", 1.5)
        actual_tp1_r = abs(tp_levels[0] - entry) / max(risk, 1e-9)
        if actual_tp1_r < min_rr - 0.02:
            logging.info(
                f"[{instId}] {side} 訊號 R:R={actual_tp1_r:.2f} < {min_rr}，拒絕"
            )
            continue

        # 🧱 把 OB 區間存進訊號（給失效退場用）
        ob_zone = find_order_block(df, side)

        candidates.append(
            {
                "instId": instId,
                "side": side,
                "tf": "15m",
                "entry": round(entry, 4),
                "sl": round(sl, 4),
                "tp1": round(tp_levels[0], 4),
                "tp2": round(tp_levels[1], 4),
                "tp3": round(tp_levels[2], 4),
                "score": score,
                "grade": grade,
                "detail": detail,
                "funding_rate": funding_rate,
                "mtf_snapshot": mtf,
                "regime_snapshot": regime_info,
                "ob_zone": ob_zone,            # 🧱 OB 失效退場用
                "created": time.time(),
                "expires": time.time() + signal_expire_hours * 3600,
            }
        )

    return max(candidates, key=lambda x: x["score"]) if candidates else None


# ═════════════════════════════════════════════════════════
# 9. 持久化（冷卻 / 訊號 / 交易）
# ═════════════════════════════════════════════════════════
def _load_json(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logging.warning(f"⚠️ 讀取 {path} 失敗：{e}")
    return default


def _save_json(path: str, data) -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logging.error(f"❌ 寫入 {path} 失敗：{e}")


# ═════════════════════════════════════════════════════════
# 9.5 配置熱更新與驗證
# ═════════════════════════════════════════════════════════
def _deep_merge(base: dict, override: dict) -> dict:
    """遞迴合併：override 覆蓋 base，但保留 base 中 override 沒覆蓋的鍵"""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _validate_config(cfg: dict) -> list:
    """🛡️ 驗證 config 合理性 → 回傳錯誤訊息列表（空代表通過）"""
    errs = []
    if not (50 <= cfg.get("score_threshold", 0) <= 100):
        errs.append("score_threshold 必須在 50–100")
    if not (1 <= cfg.get("max_signals", 0) <= 10):
        errs.append("max_signals 必須在 1–10")
    if cfg.get("cooldown_hours", -1) < 0:
        errs.append("cooldown_hours 不能為負")
    if cfg.get("signal_expire_hours", 0) <= 0:
        errs.append("signal_expire_hours 必須 > 0")
    pv = cfg.get("price_verification", {})
    if not (0 < pv.get("max_deviation_pct", 0) < 10):
        errs.append("price_verification.max_deviation_pct 應在 0–10%")
    cb = cfg.get("circuit_breaker", {})
    if cb.get("soft_threshold", 0) >= cb.get("hard_threshold", 99):
        errs.append("soft_threshold 應 < hard_threshold")
    for w in cfg.get("blackout_windows_tw", []):
        try:
            for k in ("start", "end"):
                hh, mm = map(int, w[k].split(":"))
                assert 0 <= hh < 24 and 0 <= mm < 60
        except Exception:
            errs.append(f"blackout_windows_tw 時段格式錯誤：{w}")
    return errs


def load_config() -> dict:
    """🔄 載入 config.json（不存在或驗證失敗則用預設值）"""
    user_cfg = _load_json(CONFIG_FILE, {})
    merged = _deep_merge(DEFAULT_CONFIG, user_cfg) if user_cfg else dict(DEFAULT_CONFIG)
    errs = _validate_config(merged)
    if errs:
        logging.warning("⚠️ 配置驗證失敗，全面 fallback 到預設值：" + "; ".join(errs))
        return dict(DEFAULT_CONFIG)
    return merged


def is_cooling(instId: str, cooldown_hours: float = COOLDOWN_HOURS) -> bool:
    """🧊 是否還在冷卻期內（持久化版本）"""
    cd = _load_json(COOLDOWN_FILE, {})
    last = cd.get(instId)
    if last is None:
        return False
    return (time.time() - float(last)) < cooldown_hours * 3600


def mark_cooldown(instId: str, cooldown_hours: float = COOLDOWN_HOURS) -> None:
    cd = _load_json(COOLDOWN_FILE, {})
    cd[instId] = time.time()
    # 順便清除過期紀錄
    cutoff = time.time() - cooldown_hours * 3600 * 3
    cd = {k: v for k, v in cd.items() if float(v) > cutoff}
    _save_json(COOLDOWN_FILE, cd)


def record_trade(
    coin: str,
    side: str,
    order_id: str,
    entry: float,
    close_price: float,
    close_type: str,
    score: int,
    sig_snapshot: dict | None = None,
) -> None:
    """📝 記錄交易歷史 + 餵給學習機制"""
    is_win = close_type in ("TP1", "TP2", "TP3", "LOCK")
    is_be = close_type == "BE"
    pnl = (
        (close_price - entry) / entry * 100
        if side == "LONG"
        else (entry - close_price) / entry * 100
    )
    snap = sig_snapshot or {}
    detail = snap.get("detail", {}) or {}
    funding_rate = snap.get("funding_rate")
    mtf = snap.get("mtf_snapshot")
    regime = snap.get("regime_snapshot")

    # 🧬 進場時的特徵向量（給 KNN 學習查相似度用）
    features = vectorize_signal(score, side, detail, funding_rate, mtf, regime)

    trade = {
        "time": tw_now().strftime("%Y-%m-%d %H:%M"),
        "date": tw_now().strftime("%Y-%m-%d"),
        "order_id": order_id,
        "coin": coin,
        "side": side,
        "entry": entry,
        "close": close_price,
        "close_type": close_type,
        "pnl": round(pnl, 2),
        "is_win": is_win,
        "is_be": is_be,
        "score": score,
        "funding_rate": funding_rate,
        "detail": detail,
        "features": features,        # 🧬 KNN 用
        "mtf": mtf,                  # 進場時 1H/4H 趨勢
        "regime": regime,            # 進場時市場狀態
    }
    history = _load_json(TRADE_HISTORY_FILE, [])
    history.append(trade)
    _save_json(TRADE_HISTORY_FILE, history)
    logging.info(f"📝 記錄交易：{coin} {order_id} {close_type}")

    # 🧠 餵給學習機制
    try:
        update_learning(trade, sig_snapshot)
    except Exception as e:
        logging.warning(f"⚠️ 更新學習狀態失敗：{e}")


# ═════════════════════════════════════════════════════════
# 9.6 學習機制（每筆交易結束後更新桶 → 評分時自動套用調整）
# ═════════════════════════════════════════════════════════
def _bucket_score(score: int) -> str:
    if score >= 90:
        return "score:90+"
    if score >= 80:
        return "score:80-89"
    if score >= 70:
        return "score:70-79"
    return "score:60-69"


def _bucket_rsi(rsi: float, side: str) -> str:
    bucket = int(rsi // 10) * 10
    return f"rsi_{side.lower()}:{bucket}-{bucket + 9}"


def _bucket_funding(fr) -> str:
    if fr is None:
        return "fund:none"
    if fr > 0.0008:
        return "fund:very_pos"
    if fr > 0.0001:
        return "fund:pos"
    if fr > -0.0001:
        return "fund:neutral"
    if fr > -0.0008:
        return "fund:neg"
    return "fund:very_neg"


def _bucket_session_tw() -> str:
    """以台灣時間粗分四個交易時段"""
    h = tw_now().hour
    if 0 <= h < 6:
        return "sess:asia_dawn"
    if 6 <= h < 14:
        return "sess:asia_day"
    if 14 <= h < 21:
        return "sess:europe"
    return "sess:us"


def _signal_buckets(score: int, side: str, detail: dict, funding_rate, coin: str) -> list:
    """把訊號特徵打成多個桶 → 供學習查詢"""
    rsi = (detail or {}).get("rsi_value", 50)
    return [
        _bucket_score(score),
        _bucket_rsi(rsi, side),
        _bucket_funding(funding_rate),
        _bucket_session_tw(),
        f"coin:{coin}",
        f"coin_side:{coin}_{side}",
    ]


def update_learning(trade: dict, sig_snapshot: dict | None = None) -> None:
    """🧠 每筆交易結束後更新學習桶與按幣種統計"""
    state = _load_json(LEARNING_FILE, {})
    state.setdefault("buckets", {})
    state.setdefault("by_coin", {})
    state.setdefault("loss_reasons", [])
    state.setdefault("updated_at", 0)

    score = trade.get("score", 0)
    coin = trade.get("coin", "?")
    side = trade.get("side", "?")
    close_type = trade.get("close_type", "?")
    funding_rate = trade.get("funding_rate")
    detail = trade.get("detail") or (sig_snapshot.get("detail") if sig_snapshot else {})

    is_win = close_type in ("TP1", "TP2", "TP3", "LOCK")
    is_be = close_type == "BE"
    is_loss = close_type == "SL"

    for b in _signal_buckets(score, side, detail, funding_rate, coin):
        bd = state["buckets"].setdefault(
            b, {"win": 0, "loss": 0, "be": 0, "total": 0}
        )
        bd["total"] += 1
        if is_win:
            bd["win"] += 1
        elif is_loss:
            bd["loss"] += 1
        elif is_be:
            bd["be"] += 1

    cd = state["by_coin"].setdefault(
        coin, {"win": 0, "loss": 0, "be": 0, "total": 0}
    )
    cd["total"] += 1
    if is_win:
        cd["win"] += 1
    elif is_loss:
        cd["loss"] += 1
    elif is_be:
        cd["be"] += 1

    state["updated_at"] = time.time()
    _save_json(LEARNING_FILE, state)


def apply_learning_adjustment(
    score: int,
    side: str,
    detail: dict,
    funding_rate,
    coin: str,
) -> tuple[int, list]:
    """🧠 套用學習狀態 → (調整後分數, 套用紀錄)"""
    cfg = load_config()
    lcfg = cfg.get("learning", {})
    if not lcfg.get("enabled", True):
        return score, []

    state = _load_json(LEARNING_FILE, {})
    buckets = state.get("buckets", {})
    min_samples = lcfg.get("min_samples", 5)
    max_adj = lcfg.get("max_score_adjust", 10)

    notes = []
    adj_total = 0
    for b in _signal_buckets(score, side, detail, funding_rate, coin):
        bd = buckets.get(b)
        if not bd or bd.get("total", 0) < min_samples:
            continue
        wr = bd["win"] / bd["total"]
        if wr < 0.30:
            d = -3
        elif wr < 0.40:
            d = -2
        elif wr > 0.70:
            d = +2
        elif wr > 0.60:
            d = +1
        else:
            continue
        adj_total += d
        notes.append(f"{b} (n={bd['total']}, 勝率 {wr:.0%}) → {d:+d}")

    adj_total = max(-max_adj, min(max_adj, adj_total))
    return score + adj_total, notes


def _summarize_trades(trades: list) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0}
    win = sum(1 for t in trades if t.get("close_type") in ("TP1", "TP2", "TP3", "LOCK"))
    loss = sum(1 for t in trades if t.get("close_type") == "SL")
    be = sum(1 for t in trades if t.get("close_type") == "BE")
    pnl = sum(t.get("pnl", 0) for t in trades)
    pnls = [t.get("pnl", 0) for t in trades]
    avg = pnl / n if n else 0
    biggest_win = max(pnls) if pnls else 0
    biggest_loss = min(pnls) if pnls else 0
    return {
        "n": n,
        "win": win,
        "loss": loss,
        "be": be,
        "wr": win / n * 100 if n else 0,
        "pnl": pnl,
        "avg": avg,
        "max_win": biggest_win,
        "max_loss": biggest_loss,
    }


def format_daily_report(date: str | None = None) -> str:
    """📊 日報：當天交易概覽 + 績效"""
    if date is None:
        date = tw_now().strftime("%Y-%m-%d")
    history = _load_json(TRADE_HISTORY_FILE, [])
    today = [t for t in history if t.get("date") == date]
    s = _summarize_trades(today)
    if s["n"] == 0:
        return f"📭 *日報 {date}*\n當日尚無交易紀錄"

    lines = [
        f"📊 *日報 {date}*",
        "━━━━━━━━━━━━━━",
        f"交易筆數：{s['n']}",
        f"勝 / 平 / 敗：{s['win']} / {s['be']} / {s['loss']}",
        f"勝率：`{s['wr']:.0f}%`",
        f"總 PnL：`{s['pnl']:+.2f}%`",
        f"平均：`{s['avg']:+.2f}%/筆`",
        f"最大獲利：`{s['max_win']:+.2f}%`　最大虧損：`{s['max_loss']:+.2f}%`",
        "",
    ]

    # 各幣種表現
    by_coin = {}
    for t in today:
        c = t.get("coin", "?")
        by_coin.setdefault(c, []).append(t)
    if by_coin:
        lines.append("💎 *各幣種表現*：")
        for c, ts in sorted(by_coin.items(), key=lambda x: -sum(t.get("pnl", 0) for t in x[1])):
            sub = _summarize_trades(ts)
            lines.append(
                f"  {c}: {sub['n']} 筆 (勝 {sub['win']}/敗 {sub['loss']}) "
                f"PnL `{sub['pnl']:+.2f}%`"
            )

    return "\n".join(lines)


def format_monthly_report(year_month: str | None = None) -> str:
    """📈 月報：當月績效 + 學習進展"""
    if year_month is None:
        year_month = tw_now().strftime("%Y-%m")
    history = _load_json(TRADE_HISTORY_FILE, [])
    month = [t for t in history if t.get("date", "").startswith(year_month)]
    s = _summarize_trades(month)
    if s["n"] == 0:
        return f"📭 *月報 {year_month}*\n本月尚無交易紀錄"

    lines = [
        f"📈 *月報 {year_month}*",
        "━━━━━━━━━━━━━━",
        f"總交易：{s['n']} 筆",
        f"勝 / 平 / 敗：{s['win']} / {s['be']} / {s['loss']}",
        f"勝率：`{s['wr']:.0f}%`",
        f"總 PnL：`{s['pnl']:+.2f}%`",
        f"平均：`{s['avg']:+.2f}%/筆`",
        f"最大獲利：`{s['max_win']:+.2f}%`　最大虧損：`{s['max_loss']:+.2f}%`",
        "",
    ]

    # 連勝 / 連敗
    cur_streak = 0
    streak_type = None
    max_win_streak = 0
    max_loss_streak = 0
    for t in month:
        ct = t.get("close_type")
        is_w = ct in ("TP1", "TP2", "TP3", "LOCK")
        is_l = ct == "SL"
        if is_w:
            if streak_type == "win":
                cur_streak += 1
            else:
                streak_type = "win"
                cur_streak = 1
            max_win_streak = max(max_win_streak, cur_streak)
        elif is_l:
            if streak_type == "loss":
                cur_streak += 1
            else:
                streak_type = "loss"
                cur_streak = 1
            max_loss_streak = max(max_loss_streak, cur_streak)

    lines.append(f"🔥 最大連勝：{max_win_streak}　❄️ 最大連敗：{max_loss_streak}")
    lines.append("")

    # 各幣種
    by_coin = {}
    for t in month:
        c = t.get("coin", "?")
        by_coin.setdefault(c, []).append(t)
    if by_coin:
        lines.append("💎 *各幣種表現*：")
        ranked = sorted(by_coin.items(), key=lambda x: -sum(t.get("pnl", 0) for t in x[1]))
        for c, ts in ranked:
            sub = _summarize_trades(ts)
            lines.append(
                f"  {c}: {sub['n']} 筆 · 勝率 `{sub['wr']:.0f}%` · PnL `{sub['pnl']:+.2f}%`"
            )

    return "\n".join(lines)


def format_learning_report() -> str:
    """🧠 /learning 命令 → 顯示機器人學到了什麼"""
    state = _load_json(LEARNING_FILE, {})
    buckets = state.get("buckets", {})
    by_coin = state.get("by_coin", {})
    loss_reasons = state.get("loss_reasons", [])

    if not buckets and not by_coin:
        return (
            "🧠 *機器人學習狀態*\n\n"
            "📭 目前還沒累積足夠資料\n"
            "至少需要 5 筆已結束交易才會開始套用學習調整"
        )

    lines = ["🧠 *機器人學習狀態*", "━━━━━━━━━━━━━━", ""]

    # 1. 按幣種勝率
    if by_coin:
        lines.append("📊 *各幣種戰績*：")
        sorted_coins = sorted(by_coin.items(), key=lambda x: -x[1].get("total", 0))
        for coin, d in sorted_coins[:12]:
            n = d.get("total", 0)
            w = d.get("win", 0)
            l = d.get("loss", 0)
            be = d.get("be", 0)
            wr = w / n * 100 if n else 0
            lines.append(
                f"  {coin}: {n} 筆（勝 {w} / 平 {be} / 敗 {l}，勝率 `{wr:.0f}%`）"
            )
        lines.append("")

    # 2. 高勝率組合（樣本 ≥ 5）
    high_wr = [
        (b, d) for b, d in buckets.items()
        if d.get("total", 0) >= 5 and d["win"] / d["total"] > 0.6
    ]
    if high_wr:
        lines.append("✅ *高勝率組合（>60%）*：")
        for b, d in sorted(high_wr, key=lambda x: -x[1]["win"] / x[1]["total"])[:5]:
            wr = d["win"] / d["total"] * 100
            lines.append(f"  `{b}` → {d['total']} 筆，勝率 `{wr:.0f}%`")
        lines.append("")

    # 3. 低勝率組合
    low_wr = [
        (b, d) for b, d in buckets.items()
        if d.get("total", 0) >= 5 and d["win"] / d["total"] < 0.4
    ]
    if low_wr:
        lines.append("⚠️ *低勝率組合（<40%）*：")
        for b, d in sorted(low_wr, key=lambda x: x[1]["win"] / x[1]["total"])[:5]:
            wr = d["win"] / d["total"] * 100
            lines.append(f"  `{b}` → {d['total']} 筆，勝率 `{wr:.0f}%`")
        lines.append("")

    # 4. 主要止損原因
    if loss_reasons:
        from collections import Counter
        cnt = Counter(r.get("title", "?") for r in loss_reasons[-30:])
        lines.append("🔍 *最近 30 筆止損主因 TOP3*：")
        for title, c in cnt.most_common(3):
            lines.append(f"  {title} × {c}")
        lines.append("")

    lines.append("💡 _這些統計每筆交易結算後自動更新；下次相似情境的訊號評分會自動微調_")
    return "\n".join(lines)


def vectorize_signal(
    score: int,
    side: str,
    detail: dict,
    funding_rate,
    mtf: dict | None = None,
    regime: dict | None = None,
) -> dict:
    """🧬 把訊號特徵打成向量（給 KNN 用）"""
    rsi = (detail or {}).get("rsi_value", 50)
    return {
        "score": float(score),
        "rsi": float(rsi),
        "atr_pct": float((detail or {}).get("atr_pct", 1.0)),
        "funding": float(funding_rate or 0) * 1000,
        "vol_ratio": float((detail or {}).get("volume_ratio", 1.0)),
        "adx": float((regime or {}).get("adx", 20)),
        "mtf_h1": 1.0 if (mtf or {}).get("1H", {}).get("supertrend") == (1 if side == "LONG" else -1) else 0.0,
        "mtf_h4": 1.0 if (mtf or {}).get("4H", {}).get("supertrend") == (1 if side == "LONG" else -1) else 0.0,
        "side": 1.0 if side == "LONG" else 0.0,
    }


_FEATURE_SCALE = {
    "score": 30, "rsi": 50, "atr_pct": 3, "funding": 2,
    "vol_ratio": 3, "adx": 50, "mtf_h1": 1, "mtf_h4": 1, "side": 1,
}


def find_similar_trades(features: dict, history: list, k: int = 10) -> list:
    """🧬 KNN：找最相似的 k 筆有特徵的歷史交易（歐式距離，已歸一化）"""
    candidates = []
    for t in history:
        f = t.get("features")
        if not f:
            continue
        d2 = 0.0
        for key, scale in _FEATURE_SCALE.items():
            diff = (features.get(key, 0) - f.get(key, 0)) / max(scale, 1)
            d2 += diff * diff
        candidates.append((d2, t))
    candidates.sort(key=lambda x: x[0])
    return [t for _, t in candidates[:k]]


def apply_knn_learning(
    score: int,
    side: str,
    detail: dict,
    funding_rate,
    coin: str,
    mtf: dict | None,
    regime: dict | None,
) -> tuple[int, list]:
    """🧬 KNN 學習：找最相似的 10 筆歷史交易，看勝率 → (調整後分數, 紀錄)"""
    cfg = load_config()
    if not cfg.get("learning", {}).get("knn_enabled", True):
        return score, []
    history = _load_json(TRADE_HISTORY_FILE, [])
    if len(history) < 10:
        return score, []
    feat = vectorize_signal(score, side, detail, funding_rate, mtf, regime)
    similar = find_similar_trades(feat, history, k=10)
    if len(similar) < 3:
        return score, []
    wins = sum(1 for t in similar if t.get("close_type") in ("TP1", "TP2", "TP3", "LOCK"))
    losses = sum(1 for t in similar if t.get("close_type") == "SL")
    n = len(similar)
    wr = wins / n
    notes = [f"🧬 KNN：{n} 筆最相似訊號 → 勝 {wins} / 敗 {losses} (勝率 {wr:.0%})"]
    if wr < 0.30:
        return score - 8, notes + ["KNN 低勝率 → -8"]
    if wr < 0.40:
        return score - 4, notes + ["KNN 偏低勝率 → -4"]
    if wr > 0.70:
        return score + 5, notes + ["KNN 高勝率 → +5"]
    if wr > 0.60:
        return score + 3, notes + ["KNN 中高勝率 → +3"]
    return score, notes


def record_loss_reason(coin: str, side: str, reasons: list) -> None:
    """記錄止損主因到 learning_state（供後續查詢）"""
    state = _load_json(LEARNING_FILE, {})
    state.setdefault("loss_reasons", [])
    for r in reasons[:1]:  # 只記第一名主因
        state["loss_reasons"].append({
            "ts": time.time(),
            "coin": coin,
            "side": side,
            "code": r.get("code"),
            "title": r.get("title"),
        })
    # 只保留最近 100 筆
    state["loss_reasons"] = state["loss_reasons"][-100:]
    _save_json(LEARNING_FILE, state)


# ═════════════════════════════════════════════════════════
# 9.65 覆盤分析（SL/BE/LOCK 後解釋為什麼）
# ═════════════════════════════════════════════════════════
def analyze_loss(sig: dict, df_at_loss: list) -> list:
    """🔍 比較進場附近 vs 出場附近的市況，回推主因（最多 3 名）"""
    if not df_at_loss or len(df_at_loss) < 20:
        return [{
            "code": "INSUFFICIENT",
            "title": "📋 資料不足",
            "detail": "進場後 K 線太少，無法詳細分析",
            "severity": 0,
        }]

    side = sig["side"]
    expect = 1 if side == "LONG" else -1
    n = len(df_at_loss)
    df_then = df_at_loss[: max(20, n // 3)]
    df_now = df_at_loss

    reasons = []

    # 1. 趨勢反轉
    st_then = calc_supertrend(df_then)
    st_now = calc_supertrend(df_now)
    if st_then == expect and st_now == -expect:
        reasons.append({
            "code": "TREND_REVERSAL",
            "title": "🔄 趨勢反轉",
            "detail": f"進場時 Supertrend 順勢（{'多' if expect == 1 else '空'}），止損前已翻向反向",
            "severity": 30,
        })

    # 2. RSI 動能崩塌 / 反轉
    rsi_then = calc_rsi(df_then)
    rsi_now = calc_rsi(df_now)
    if side == "LONG" and rsi_then > 45 and rsi_now < 35 and (rsi_then - rsi_now) > 12:
        reasons.append({
            "code": "RSI_COLLAPSE",
            "title": "📉 多頭動能瓦解",
            "detail": f"RSI 從 {rsi_then:.0f} 急跌至 {rsi_now:.0f}（下跌 {rsi_then - rsi_now:.0f} 分）",
            "severity": 25,
        })
    elif side == "SHORT" and rsi_then < 55 and rsi_now > 65 and (rsi_now - rsi_then) > 12:
        reasons.append({
            "code": "RSI_REBOUND",
            "title": "📈 空頭動能反轉",
            "detail": f"RSI 從 {rsi_then:.0f} 反彈至 {rsi_now:.0f}（上漲 {rsi_now - rsi_then:.0f} 分）",
            "severity": 25,
        })

    # 3. 流動性掃蕩（反向假突破）
    sweep_dir = "SHORT" if side == "LONG" else "LONG"
    if detect_liquidity_sweep(df_now[-12:], sweep_dir):
        reasons.append({
            "code": "LIQ_SWEEP",
            "title": "🌊 流動性掃蕩",
            "detail": "止損前出現反向假突破插針後快速收回，疑似主力掃損",
            "severity": 22,
        })

    # 4. 波動率激增
    atr_then = calc_atr(df_then)
    atr_now = calc_atr(df_now)
    if atr_then > 0 and atr_now / atr_then > 1.5:
        reasons.append({
            "code": "VOL_SPIKE",
            "title": "🌪 波動率激增",
            "detail": f"ATR 從 {atr_then:.4f} 擴張至 {atr_now:.4f}（{(atr_now / atr_then - 1) * 100:.0f}% 增幅）",
            "severity": 18,
        })

    # 5. 連續反向 K 線
    last10 = df_now[-10:]
    against = sum(
        1 for b in last10
        if (side == "LONG" and b["c"] < b["o"]) or (side == "SHORT" and b["c"] > b["o"])
    )
    if against >= 7:
        reasons.append({
            "code": "AGAINST_MOMENTUM",
            "title": "💪 持續反向動能",
            "detail": f"出場前 10 根 K 線中 {against} 根反向收線，趨勢已轉",
            "severity": 15,
        })

    # 6. OB / FVG 結構失效
    ob = find_order_block(df_then, side)
    if ob:
        breached = (
            (side == "LONG" and df_now[-1]["c"] < ob["low"])
            or (side == "SHORT" and df_now[-1]["c"] > ob["high"])
        )
        if breached:
            reasons.append({
                "code": "OB_BROKEN",
                "title": "🧱 訂單塊跌破",
                "detail": "進場依據的 SMC 訂單塊已被收盤跌破，結構失效",
                "severity": 20,
            })

    if not reasons:
        reasons.append({
            "code": "NORMAL_NOISE",
            "title": "📊 正常波動",
            "detail": "未偵測到明確的趨勢反轉或結構破壞，可能是 ATR 範圍內的正常雜訊掃損",
            "severity": 5,
        })

    reasons.sort(key=lambda x: -x["severity"])
    return reasons[:3]


def _generate_lessons(reasons: list) -> list:
    """根據主因產生「下次該怎麼判斷」的建議"""
    advice_map = {
        "TREND_REVERSAL": "進場後若 Supertrend 翻向反向，建議立即減倉或主動出場，不要等止損",
        "RSI_COLLAPSE": "RSI 從中性區（>45）急跌到超賣（<35）通常代表動能轉換，可作為提前離場信號",
        "RSI_REBOUND": "RSI 從中性區（<55）反彈到超買（>65）通常代表空頭動能瓦解，提早平倉避損",
        "LIQ_SWEEP": "插針型止損若反向 K 隨後出現，多半是主力誘多/誘空，下次可把 SL 拉遠 0.2 ATR",
        "VOL_SPIKE": "ATR 突然擴張代表進入高波動區，建議該幣種暫停 1–2 小時或縮小倉位",
        "AGAINST_MOMENTUM": "反向 K 連續 7 根以上 = 趨勢明確，應比原訂 SL 更早主動止損鎖損",
        "OB_BROKEN": "SMC 訂單塊一旦收盤跌破代表結構失效，這時繼續抱單虧損會放大",
        "NORMAL_NOISE": "本次屬正常波動雜訊，可能 SL 設得太緊，下次 ATR×1.5 → ATR×1.8 會更穩",
        "INSUFFICIENT": "進場後資料不足，無法詳細歸因",
    }
    out = []
    seen = set()
    for r in reasons[:2]:
        code = r.get("code")
        if code in seen or code not in advice_map:
            continue
        seen.add(code)
        out.append(advice_map[code])
    return out


def _fmt_postmortem(
    sig: dict,
    mode: str,
    reasons: list,
    lessons: list,
    similar_stats: tuple | None = None,
) -> str:
    """🔍 覆盤分析訊息"""
    coin = sig["instId"].split("-")[0]
    order_id = sig.get("order_id", "N/A")
    side = sig["side"]
    direction = "做多" if side == "LONG" else "做空"
    label = (
        "❌ 止損"
        if mode == "LOSS"
        else "🔒 保本"
        if mode == "BE"
        else "🔐 鎖利"
        if mode == "LOCK"
        else "🎯 止盈"
    )

    lines = [
        f"🔍 *{coin} 覆盤分析*",
        f"━━━━━━━━━━━━━━",
        f"🆔 訂單：`{order_id}`",
        f"⏰ 時間：{tw_ts()}",
        f"方向：{direction}　結算：{label}",
        f"原始評分：{sig.get('score', 0)} 分",
        "",
        "📋 *主要原因（依嚴重度）*：",
    ]
    for i, r in enumerate(reasons, 1):
        lines.append(f"{i}. {r['title']}")
        lines.append(f"   _{r['detail']}_")

    if lessons:
        lines.append("")
        lines.append("💡 *下次該怎麼判斷*：")
        for l in lessons:
            lines.append(f"  • {l}")

    if similar_stats:
        n, w, l, be = similar_stats
        if n >= 3:
            wr = w / n * 100
            lines.append("")
            lines.append(
                f"📊 同類設定歷史：{n} 筆（勝 {w} / 平 {be} / 敗 {l}，勝率 `{wr:.0f}%`）"
            )

    lines.append("")
    lines.append("🧠 _此次主因已寫入學習資料，下次相似情況評分自動調整_")
    return "\n".join(lines)


def get_similar_stats(score: int, side: str, detail: dict, funding_rate, coin: str) -> tuple:
    """從學習狀態取「同類設定」的歷史勝負"""
    state = _load_json(LEARNING_FILE, {})
    buckets = state.get("buckets", {})
    # 取「coin_side」這個最具體的桶
    key = f"coin_side:{coin}_{side}"
    bd = buckets.get(key, {})
    n = bd.get("total", 0)
    w = bd.get("win", 0)
    l = bd.get("loss", 0)
    be = bd.get("be", 0)
    return (n, w, l, be)


# ═════════════════════════════════════════════════════════
# 9.7 系統狀態（熔斷紀錄）
# ═════════════════════════════════════════════════════════
def get_system_state() -> dict:
    return _load_json(SYSTEM_STATE_FILE, {})


def set_system_state(state: dict) -> None:
    _save_json(SYSTEM_STATE_FILE, state)


def check_health() -> tuple[bool, str]:
    """🩺 系統健康檢查 → (有問題?, 訊息)

    觸發條件：
      1. 超過 24 小時沒成功送過任何 TG 訊息
      2. 連續 5 次掃描異常結束
    （6 小時內不重複警報）
    """
    state = get_system_state()
    last_tg = state.get("last_tg_sent", 0)
    last_warn = state.get("last_health_warning", 0)
    fail_count = state.get("scan_failure_count", 0)

    # 6h 內不重複警報
    if time.time() - last_warn < 6 * 3600:
        return False, ""

    # 條件 1：24h 沒送過 TG
    if last_tg > 0:
        hours_since = (time.time() - last_tg) / 3600
        if hours_since > 24:
            state["last_health_warning"] = time.time()
            set_system_state(state)
            return True, (
                f"⚠️ *系統健康警報*\n"
                f"━━━━━━━━━━━━━━\n"
                f"⏰ 時間：{tw_ts()}\n"
                f"\n"
                f"已超過 *{hours_since:.0f} 小時*沒發出任何訊號 / 通知\n"
                f"\n"
                f"💡 可能原因：\n"
                f"  • TG_TOKEN 失效\n"
                f"  • OKX API 異常\n"
                f"  • 訊號全被學習機制 / 風控過濾\n"
                f"  • GitHub Actions 配額耗盡\n"
                f"\n"
                f"請檢查 GitHub Actions 頁面與 Telegram bot 狀態"
            )

    # 條件 2：連續失敗 5 次
    if fail_count >= 5:
        state["last_health_warning"] = time.time()
        set_system_state(state)
        return True, (
            f"⚠️ *系統健康警報*\n"
            f"━━━━━━━━━━━━━━\n"
            f"⏰ 時間：{tw_ts()}\n"
            f"\n"
            f"主掃描已連續失敗 *{fail_count} 次*\n"
            f"\n"
            f"💡 請進 GitHub Actions 查最近一次失敗的 log，或 @ 我幫你 debug"
        )
    return False, ""


def increment_failure_count() -> None:
    """掃描失敗時呼叫 → 失敗計數 +1"""
    state = get_system_state()
    state["scan_failure_count"] = state.get("scan_failure_count", 0) + 1
    set_system_state(state)


def reset_failure_count() -> None:
    """掃描成功時呼叫 → 重置失敗計數"""
    state = get_system_state()
    if state.get("scan_failure_count", 0) > 0:
        state["scan_failure_count"] = 0
        set_system_state(state)


# ═════════════════════════════════════════════════════════
# 9.8 連續虧損熔斷
# ═════════════════════════════════════════════════════════
def check_circuit_breaker(cfg: dict) -> tuple[bool, str, int]:
    """🛑 檢查連續虧損熔斷 → (是否暫停, 訊息, 連敗次數)"""
    cb = cfg.get("circuit_breaker", {})
    if not cb.get("enabled", True):
        return False, "", 0

    history = _load_json(TRADE_HISTORY_FILE, [])
    # 只看最近 20 筆已結束交易（含 LOCK 鎖利）
    recent = [
        t for t in history
        if t.get("close_type") in ("SL", "BE", "LOCK", "TP1", "TP2", "TP3")
    ][-20:]
    if not recent:
        return False, "", 0

    # 從尾巴往前數連敗（SL 計敗、TP1/2/3/BE 中斷連敗）
    losses = 0
    last_loss_time: datetime | None = None
    for t in reversed(recent):
        if t.get("close_type") == "SL":
            losses += 1
            if last_loss_time is None:
                try:
                    last_loss_time = datetime.strptime(
                        t["time"], "%Y-%m-%d %H:%M"
                    ).replace(tzinfo=TW_TZ)
                except Exception:
                    last_loss_time = tw_now()
        else:
            break

    if losses == 0 or last_loss_time is None:
        return False, "", 0

    elapsed_h = (tw_now() - last_loss_time).total_seconds() / 3600

    hard_n = cb.get("hard_threshold", 5)
    hard_h = cb.get("hard_pause_hours", 24)
    soft_n = cb.get("soft_threshold", 3)
    soft_h = cb.get("soft_pause_hours", 4)

    if losses >= hard_n and elapsed_h < hard_h:
        return (
            True,
            f"🚨 *硬熔斷觸發*\n連續 {losses} 次止損，系統暫停 {hard_h} 小時\n"
            f"剩餘約 `{hard_h - elapsed_h:.1f}` 小時恢復",
            losses,
        )
    if losses >= soft_n and elapsed_h < soft_h:
        return (
            True,
            f"⚠️ *軟熔斷觸發*\n連續 {losses} 次止損，暫停 {soft_h} 小時\n"
            f"剩餘約 `{soft_h - elapsed_h:.1f}` 小時恢復",
            losses,
        )
    return False, "", losses


# ═════════════════════════════════════════════════════════
# 9.85 v14.1 自動暫停爛幣 + 連敗冷靜期
# ═════════════════════════════════════════════════════════
def is_coin_underperforming(coin: str, cfg: dict) -> tuple[bool, str]:
    """⏸️ 檢查單一幣種過去 N 天是否表現太差，要自動暫停"""
    cap_cfg = cfg.get("coin_auto_pause", {})
    if not cap_cfg.get("enabled", True):
        return False, ""

    days = cap_cfg.get("days", 7)
    min_trades = cap_cfg.get("min_trades", 5)
    max_wr = cap_cfg.get("max_winrate", 0.30)

    history = _load_json(TRADE_HISTORY_FILE, [])
    cutoff_ts = time.time() - days * 86400

    recent = []
    for t in history:
        if t.get("coin") != coin:
            continue
        try:
            t_dt = datetime.strptime(t["time"], "%Y-%m-%d %H:%M").replace(tzinfo=TW_TZ)
            if t_dt.timestamp() > cutoff_ts:
                recent.append(t)
        except Exception:
            continue

    if len(recent) < min_trades:
        return False, ""

    wins = sum(1 for t in recent if t.get("close_type") in ("TP1", "TP2", "TP3", "LOCK"))
    wr = wins / len(recent)
    if wr < max_wr:
        return True, (
            f"{coin} 過去 {days} 天 {len(recent)} 筆，"
            f"勝率 `{wr:.0%}` < `{max_wr:.0%}` → 暫停 {cap_cfg.get('pause_hours', 24)}h"
        )
    return False, ""


def get_direction_bias() -> tuple[str | None, int, str]:
    """📊 從歷史交易算出多空方向偏好 → (偏好方向, 加分量, 說明)"""
    cfg = load_config()
    db_cfg = cfg.get("direction_bias", {})
    if not db_cfg.get("enabled", True):
        return None, 0, ""

    min_diff = db_cfg.get("min_diff_pct", 15)
    min_samples = db_cfg.get("min_samples", 10)
    bonus = db_cfg.get("bonus", 3)

    history = _load_json(TRADE_HISTORY_FILE, [])
    closed = [t for t in history if t.get("close_type") in ("SL", "BE", "LOCK", "TP1", "TP2", "TP3")]

    longs = [t for t in closed if t.get("side") == "LONG"]
    shorts = [t for t in closed if t.get("side") == "SHORT"]
    if len(longs) < min_samples or len(shorts) < min_samples:
        return None, 0, ""

    long_wins = sum(1 for t in longs if t["close_type"] in ("TP1", "TP2", "TP3", "LOCK"))
    short_wins = sum(1 for t in shorts if t["close_type"] in ("TP1", "TP2", "TP3", "LOCK"))
    long_wr = long_wins / len(longs) * 100
    short_wr = short_wins / len(shorts) * 100
    diff = long_wr - short_wr

    if abs(diff) < min_diff:
        return None, 0, ""
    preferred = "LONG" if diff > 0 else "SHORT"
    return preferred, bonus, f"{preferred} 歷史勝率 {long_wr if preferred == 'LONG' else short_wr:.0f}% 較高（差 {abs(diff):.0f}%）"


def format_direction_stats() -> str:
    """📊 方向勝率統計報表（給 /direction 命令用）"""
    history = _load_json(TRADE_HISTORY_FILE, [])
    closed = [t for t in history if t.get("close_type") in ("SL", "BE", "LOCK", "TP1", "TP2", "TP3")]
    longs = [t for t in closed if t.get("side") == "LONG"]
    shorts = [t for t in closed if t.get("side") == "SHORT"]

    def calc_stats(trades):
        n = len(trades)
        if n == 0:
            return None
        wins = sum(1 for t in trades if t["close_type"] in ("TP1", "TP2", "TP3", "LOCK"))
        loss = sum(1 for t in trades if t["close_type"] == "SL")
        be = sum(1 for t in trades if t["close_type"] == "BE")
        pnl = sum(t.get("pnl", 0) for t in trades)
        return {"n": n, "win": wins, "loss": loss, "be": be, "wr": wins / n * 100, "pnl": pnl}

    l = calc_stats(longs)
    s = calc_stats(shorts)

    lines = ["📊 *方向勝率統計*", "━━━━━━━━━━━━━━"]
    if l:
        lines.append(
            f"🟢 LONG：{l['n']} 筆（勝 {l['win']} / 平 {l['be']} / 敗 {l['loss']}）"
        )
        lines.append(f"   勝率 `{l['wr']:.0f}%` · PnL `{l['pnl']:+.2f}%`")
    else:
        lines.append("🟢 LONG：暫無資料")
    if s:
        lines.append(
            f"🔴 SHORT：{s['n']} 筆（勝 {s['win']} / 平 {s['be']} / 敗 {s['loss']}）"
        )
        lines.append(f"   勝率 `{s['wr']:.0f}%` · PnL `{s['pnl']:+.2f}%`")
    else:
        lines.append("🔴 SHORT：暫無資料")

    bias_dir, bias_amount, bias_note = get_direction_bias()
    if bias_dir:
        lines.append("")
        lines.append(f"🎯 *系統當前偏好：{bias_dir}*")
        lines.append(f"   {bias_note}")
        lines.append(f"   下次同方向訊號 +{bias_amount} / 反方向 -{bias_amount}")
    else:
        lines.append("")
        lines.append("⚖️ 系統未偏好方向（資料不足或勝率接近）")
    return "\n".join(lines)


def format_audit_report() -> str:
    """🔬 指標有效性審查 — 把交易切成「滿足 X 條件 vs 不滿足」對比勝率

    驗證 v14 加的所有評分項是否真的有效：
      - 神級訊號（95+）vs 一般（80-94）
      - MTF 1H 順勢 vs 中性 / 反向
      - 高量能（≥1.5×）vs 低量能（<1×）
      - 多頭 vs 空頭
      - 趨勢市 vs 震盪市
      - EMA 完美排列 vs 其他
    """
    history = _load_json(TRADE_HISTORY_FILE, [])
    closed = [
        t for t in history
        if t.get("close_type") in ("SL", "BE", "LOCK", "TP1", "TP2", "TP3", "OB_FAIL")
    ]
    n_closed = len(closed)
    if n_closed < 10:
        return (
            f"📭 *指標有效性審查*\n\n"
            f"資料不足（{n_closed} 筆 < 10）。\n"
            f"至少累積 10 筆已結束交易才能審查指標效度。"
        )

    def stats(trades):
        n = len(trades)
        if n == 0:
            return None
        wins = sum(1 for t in trades if t["close_type"] in ("TP1", "TP2", "TP3", "LOCK"))
        return {"n": n, "wr": wins / n * 100}

    def _verdict(diff_pct: float) -> str:
        if diff_pct > 10:
            return "✅"
        if diff_pct > 0:
            return "⚠️"
        return "❌"

    overall = stats(closed)
    lines = [
        f"🔬 *指標有效性審查*",
        f"━━━━━━━━━━━━━━",
        f"樣本：{n_closed} 筆已結束交易",
        f"整體勝率：`{overall['wr']:.0f}%`",
        f"",
    ]

    sections = []

    # 1️⃣ 神級訊號（95+）vs 一般（80-94）
    god = [t for t in closed if t.get("score", 0) >= 95]
    normal = [t for t in closed if 80 <= t.get("score", 0) < 95]
    if len(god) >= 2 and len(normal) >= 5:
        g = stats(god)
        nm = stats(normal)
        diff = g["wr"] - nm["wr"]
        sections.append(
            f"{_verdict(diff)} *神級訊號 vs 一般*\n"
            f"  神級（95+）：{g['n']} 筆 / `{g['wr']:.0f}%`\n"
            f"  一般（80-94）：{nm['n']} 筆 / `{nm['wr']:.0f}%`\n"
            f"  差異：`{diff:+.0f}%`"
        )

    # 2️⃣ MTF 1H 順勢 vs 中性
    mtf_aligned = [t for t in closed if (t.get("features") or {}).get("mtf_h1", 0) == 1.0]
    mtf_other = [t for t in closed if (t.get("features") or {}).get("mtf_h1", 1.0) == 0.0]
    if len(mtf_aligned) >= 3 and len(mtf_other) >= 3:
        a = stats(mtf_aligned)
        o = stats(mtf_other)
        diff = a["wr"] - o["wr"]
        sections.append(
            f"{_verdict(diff)} *MTF 1H 順勢 vs 中性*\n"
            f"  順勢：{a['n']} 筆 / `{a['wr']:.0f}%`\n"
            f"  中性：{o['n']} 筆 / `{o['wr']:.0f}%`\n"
            f"  差異：`{diff:+.0f}%`"
        )

    # 3️⃣ 高量能 vs 低量能
    high_vol = [t for t in closed if (t.get("features") or {}).get("vol_ratio", 1.0) >= 1.5]
    low_vol = [t for t in closed if (t.get("features") or {}).get("vol_ratio", 1.0) < 1.0]
    if len(high_vol) >= 3 and len(low_vol) >= 3:
        h = stats(high_vol)
        l = stats(low_vol)
        diff = h["wr"] - l["wr"]
        sections.append(
            f"{_verdict(diff)} *高量能 vs 低量能*\n"
            f"  高量（≥1.5×）：{h['n']} 筆 / `{h['wr']:.0f}%`\n"
            f"  低量（<1×）：{l['n']} 筆 / `{l['wr']:.0f}%`\n"
            f"  差異：`{diff:+.0f}%`"
        )

    # 4️⃣ ADX 趨勢市 vs 震盪市
    trend_mkt = [t for t in closed if (t.get("regime") or {}).get("regime") == "trend"]
    range_mkt = [t for t in closed if (t.get("regime") or {}).get("regime") == "range"]
    if len(trend_mkt) >= 3 and len(range_mkt) >= 3:
        t_st = stats(trend_mkt)
        r_st = stats(range_mkt)
        diff = t_st["wr"] - r_st["wr"]
        sections.append(
            f"{_verdict(diff)} *趨勢市 vs 震盪市*\n"
            f"  趨勢市（ADX>25）：{t_st['n']} 筆 / `{t_st['wr']:.0f}%`\n"
            f"  震盪市（ADX<18）：{r_st['n']} 筆 / `{r_st['wr']:.0f}%`\n"
            f"  差異：`{diff:+.0f}%`"
        )

    # 5️⃣ 多空方向
    longs = [t for t in closed if t.get("side") == "LONG"]
    shorts = [t for t in closed if t.get("side") == "SHORT"]
    if longs and shorts:
        l_st = stats(longs)
        s_st = stats(shorts)
        diff = l_st["wr"] - s_st["wr"]
        balance_emoji = "✅" if abs(diff) < 10 else "⚠️" if abs(diff) < 20 else "❌"
        sections.append(
            f"{balance_emoji} *方向平衡*\n"
            f"  LONG：{l_st['n']} 筆 / `{l_st['wr']:.0f}%`\n"
            f"  SHORT：{s_st['n']} 筆 / `{s_st['wr']:.0f}%`\n"
            f"  差異：`{diff:+.0f}%`"
        )

    if sections:
        lines.append("\n\n".join(sections))
        lines.append("")
    else:
        lines.append("各項分組樣本不足，需累積更多交易")
        lines.append("")

    lines.append("💡 *判讀標準*")
    lines.append("  ✅ = 該項指標有效（差異 > 10%）")
    lines.append("  ⚠️ = 邊際有效（差異 0–10%）")
    lines.append("  ❌ = 反向關係（建議降權或關閉）")
    return "\n".join(lines)


def is_coin_overheating(coin: str, cfg: dict) -> tuple[bool, str]:
    """🔥 過度集中保護：某幣連 N 勝後暫停一輪"""
    oh_cfg = cfg.get("overheating", {})
    if not oh_cfg.get("enabled", True):
        return False, ""

    threshold = oh_cfg.get("win_streak_threshold", 3)
    cooldown_h = oh_cfg.get("cooldown_hours", 4)

    history = _load_json(TRADE_HISTORY_FILE, [])
    coin_closed = [
        t for t in history
        if t.get("coin") == coin
        and t.get("close_type") in ("SL", "BE", "LOCK", "TP1", "TP2", "TP3")
    ]
    if len(coin_closed) < threshold:
        return False, ""

    last_n = coin_closed[-threshold:]
    if not all(t["close_type"] in ("TP1", "TP2", "TP3", "LOCK") for t in last_n):
        return False, ""

    try:
        last_win_dt = datetime.strptime(last_n[-1]["time"], "%Y-%m-%d %H:%M").replace(tzinfo=TW_TZ)
    except Exception:
        return False, ""

    elapsed_h = (tw_now() - last_win_dt).total_seconds() / 3600
    if elapsed_h < cooldown_h:
        remaining = cooldown_h - elapsed_h
        return True, (
            f"{coin} 已連 {threshold} 勝，暫停一輪避免過度依賴（剩餘 `{remaining:.1f}h`）"
        )
    return False, ""


def check_cooling_off(cfg: dict) -> tuple[bool, int, str]:
    """❄️ 連敗冷靜期 → (是否冷靜中, 剩餘秒數, 說明)"""
    co_cfg = cfg.get("cooling_off", {})
    if not co_cfg.get("enabled", True):
        return False, 0, ""

    threshold = co_cfg.get("loss_threshold", 2)
    period_min = co_cfg.get("period_minutes", 30)

    history = _load_json(TRADE_HISTORY_FILE, [])
    closed = [t for t in history if t.get("close_type") in ("SL", "BE", "LOCK", "TP1", "TP2", "TP3")]
    if len(closed) < threshold:
        return False, 0, ""

    last_n = closed[-threshold:]
    if not all(t.get("close_type") == "SL" for t in last_n):
        return False, 0, ""

    try:
        last_sl_dt = datetime.strptime(last_n[-1]["time"], "%Y-%m-%d %H:%M").replace(tzinfo=TW_TZ)
    except Exception:
        return False, 0, ""

    elapsed = (tw_now() - last_sl_dt).total_seconds()
    cooling_sec = period_min * 60
    if elapsed < cooling_sec:
        remaining = int(cooling_sec - elapsed)
        return True, remaining, (
            f"連 {threshold} 敗冷靜期，剩餘 `{remaining // 60}` 分鐘後恢復開新單"
        )
    return False, 0, ""


# ═════════════════════════════════════════════════════════
# 9.87 v14.6 嚴格每日風控
# ═════════════════════════════════════════════════════════
def get_today_stats() -> dict:
    """📊 今日交易統計（依台灣時間日期）"""
    today_str = tw_now().strftime("%Y-%m-%d")
    history = _load_json(TRADE_HISTORY_FILE, [])
    today_trades = [t for t in history if t.get("date") == today_str]
    closed = [
        t for t in today_trades
        if t.get("close_type") in ("SL", "BE", "LOCK", "TP1", "TP2", "TP3", "OB_FAIL")
    ]
    return {
        "trades_count": len(today_trades),
        "closed_count": len(closed),
        "pnl_pct": sum(t.get("pnl", 0) for t in closed),
        "wins": sum(1 for t in closed if t.get("close_type") in ("TP1", "TP2", "TP3", "LOCK")),
        "losses": sum(1 for t in closed if t.get("close_type") == "SL"),
    }


def check_daily_limits(cfg: dict, tracker) -> tuple[bool, str]:
    """🛡️ 每日風控檢查 → (是否暫停, 訊息)

    三條紅線：
      1. 同時持倉數上限（max_concurrent_positions）
      2. 當日損失上限（daily_loss_limit_pct）
      3. 每日訊號數上限（max_daily_signals）
    """
    dl_cfg = cfg.get("daily_limits", {})
    if not dl_cfg.get("enabled", True):
        return False, ""

    stats = get_today_stats()

    # ① 同時持倉數
    max_concurrent = dl_cfg.get("max_concurrent_positions", 2)
    open_count = sum(
        1 for s in tracker.signals.values()
        if s.get("status") in ("PENDING", "ACTIVE", "BE", "TRAIL")
    )
    if open_count >= max_concurrent:
        return True, (
            f"📦 持倉數已達上限：開單中 *{open_count}* / 上限 *{max_concurrent}*"
        )

    # ② 當日累計損失
    loss_limit = dl_cfg.get("daily_loss_limit_pct", 5.0)
    if stats["pnl_pct"] < -loss_limit:
        return True, (
            f"⚠️ 當日 PnL `{stats['pnl_pct']:.2f}%` 已跌破停損紅線 `-{loss_limit}%`"
            f"（{stats['losses']} 敗 / {stats['wins']} 勝），停止開新單到隔天"
        )

    # ③ 每日訊號數
    max_daily = dl_cfg.get("max_daily_signals", 6)
    if stats["trades_count"] >= max_daily:
        return True, (
            f"📊 今日訊號數 *{stats['trades_count']}* 已達上限 *{max_daily}*，停止開新單"
        )

    return False, ""


# ═════════════════════════════════════════════════════════
# 9.9 關鍵時段過濾
# ═════════════════════════════════════════════════════════
def _in_window(cur_min: int, start_min: int, end_min: int) -> bool:
    """支援跨午夜時段（如 23:50–00:10）"""
    if start_min <= end_min:
        return start_min <= cur_min < end_min
    return cur_min >= start_min or cur_min < end_min


def is_in_news_window(cfg: dict) -> tuple[bool, str]:
    """📰 新聞事件時段檢查（自訂事件 + NFP 自動規則）"""
    now = tw_now()

    # 1. config 中的自訂事件
    for nb in cfg.get("news_blackouts", []):
        try:
            start = datetime.fromisoformat(nb["start"])
            end = datetime.fromisoformat(nb["end"])
            if start.tzinfo is None:
                start = start.replace(tzinfo=TW_TZ)
                end = end.replace(tzinfo=TW_TZ)
            if start <= now <= end:
                return True, nb.get("reason", "新聞事件")
        except Exception:
            continue

    # 2. NFP 自動規則：每月第一個週五 21:25–22:30（台灣時間）
    auto = cfg.get("auto_news_blackout", {})
    if auto.get("nfp", True):
        if now.weekday() == 4 and now.day <= 7:
            cur = now.hour * 60 + now.minute
            if 21 * 60 + 25 <= cur < 22 * 60 + 30:
                return True, "NFP 非農（自動偵測）"

    # 3. CPI 約莫每月中旬 21:25–22:30
    if auto.get("cpi", True):
        if 10 <= now.day <= 16:
            cur = now.hour * 60 + now.minute
            if 21 * 60 + 25 <= cur < 22 * 60 + 30:
                return True, "CPI 數據時段（自動偵測）"

    return False, ""


def is_blackout_time(cfg: dict) -> tuple[bool, str]:
    """🕒 檢查當前是否在禁止交易時段（台灣時間）"""
    windows = cfg.get("blackout_windows_tw", [])
    now = tw_now()
    cur_min = now.hour * 60 + now.minute
    for w in windows:
        try:
            sh, sm = map(int, w["start"].split(":"))
            eh, em = map(int, w["end"].split(":"))
            if _in_window(cur_min, sh * 60 + sm, eh * 60 + em):
                return True, w.get("reason", "禁止時段")
        except Exception:
            continue
    return False, ""


# ═════════════════════════════════════════════════════════
# 10. 訊號追蹤
# ═════════════════════════════════════════════════════════
class SignalTracker:
    def __init__(self, filepath: str = ACTIVE_SIGNALS_FILE):
        self.filepath = filepath
        self.signals: dict = _load_json(filepath, {})
        self.transitions = 0

    def _save(self) -> None:
        _save_json(self.filepath, self.signals)

    def add(self, signal: dict, active: bool = False) -> tuple[str, str]:
        """新增訊號 → 回傳 (key, order_id)"""
        order_id = f"{int(time.time())}-{uuid.uuid4().hex[:8].upper()}"
        key = f"{signal['instId']}_{signal['side']}_{order_id}"
        now_ts = time.time()
        self.signals[key] = {
            **signal,
            "order_id": order_id,
            "status": "ACTIVE" if active else "PENDING",
            "hit_tp1": False,
            "hit_tp2": False,
            "hit_tp3": False,
            "activated_at": now_ts if active else None,
            "entry_message_id": None,
            # 🪡 歷史插針補抓的游標（秒）：下次 _check_one 從這之後的 K 線開始掃
            "last_checked_ts": now_ts if active else None,
        }
        self._save()
        logging.info(f"📌 新增訂單：{order_id} ({signal['instId']} {signal['side']})")
        return key, order_id

    def set_entry_message_id(self, key: str, message_id: int | None) -> None:
        if key in self.signals and message_id:
            self.signals[key]["entry_message_id"] = message_id
            self._save()

    def _send_postmortem(self, sig: dict, mode: str) -> None:
        """🔍 SL/BE/LOCK/OB_FAIL 後送覆盤分析訊息（不再靜默失敗）"""
        coin = sig.get("instId", "?").split("-")[0]
        order_id = sig.get("order_id", "?")

        try:
            cfg = load_config()
            pm_cfg = cfg.get("post_mortem", {})
            if not pm_cfg.get("enabled", True):
                return
            if mode == "LOCK" and pm_cfg.get("loss_only", False):
                return  # LOCK 不算敗，loss_only 模式下跳過

            activated_at = sig.get("activated_at") or sig.get("created") or 0
            all_candles = fetch_candles_full(sig["instId"], limit=100)
            df_at_loss = [
                {"ts": c["ts"], "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c["v"]}
                for c in all_candles
                if (c["ts"] / 1000) >= (activated_at - 900)
            ]

            # 🛡️ 資料不足也要送，避免「為什麼沒原因？」
            if len(df_at_loss) < 10:
                send_tg(
                    f"🔍 *{coin} 覆盤*\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"🆔 訂單：`{order_id}`\n"
                    f"⏰ 時間：{tw_ts()}\n"
                    f"\n"
                    f"📋 *結論：進場後資料太少（僅 {len(df_at_loss)} 根 K 線）*\n"
                    f"\n"
                    f"💡 可能原因：\n"
                    f"  • 訊號剛開沒多久就被插針掃損\n"
                    f"  • 進場時間距現在 < 15 分鐘\n"
                    f"\n"
                    f"建議手動翻 K 線看是哪根 K 觸發 SL，並注意是否高波動時段",
                    reply_to_message_id=sig.get("entry_message_id"),
                )
                return

            reasons = analyze_loss(sig, df_at_loss)
            lessons = _generate_lessons(reasons)
            similar = get_similar_stats(
                sig.get("score", 0),
                sig["side"],
                sig.get("detail", {}),
                sig.get("funding_rate"),
                coin,
            )

            msg = _fmt_postmortem(sig, mode, reasons, lessons, similar)
            send_tg(msg, reply_to_message_id=sig.get("entry_message_id"))

            if mode == "LOSS":
                record_loss_reason(coin, sig["side"], reasons)
        except Exception as e:
            logging.error(f"❌ 覆盤分析失敗：{e}")
            # 🛡️ 例外也送 fallback，不再靜默
            try:
                send_tg(
                    f"🔍 *{coin} 覆盤錯誤*\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"🆔 訂單：`{order_id}`\n"
                    f"\n"
                    f"⚠️ 覆盤分析發生例外：`{str(e)[:120]}`\n"
                    f"\n"
                    f"訂單已正常平倉，請手動檢視 K 線。",
                    reply_to_message_id=sig.get("entry_message_id"),
                )
            except Exception:
                pass

    def has_open_position(self, instId: str) -> bool:
        """🔒 該幣種是否還有未結束的訊號（PENDING / ACTIVE / BE / TRAIL）

        用途：避免在平倉前對同一幣種重複開倉。
        """
        for sig in self.signals.values():
            if sig.get("instId") == instId and sig.get("status") in (
                "PENDING", "ACTIVE", "BE", "TRAIL"
            ):
                return True
        return False

    def check_all(self) -> None:
        """檢查所有訊號並發送通知"""
        self.transitions = 0
        to_remove = []
        for key, sig in list(self.signals.items()):
            if self._check_one(key, sig):
                to_remove.append(key)
        for key in to_remove:
            del self.signals[key]
        if to_remove:
            self._save()

    def _check_one(self, key: str, sig: dict) -> bool:
        """檢查單一訊號 → True 代表結束（要從追蹤移除）

        v12.2：歷史 K 線補抓版
          - PENDING：價格進入觸發區間時轉 ACTIVE
          - ACTIVE/BE/TRAIL：抓 last_checked_ts 之後所有 K 線，依時序逐根處理
            ↳ 每根 K 線檢查 TP1 → TP2 → TP3 → SL（SL 用更新後的值）
            ↳ 即便 cron 漏跑、訊號活了 3 小時才檢查，歷史插針也不會漏
          - SL 觸發時依狀態自動分類：止損(LOSS) / 保本(BE) / 鎖利(LOCK)
        """
        try:
            price = fetch_price(sig["instId"])
            if price <= 0:
                return False

            sig["current_price"] = price
            status = sig["status"]

            # ── PENDING：等待進場 ──
            if status == "PENDING":
                return self._check_pending(sig, price)

            if status not in ("ACTIVE", "BE", "TRAIL"):
                return False

            # ── 抓 last_checked_ts 之後的所有 K 線，依時序處理 ──
            all_candles = fetch_candles_full(sig["instId"])
            last_ts_s = (
                sig.get("last_checked_ts")
                or sig.get("activated_at")
                or sig.get("created")
                or 0
            )
            last_ts_ms = int(last_ts_s * 1000)
            new_candles = [c for c in all_candles if c["ts"] > last_ts_ms]

            # 🪙 v14.7：即時價合併進最後一根 K 線（OKX K 線 API 偶有 5–10 秒延遲，
            # 即時 ticker 比 K 線更快反映剛剛的插針）
            if new_candles and price > 0:
                last = dict(new_candles[-1])
                last["h"] = max(last["h"], price)
                last["l"] = min(last["l"], price)
                new_candles[-1] = last

            for c in new_candles:
                if self._process_candle(sig, c):
                    return True

            # 把游標推進到最後一根「已收線」K 線（未收線下次再掃）
            confirmed = [c for c in new_candles if c["confirmed"]]
            if confirmed:
                sig["last_checked_ts"] = max(c["ts"] for c in confirmed) / 1000.0

            self._save()
            return False
        except Exception as e:
            logging.error(f"❌ check_one [{key}] 錯誤：{e}")
            return False

    def _check_pending(self, sig: dict, price: float) -> bool:
        """PENDING 狀態檢查：等待價格進入區間轉 ACTIVE，過期自動取消"""
        coin = sig["instId"].split("-")[0]
        order_id = sig.get("order_id", "N/A")
        side = sig["side"]
        entry, sl = sig["entry"], sig["sl"]
        tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
        kb = _order_keyboard(order_id)

        if time.time() > sig["expires"]:
            send_tg(
                f"⏰ *{coin} 訊號過期*\n"
                f"🆔 訂單：`{order_id}`\n"
                f"進場 `{entry:.4f}` 未觸發，已自動取消"
            )
            self.transitions += 1
            return True

        in_zone = (
            side == "LONG"
            and entry * (1 - 0.006) <= price <= entry * (1 + 0.002)
        ) or (
            side == "SHORT"
            and entry * (1 - 0.002) <= price <= entry * (1 + 0.006)
        )
        if in_zone:
            now_ts = time.time()
            sig["status"] = "ACTIVE"
            sig["activated_at"] = now_ts
            sig["last_checked_ts"] = now_ts
            msg_id = send_tg(
                _fmt_entry(
                    coin, side, order_id, price, entry, sl,
                    tp1, tp2, tp3, sig["score"], sig.get("funding_rate"),
                    detail=sig.get("detail"),
                ),
                reply_markup=kb,
            )
            if msg_id:
                sig["entry_message_id"] = msg_id
            self._save()
            self.transitions += 1
        return False

    def _process_candle(self, sig: dict, candle: dict) -> bool:
        """對單一 K 線檢查 TP1 → TP2 → TP3 → SL → True 代表訊號結束

        - 用 K 線的 high / low 作極值（自然涵蓋插針）
        - 多 TP 在同一根 K 線都觸到時，依序更新 SL（TP1→保本、TP2→鎖利）
        - 處理完所有 TP 後，再用「最終 SL 值」檢查 SL 是否觸發
        """
        side = sig["side"]
        entry = sig["entry"]
        sl = sig["sl"]
        tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
        coin = sig["instId"].split("-")[0]
        order_id = sig.get("order_id", "N/A")
        reply_to = sig.get("entry_message_id")
        kb = _order_keyboard(order_id)
        ch, cl, cc = candle["h"], candle["l"], candle["c"]

        if side == "LONG":
            favor_hit = lambda t: ch >= t
            against_hit = lambda t: cl <= t
            wick_favor = lambda t: cc < t and ch >= t        # 收盤未到、影線觸及
            wick_against = lambda t: cc > t and cl <= t      # 收盤未破、影線插針
        else:
            favor_hit = lambda t: cl <= t
            against_hit = lambda t: ch >= t
            wick_favor = lambda t: cc > t and cl <= t
            wick_against = lambda t: cc < t and ch >= t

        # 🧱 OB 失效退場（僅在還沒到 TP1 前檢查）
        if not sig.get("hit_tp1"):
            ob_zone = sig.get("ob_zone")
            cfg_oi = load_config().get("ob_invalidation", {})
            if ob_zone and cfg_oi.get("enabled", True):
                buf = cfg_oi.get("break_buffer_pct", 0.2) / 100
                ob_low = ob_zone.get("low", 0)
                ob_high = ob_zone.get("high", 0)
                # LONG：OB low 收盤被擊穿 → 失效
                # SHORT：OB high 收盤被突破 → 失效
                broken = (
                    side == "LONG" and cc < ob_low * (1 - buf)
                ) or (
                    side == "SHORT" and cc > ob_high * (1 + buf)
                )
                if broken:
                    pnl = (
                        (cc - entry) / entry * 100
                        if side == "LONG"
                        else (entry - cc) / entry * 100
                    )
                    send_tg(
                        f"⚠️ *{coin} OB 失效，主動退場*\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"🆔 訂單：`{order_id}`\n"
                        f"⏰ 時間：{tw_ts()}\n"
                        f"方向：{'做多' if side == 'LONG' else '做空'}\n"
                        f"退場價：`{cc:.4f}`\n"
                        f"結算：`{pnl:+.2f}%`\n"
                        f"\n"
                        f"💡 進場依據的 SMC 訂單塊已被收盤跌破\n"
                        f"   結構失效，提前退場避免擴大虧損",
                        reply_markup=kb,
                        reply_to_message_id=reply_to,
                    )
                    record_trade(coin, side, order_id, entry, cc, "OB_FAIL", sig["score"], sig)
                    # 🔍 OB 失效也送覆盤分析
                    self._send_postmortem(sig, "LOSS")
                    self.transitions += 1
                    return True

        # 🥇 TP1
        if not sig.get("hit_tp1") and favor_hit(tp1):
            sig["hit_tp1"] = True
            sig["sl"] = entry
            sig["status"] = "BE"
            sl = entry
            pnl = (
                (tp1 - entry) / entry * 100
                if side == "LONG"
                else (entry - tp1) / entry * 100
            )
            send_tg(
                _fmt_tp(
                    coin, side, order_id, "TP1", tp1, pnl, 1.5,
                    wick_triggered=wick_favor(tp1),
                ),
                reply_markup=kb,
                reply_to_message_id=reply_to,
            )
            record_trade(coin, side, order_id, entry, tp1, "TP1", sig["score"], sig)
            self._save()
            self.transitions += 1

        # 🥈 TP2
        if not sig.get("hit_tp2") and favor_hit(tp2):
            sig["hit_tp2"] = True
            sig["sl"] = tp1
            sig["status"] = "TRAIL"
            sl = tp1
            pnl = (
                (tp2 - entry) / entry * 100
                if side == "LONG"
                else (entry - tp2) / entry * 100
            )
            send_tg(
                _fmt_tp(
                    coin, side, order_id, "TP2", tp2, pnl, 3.0,
                    wick_triggered=wick_favor(tp2),
                ),
                reply_markup=kb,
                reply_to_message_id=reply_to,
            )
            record_trade(coin, side, order_id, entry, tp2, "TP2", sig["score"], sig)
            self._save()
            self.transitions += 1

        # 🏆 TP3 → 結束
        if not sig.get("hit_tp3") and favor_hit(tp3):
            sig["hit_tp3"] = True
            pnl = (
                (tp3 - entry) / entry * 100
                if side == "LONG"
                else (entry - tp3) / entry * 100
            )
            send_tg(
                _fmt_tp(
                    coin, side, order_id, "TP3", tp3, pnl, 5.0,
                    wick_triggered=wick_favor(tp3),
                ),
                reply_markup=kb,
                reply_to_message_id=reply_to,
            )
            record_trade(coin, side, order_id, entry, tp3, "TP3", sig["score"], sig)
            self.transitions += 1
            return True

        # 🛑 SL（用更新後的 sl 值）→ 依狀態分類
        if against_hit(sl):
            if sig.get("hit_tp2"):
                mode, r_value, close_type = "LOCK", 1.5, "LOCK"
            elif sig.get("hit_tp1"):
                mode, r_value, close_type = "BE", 0.0, "BE"
            else:
                mode, r_value, close_type = "LOSS", -1.0, "SL"
            pnl = (
                (sl - entry) / entry * 100
                if side == "LONG"
                else (entry - sl) / entry * 100
            )
            send_tg(
                _fmt_sl(
                    coin, side, order_id, sl, pnl, mode, r_value,
                    wick_triggered=wick_against(sl),
                ),
                reply_markup=kb,
                reply_to_message_id=reply_to,
            )
            record_trade(coin, side, order_id, entry, sl, close_type, sig["score"], sig)
            # 🔍 覆盤分析
            self._send_postmortem(sig, mode)
            self.transitions += 1
            return True

        return False

    def send_position_updates(self) -> None:
        """📊 發送所有持倉的進度更新

        v14.7 加 15 分鐘 throttle：每 15 分鐘最多送一次，
        避免 1 分鐘 cron 把 TP/SL 達標等重要訊息洗到上面去。
        """
        state = get_system_state()
        now = time.time()
        last_sent = state.get("last_position_update_ts", 0)
        interval = 15 * 60  # 15 分鐘
        if now - last_sent < interval:
            return

        cnt = 0
        for sig in self.signals.values():
            if sig["status"] not in ("ACTIVE", "BE", "TRAIL"):
                continue
            price = fetch_price(sig["instId"])
            if price <= 0:
                continue
            send_tg(
                _fmt_position(sig, price),
                reply_markup=_order_keyboard(sig.get("order_id", "")),
                reply_to_message_id=sig.get("entry_message_id"),
            )
            cnt += 1
        if cnt:
            state["last_position_update_ts"] = now
            set_system_state(state)
            logging.info(f"📊 已發送 {cnt} 筆持倉更新（下次最早 15 分鐘後）")

    def get_position_stats(self) -> str:
        """📋 持倉統計（給 /stats 命令用）"""
        positions = list(self.signals.values())
        if not positions:
            return "📭 *目前無持倉*\n\n🔄 系統持續掃描中..."

        lines = [f"📊 *追蹤中訊號（{len(positions)} 筆）*", "═" * 22, ""]
        for i, p in enumerate(positions):
            price = fetch_price(p["instId"]) or p["entry"]
            coin = p["instId"].split("-")[0]
            coin_emoji = (
                "🟠" if "BTC" in p["instId"] else "🔷" if "ETH" in p["instId"] else "🟣"
            )
            side_emoji = "🟢" if p["side"] == "LONG" else "🔴"
            order_id = p.get("order_id", "N/A")
            pnl = (
                (price - p["entry"]) / p["entry"] * 100
                if p["side"] == "LONG"
                else (p["entry"] - price) / p["entry"] * 100
            )
            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            progress = (
                "🏆 TP3"
                if p.get("hit_tp3")
                else "🥈 TP2"
                if p.get("hit_tp2")
                else "🥇 TP1"
                if p.get("hit_tp1")
                else "⏳ 等待"
            )
            lines.append(
                f"{coin_emoji} *#{coin}* · {side_emoji} {p['side']} · {p.get('score', 0)} 分\n"
                f"🆔 訂單：`{order_id}`\n"
                f"狀態：{p['status']}\n"
                f"當前 `{price:.4f}` {pnl_emoji}{pnl:+.2f}%\n"
                f"進場 `{p['entry']:.4f}` · 止損 `{p['sl']:.4f}`\n"
                f"TP1 `{p['tp1']:.4f}` · TP2 `{p['tp2']:.4f}` · TP3 `{p['tp3']:.4f}`\n"
                f"進度：{progress}"
            )
            if i < len(positions) - 1:
                lines.append("─" * 22)
        return "\n".join(lines)


# ═════════════════════════════════════════════════════════
# 11. 主掃描
# ═════════════════════════════════════════════════════════
def run_monitor(tracker: SignalTracker, in_run_polls: int = 1, poll_interval: int = 30) -> None:
    """🔔 高頻監控模式 — 只檢查既有訊號的 PENDING 進場 / TP / SL，不生成新訊號

    in_run_polls: 一次 cron 執行內輪詢幾次（搭配 poll_interval 秒間隔）
      預設 1 次 = 純靠 cron 頻率；設成 3 + interval=20 → 一次 cron 內 1 分鐘內掃 3 次

    用法：python main.py monitor
    建議搭配 monitor-only.yml workflow（每 3 分鐘 cron）
    """
    if not tracker.signals:
        logging.info("📭 無追蹤中訊號，monitor 跳過")
        return

    n = len(tracker.signals)
    logging.info(f"🔔 monitor 模式啟動，追蹤中 {n} 筆訊號 × {in_run_polls} 輪")

    total_transitions = 0
    for poll_idx in range(in_run_polls):
        if not tracker.signals:
            logging.info("📭 所有訊號已結束，提早收工")
            break
        try:
            tracker.check_all()
            total_transitions += tracker.transitions
            if poll_idx < in_run_polls - 1:
                time.sleep(poll_interval)
        except Exception as e:
            logging.error(f"❌ monitor poll {poll_idx + 1} 出錯：{e}")

    logging.info(f"✅ monitor 完成，{in_run_polls} 輪共觸發 {total_transitions} 次狀態變動")


def run_scan(tracker: SignalTracker) -> int:
    """🔍 執行掃描（整合 v12 全部風控）"""
    logging.info("🚀 開始掃描...")

    # 🩺 健康監控（先檢查自己是否異常）
    unhealthy, health_msg = check_health()
    if unhealthy:
        send_tg(health_msg)

    # ── 0. 熱載入配置 ──
    cfg = load_config()
    coins = cfg.get("coins", ALL_COINS)
    max_signals = cfg.get("max_signals", MAX_SIGNALS)
    score_thr = cfg.get("score_threshold", SCORE_THRESHOLD)
    cooldown_h = cfg.get("cooldown_hours", COOLDOWN_HOURS)
    expire_h = cfg.get("signal_expire_hours", SIGNAL_EXPIRE_HOURS)
    atr_max = cfg.get("atr_max_pct", 0.04)
    pv_cfg = cfg.get("price_verification", {})
    pv_enabled = pv_cfg.get("enabled", True)
    pv_max_dev = pv_cfg.get("max_deviation_pct", 0.5)
    pv_block_unverified = pv_cfg.get("block_on_unverified", False)

    state = get_system_state()

    # ── 1. 連續虧損熔斷 ──
    paused, msg, losses = check_circuit_breaker(cfg)
    if paused:
        if not state.get("circuit_active"):
            send_tg(msg)
            state["circuit_active"] = True
            state["circuit_since"] = time.time()
            set_system_state(state)
        logging.warning(f"🛑 熔斷中（連敗 {losses}）→ 仍持續監控既有訊號")
        # 熔斷期間不開新單，但要繼續追既有單
        tracker.check_all()
        tracker.send_position_updates()
        return 0
    else:
        if state.get("circuit_active"):
            send_tg("✅ *熔斷已解除*\n系統恢復正常掃描，繼續加油 🚀")
            state["circuit_active"] = False
            state["circuit_since"] = None
            set_system_state(state)

    # ── 2. 關鍵時段過濾 ──
    blocked, btime_reason = is_blackout_time(cfg)
    if blocked:
        logging.info(f"🕒 禁止交易時段（{btime_reason}），不開新單但繼續監控")
        tracker.check_all()
        tracker.send_position_updates()
        return 0

    # ── 2.5 新聞事件時段過濾 ──
    in_news, news_reason = is_in_news_window(cfg)
    if in_news:
        logging.info(f"📰 新聞事件時段（{news_reason}），不開新單但繼續監控")
        tracker.check_all()
        tracker.send_position_updates()
        return 0

    # ── 2.7 連敗冷靜期 ──
    cooling, remaining_sec, cool_msg = check_cooling_off(cfg)
    if cooling:
        logging.info(f"❄️ {cool_msg}")
        tracker.check_all()
        tracker.send_position_updates()
        return 0

    # ── 2.8 🛡️ 每日風控紅線（持倉數 / 累計損失 / 訊號數）──
    limit_hit, limit_msg = check_daily_limits(cfg, tracker)
    if limit_hit:
        logging.info(f"🛡️ {limit_msg}")
        tracker.check_all()
        tracker.send_position_updates()
        return 0

    # ── 2.9 ⚡ 早期退出：所有幣種都不可開單時跳過重 API 呼叫 ──
    eligible_coins = []
    for instId in coins:
        if tracker.has_open_position(instId):
            continue
        if is_cooling(instId, cooldown_h):
            continue
        cn = instId.split("-")[0]
        if is_coin_underperforming(cn, cfg)[0]:
            continue
        if is_coin_overheating(cn, cfg)[0]:
            continue
        eligible_coins.append(instId)

    if not eligible_coins:
        logging.info(f"📭 所有 {len(coins)} 幣種都不可開單（冷卻 / 持倉 / 暫停），僅跑監控")
        tracker.check_all()
        tracker.send_position_updates()
        reset_failure_count()
        return 0

    # ── 3. 掃描可開單的幣種（已篩選過冷卻 / 持倉 / 暫停 / 過熱）──
    sent = 0
    logging.info(f"🎯 可開單幣種：{len(eligible_coins)} 個 → {[c.split('-')[0] for c in eligible_coins]}")
    for instId in eligible_coins:
        if sent >= max_signals:
            break
        coin_name = instId.split("-")[0]

        try:
            okx_price = fetch_price(instId)
            if okx_price <= 0:
                logging.warning(f"[{instId}] 無法取得 OKX 價格")
                continue

            # 3.3 📡 TradingView 第二來源驗證
            if pv_enabled:
                ok, tv_price, diff = verify_price(
                    instId, okx_price, pv_max_dev, pv_block_unverified
                )
                if not ok:
                    if tv_price is None:
                        logging.warning(f"[{instId}] TV 無法驗證，根據設定擋下")
                    else:
                        send_tg(
                            f"⚠️ *{instId.split('-')[0]} 價格異常*\n"
                            f"OKX `{okx_price:.4f}` vs TV `{tv_price:.4f}`\n"
                            f"偏離 `{diff:.3f}%` > 閾值 `{pv_max_dev}%`\n"
                            f"⏸ 本輪跳過該幣種"
                        )
                    continue

            df = fetch_candles(instId)
            if df is None:
                continue

            funding = fetch_funding_rate(instId)
            signal = generate_signal(
                instId,
                df,
                okx_price,
                funding,
                score_threshold=score_thr,
                atr_max_pct=atr_max,
                signal_expire_hours=expire_h,
            )
            if not signal:
                continue

            in_zone = (
                signal["side"] == "LONG"
                and signal["entry"] * (1 - 0.006) <= okx_price <= signal["entry"] * (1 + 0.002)
            ) or (
                signal["side"] == "SHORT"
                and signal["entry"] * (1 - 0.002) <= okx_price <= signal["entry"] * (1 + 0.006)
            )

            key, order_id = tracker.add(signal, active=in_zone)

            if in_zone:
                msg = _fmt_entry(
                    coin=instId.split("-")[0],
                    side=signal["side"],
                    order_id=order_id,
                    price=okx_price,
                    entry=signal["entry"],
                    sl=signal["sl"],
                    tp1=signal["tp1"],
                    tp2=signal["tp2"],
                    tp3=signal["tp3"],
                    score=signal["score"],
                    funding_rate=funding,
                    detail=signal.get("detail"),
                )
                msg_id = send_tg(msg, reply_markup=_order_keyboard(order_id))
                tracker.set_entry_message_id(key, msg_id)
                logging.info(f"✅ {instId} 進場通知已送出，訂單 {order_id}")
            else:
                send_tg(
                    f"📍 *{instId.split('-')[0]} 訊號就位*\n"
                    f"🆔 訂單：`{order_id}`\n"
                    f"⏰ 時間：{tw_ts()}\n"
                    f"方向：{'做多' if signal['side'] == 'LONG' else '做空'}\n"
                    f"進場價：`{signal['entry']:.4f}`（當前 `{okx_price:.4f}`）\n"
                    f"評分：{signal['score']} 分\n\n"
                    f"💡 進入有效區間後會自動觸發進場通知",
                    reply_markup=_order_keyboard(order_id),
                )
                logging.info(f"📍 {instId} PENDING 訊號已建立，訂單 {order_id}")

            mark_cooldown(instId, cooldown_h)
            sent += 1
        except Exception as e:
            logging.error(f"[{instId}] 掃描失敗：{e}")
            continue

    # ── 4. 既有訊號檢查 + 持倉更新 ──
    tracker.check_all()
    tracker.send_position_updates()

    logging.info(f"✅ 掃描完成，本輪新增 {sent} 筆訊號")
    # 🩺 重置失敗計數
    reset_failure_count()
    return sent


# ═════════════════════════════════════════════════════════
# 12. 主入口
# ═════════════════════════════════════════════════════════
def main() -> None:
    try:
        logging.info("=" * 50)
        logging.info("🤖 Alpha Oracle Pro v11.0 啟動")
        logging.info(f"⏰ 台灣時間：{tw_ts()}")
        logging.info("=" * 50)

        tracker = SignalTracker(ACTIVE_SIGNALS_FILE)

        # 命令處理
        if len(sys.argv) > 1:
            cmd = sys.argv[1]
            if cmd in ("/stats", "/持倉", "stats"):
                send_tg(tracker.get_position_stats())
                return
            if cmd in ("/learning", "/學習", "/coach", "learning"):
                send_tg(format_learning_report())
                return
            if cmd in ("/daily", "/日報", "daily"):
                date = sys.argv[2] if len(sys.argv) > 2 else None
                send_tg(format_daily_report(date))
                return
            if cmd in ("/monthly", "/月報", "monthly"):
                ym = sys.argv[2] if len(sys.argv) > 2 else None
                send_tg(format_monthly_report(ym))
                return
            if cmd in ("/direction", "/方向", "direction"):
                send_tg(format_direction_stats())
                return
            if cmd in ("/audit", "/審查", "audit"):
                send_tg(format_audit_report())
                return
            if cmd in ("monitor", "/monitor", "/監控"):
                # 高頻輕量監控模式（只追既有訊號）
                # 可選：python main.py monitor 3 20 → 一次 cron 內掃 3 次、每次間隔 20s
                polls = int(sys.argv[2]) if len(sys.argv) > 2 else 1
                interval = int(sys.argv[3]) if len(sys.argv) > 3 else 30
                run_monitor(tracker, in_run_polls=polls, poll_interval=interval)
                return

        run_scan(tracker)
        logging.info("🎉 程式執行完成")

    except Exception as e:
        logging.error(f"🔥 系統錯誤：{e}")
        # 🩺 失敗計數 +1（觸發健康警報）
        try:
            increment_failure_count()
        except Exception:
            pass
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
