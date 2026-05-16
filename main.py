#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro v15.0 — 數據精準升級版（繁體中文）
══════════════════════════════════════════════════════════════════════
✨ v15.0 精準升級（在 v14.0 核心上疊加）：

📐 指標精度：
  · RSI  → Wilder EMA 平滑（消除簡化版誤差，與 TradingView 一致）
  · ATR  → Wilder EMA 平滑（正統 Wilder 法，而非簡單均值）
  · ADX  → 完整 Wilder DI 平滑（+DM/-DM/TR 三路 EMA，更貼近實際值）

📍 S/R 精準定位：
  · Classic Pivot Points（PP/R1-R3/S1-S3）取代純極值
  · Fibonacci 回調位（23.6%/38.2%/50%/61.8%/78.6%）自動識別
  · 雙來源 S/R 融合後排序，TP/SL 落點更精準

📊 成交量深化：
  · OBV（量價趨勢）：量升價漲 → 額外確認趨勢方向
  · VWAP（成交量加權均價）：價格在 VWAP 上下方判斷多空優勢

🎯 進出場強化：
  · Bollinger Bands Squeeze：帶寬收窄偵測即將爆發行情
  · RSI 背離偵測：Regular + Hidden Divergence，頂底反轉警示
  · TP 分批出場比例：通知附帶建議（TP1 30%、TP2 30%、TP3 40%）

🔧 Bug 修正：
  · 統一私有函式命名（_save_json / _load_json / _order_keyboard）
  · SignalTracker.__init__ 命名修正
  · _bucket_session_tw / _bucket_rsi 命名統一

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
    return datetime.now(TW_TZ)

def tw_ts() -> str:
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

TG_TOKEN  = _get_env("TG_TOKEN")
CHAT_ID   = _get_env("CHAT_ID")

ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",  "BNB-USDT-SWAP",
    "XRP-USDT-SWAP", "DOGE-USDT-SWAP","ADA-USDT-SWAP",  "AVAX-USDT-SWAP",
    "LINK-USDT-SWAP","DOT-USDT-SWAP", "TON-USDT-SWAP",  "NEAR-USDT-SWAP",
]

MAX_SIGNALS          = _get_env_int("MAX_SIGNALS", 3)
SCORE_THRESHOLD      = _get_env_int("SETUP_SCORE_THRESHOLD", 75)
SIGNAL_EXPIRE_HOURS  = 24
COOLDOWN_HOURS       = 2
ACTIVE_SIGNALS_FILE  = "active_signals.json"
TRADE_HISTORY_FILE   = "trade_history.json"
COOLDOWN_FILE        = "signal_cooldown.json"
CONFIG_FILE          = "config.json"
SYSTEM_STATE_FILE    = "system_state.json"
LEARNING_FILE        = "learning_state.json"

_price_cache: dict = {}

# ═════════════════════════════════════════════════════════
# 1.5 預設配置
# ═════════════════════════════════════════════════════════
DEFAULT_CONFIG: dict = {
    "coins": ALL_COINS,
    "max_signals": 3,
    "score_threshold": 75,
    "cooldown_hours": 2,
    "daily_max_trades": 15,
    # 🔴 每日最大虧損熔斷（-3% 停止新倉）
    "daily_loss_limit": {
        "enabled": True,
        "max_loss_pct": -3.0,
    },
    # 📅 時段勝率分層（樣本足夠後自動提高低勝率時段門檻）
    "session_wr_filter": {
        "enabled": True,
        "min_samples": 10,
        "low_wr_boost": 8,
        "mid_wr_boost": 5,
    },
    # 📈 週線趨勢鎖定（只做與週線 Supertrend 同向的訊號）
    "weekly_trend_filter": {
        "enabled": True,
        "score_penalty": 12,
        "block_hard": False,
    },
    "win_rate_guardian": {
        "enabled": True,
        "min_wr": 0.70,
        "lookback": 20,
        "threshold_boost": 5,
    },
    "signal_expire_hours": 24,
    "atr_max_pct": 0.04,
    "post_mortem": {"enabled": True, "loss_only": False},
    "learning": {
        "enabled": True,
        "knn_enabled": True,
        "min_samples": 5,
        "max_score_adjust": 10,
    },
    "news_blackouts": [],
    "auto_news_blackout": {"nfp": True, "cpi": True},
    "price_verification": {
        "enabled": True,
        "max_deviation_pct": 0.5,
        "block_on_unverified": False,
    },
    "circuit_breaker": {
        "enabled": True,
        "soft_threshold": 3,
        "soft_pause_hours": 4,
        "hard_threshold": 5,
        "hard_pause_hours": 24,
    },
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
) -> int | None:
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
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json=payload, timeout=8,
        )
        if r.status_code == 200:
            return r.json().get("result", {}).get("message_id")
        logging.error(f"❌ TG API 回應碼 {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logging.error(f"❌ TG 發送失敗：{e}")
    return None

def _order_keyboard(order_id: str) -> dict:
    return {
        "inline_keyboard": [[{
            "text": f"🔍 查詢訂單 {order_id[-8:]}",
            "callback_data": f"order{order_id}",
        }]]
    }

# ═════════════════════════════════════════════════════════
# 3. 通知格式
# ═════════════════════════════════════════════════════════
def _fmt_entry(
    coin: str, side: str, order_id: str,
    price: float, entry: float, sl: float,
    tp1: float, tp2: float, tp3: float,
    score: int,
    funding_rate: float | None = None,
) -> str:
    direction = "做多" if side == "LONG" else "做空"
    emoji     = "🟢" if side == "LONG" else "🔴"
    grade     = ("🔥 A+ 極強" if score >= 85 else
                 "⭐ A 強力"  if score >= 70 else "✅ B+ 合格")
    tp1_pct = (tp1 - entry) / entry * 100 * (1 if side == "LONG" else -1)
    tp2_pct = (tp2 - entry) / entry * 100 * (1 if side == "LONG" else -1)
    tp3_pct = (tp3 - entry) / entry * 100 * (1 if side == "LONG" else -1)
    sl_pct  = (sl  - entry) / entry * 100
    funding_line = ""
    if funding_rate is not None:
        funding_line = f"💰 資金費率：`{funding_rate * 100:+.4f}%`\n"
    return (
        f"{emoji} {coin} 進場提醒 {grade}\n"
        f"━━━━━━━━━━━━━━\n"
        f"🆔 訂單編號：`{order_id}`\n"
        f"⏰ 時間：{tw_ts()}\n"
        f"方向：{direction}\n"
        f"進場價：`{entry:.4f}`\n"
        f"當前價：`{price:.4f}`\n"
        f"評分：{score} 分\n"
        f"{funding_line}\n"
        f"🎯 止盈目標（建議分批出場）：\n"
        f" TP1 `{tp1:.4f}` ({tp1_pct:+.2f}%)  → 建議出 *30%*\n"
        f" TP2 `{tp2:.4f}` ({tp2_pct:+.2f}%)  → 建議出 *30%*\n"
        f" TP3 `{tp3:.4f}` ({tp3_pct:+.2f}%)  → 建議出 *40%*\n"
        f"\n"
        f"🛑 止損：`{sl:.4f}` ({sl_pct:+.2f}%)\n"
        f"\n"
        f"💰 *建議倉位（固定風險法）：*\n"
        f" 保守 1%：總資金的 `{min(abs(1/sl_pct)*100,500):.0f}%` 倉位\n"
        f" 標準 1.5%：總資金的 `{min(abs(1.5/sl_pct)*100,500):.0f}%` 倉位\n"
        f"\n"
        f"💡 到達 TP1 自動保本，到達 TP2 自動鎖利至 TP1"
    )

def _fmt_tp(
    coin: str, side: str, order_id: str,
    tp_level: str, price: float, pnl_pct: float, r_mult: float,
    wick_triggered: bool = False,
) -> str:
    direction = "做多" if side == "LONG" else "做空"
    advice = (
        "建議出場 30%，剩餘繼續持有 ⏳" if tp_level == "TP1" else
        "建議再出場 30%，剩 40% 衝 TP3 🚀" if tp_level == "TP2" else
        "建議全部出場，完美收割 🏆"
    )
    wick_note = "\n🪡 插針觸發（K 線插針觸及目標價）" if wick_triggered else ""
    return (
        f"🎯 {coin} {tp_level} 達標！\n"
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
    coin: str, side: str, order_id: str,
    price: float, pnl_pct: float,
    mode: str = "LOSS", r_value: float = -1.0,
    wick_triggered: bool = False,
) -> str:
    direction = "做多" if side == "LONG" else "做空"
    if mode == "BE":
        label   = "🔒 保本出場"
        r_tag   = "`0.0R`"
        advice  = "✨ TP1 已達成，止損上移至進場價\n本筆無損出場，資金完整保留\n💡 等待下一個高勝率訊號 💪"
    elif mode == "LOCK":
        label   = "🔐 鎖利出場"
        r_tag   = f"`+{r_value:.1f}R`"
        advice  = "🎉 TP2 已達成，止損上移至 TP1\n趨勢回頭時鎖住 TP1 的獲利優雅退場\n💡 風控完美執行，繼續保持 ✨"
    else:
        label   = "❌ 止損離場"
        r_tag   = "`-1.0R`"
        advice  = "💡 遵守風控，勿加碼攤平。下一筆訊號會更好 🚀"
    wick_note = "\n🪡 插針觸發（K 線插針觸及平倉價）" if wick_triggered else ""
    return (
        f"{label} {coin}\n"
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
    coin      = sig["instId"].split("-")[0]
    side      = sig["side"]
    direction = "做多" if side == "LONG" else "做空"
    entry     = sig["entry"]
    pnl = (
        (current_price - entry) / entry * 100 if side == "LONG"
        else (entry - current_price) / entry * 100
    )
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"
    if sig.get("hit_tp3"):     progress = "🏆 TP3 ✅"
    elif sig.get("hit_tp2"):   progress = "🥇✅ → 🥈✅ → ⏳ TP3"
    elif sig.get("hit_tp1"):   progress = "🥇✅ → ⏳ TP2"
    else:                      progress = "⏳ 等待 TP1"
    return (
        f"📊 {coin} 持倉更新\n"
        f"━━━━━━━━━━━━━━\n"
        f"🆔 訂單編號：`{sig.get('order_id', 'N/A')}`\n"
        f"⏰ 時間：{tw_ts()}\n"
        f"方向：{direction}\n"
        f"當前：`{current_price:.4f}` {pnl_emoji}{pnl:+.2f}%\n"
        f"進場：`{entry:.4f}`\n"
        f"\n"
        f"🎯 止盈進度：{progress}\n"
        f" TP1 `{sig['tp1']:.4f}`{'✅' if sig.get('hit_tp1') else ''}\n"
        f" TP2 `{sig['tp2']:.4f}`{'✅' if sig.get('hit_tp2') else ''}\n"
        f" TP3 `{sig['tp3']:.4f}`{'✅' if sig.get('hit_tp3') else ''}\n"
        f"\n"
        f"🛑 止損：`{sig['sl']:.4f}`"
    )


# ═════════════════════════════════════════════════════════
# 4. 數據抓取
# ═════════════════════════════════════════════════════════
def fetch_price(instId: str) -> float:
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

def fetch_candles(instId: str, tf: str = "15m", limit: int = 300) -> list | None:
    """已收線 K 線，由舊到新，預設抓 300 根（v15 增量）"""
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}",
            timeout=8,
        ).json()
        if res.get("code") != "0":
            return None
        data = res.get("data", [])
        if len(data) < 30:
            return None
        confirmed = [r for r in data if r[8] == "1"][::-1]
        return [
            {"ts": r[0], "o": float(r[1]), "h": float(r[2]),
             "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
            for r in confirmed
        ]
    except Exception as e:
        logging.warning(f"⚠️ 取得 {instId} K 線失敗：{e}")
        return None

_candle_full_cache: dict = {}

def fetch_candles_full(instId: str, tf: str = "15m", limit: int = 100) -> list:
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
            {"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]),
             "l": float(r[3]), "c": float(r[4]), "v": float(r[5]),
             "confirmed": r[8] == "1"}
            for r in data
        ]
        candles.sort(key=lambda x: x["ts"])
        _candle_full_cache[instId] = (candles, now)
        return candles
    except Exception as e:
        logging.warning(f"⚠️ 取得 {instId} 完整 K 線失敗：{e}")
        return _candle_full_cache.get(instId, ([], 0))[0]

def fetch_funding_rate(instId: str) -> float | None:
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
# 4.5 TradingView 第二價格來源
# ═════════════════════════════════════════════════════════
_tv_cache: dict = {}

def fetch_price_tv(instId: str) -> float | None:
    now = time.time()
    if instId in _tv_cache:
        price, t = _tv_cache[instId]
        if now - t < 10:
            return price
    try:
        from tradingview_ta import TA_Handler, Interval  # type: ignore
    except ImportError:
        logging.warning("⚠️ 未安裝 tradingview_ta，跳過 TV 驗證")
        return None
    try:
        symbol = instId.replace("-USDT-SWAP", "USDT.P").replace("-", "")
        handler = TA_Handler(
            symbol=symbol, exchange="OKX",
            screener="crypto", interval=Interval.INTERVAL_1_MINUTE, timeout=8,
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
    instId: str, okx_price: float,
    max_dev_pct: float = 0.5, block_on_unverified: bool = False,
) -> tuple[bool, float | None, float]:
    tv_price = fetch_price_tv(instId)
    if tv_price is None:
        return (not block_on_unverified, None, 0.0)
    diff_pct = abs(okx_price - tv_price) / okx_price * 100
    if diff_pct > max_dev_pct:
        logging.warning(
            f"🚨 {instId} 偏離：OKX={okx_price:.4f} TV={tv_price:.4f} diff={diff_pct:.3f}%"
        )
        return (False, tv_price, diff_pct)
    return (True, tv_price, diff_pct)


# ═════════════════════════════════════════════════════════
# 5. 精準技術指標（v15 全面升級為 Wilder 正統公式）
# ═════════════════════════════════════════════════════════

def calc_atr(df: list, period: int = 14) -> float:
    """ATR — Wilder EMA 平滑法（與 TradingView 一致）
    初始值 = 前 period 根 TR 的簡單平均；
    之後每根：ATR = (ATR_prev * (period-1) + TR) / period
    """
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
    # Wilder 初始化
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr if atr > 0 else 0.001


def calc_rsi(df: list, period: int = 14) -> float:
    """RSI — Wilder EMA 平滑法（修正舊版簡單平均誤差）
    初始 avg_gain/avg_loss = 前 period 根的簡單平均；
    之後：avg = (avg_prev * (period-1) + current) / period
    """
    if len(df) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(df)):
        ch = df[i]["c"] - df[i - 1]["c"]
        gains.append(ch if ch > 0 else 0.0)
        losses.append(-ch if ch < 0 else 0.0)
    if len(gains) < period:
        return 50.0
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))


def calc_adx(df: list, period: int = 14) -> float:
    """ADX — 完整 Wilder DI 平滑法（三路 EMA：+DM/-DM/TR）
    1. 計算每根的原始 +DM、-DM、TR
    2. Wilder 平滑三路
    3. +DI = 100 * smoothed_+DM / smoothed_TR
    4. DX  = 100 * |+DI - -DI| / (+DI + -DI)
    5. ADX = Wilder 平滑 DX
    """
    if len(df) < period * 2 + 2:
        return 0.0
    pdms, mdms, trs = [], [], []
    for i in range(1, len(df)):
        up = df[i]["h"] - df[i - 1]["h"]
        dn = df[i - 1]["l"] - df[i]["l"]
        pdms.append(up if (up > dn and up > 0) else 0.0)
        mdms.append(dn if (dn > up and dn > 0) else 0.0)
        trs.append(max(
            df[i]["h"] - df[i]["l"],
            abs(df[i]["h"] - df[i - 1]["c"]),
            abs(df[i]["l"] - df[i - 1]["c"]),
        ))
    if len(trs) < period:
        return 0.0
    # Wilder 初始化
    s_pdm = sum(pdms[:period])
    s_mdm = sum(mdms[:period])
    s_tr  = sum(trs[:period])
    dxs   = []
    for i in range(period, len(trs)):
        s_pdm = s_pdm - s_pdm / period + pdms[i]
        s_mdm = s_mdm - s_mdm / period + mdms[i]
        s_tr  = s_tr  - s_tr  / period + trs[i]
        if s_tr == 0:
            continue
        pdi = 100 * s_pdm / s_tr
        mdi = 100 * s_mdm / s_tr
        denom = pdi + mdi
        if denom == 0:
            continue
        dxs.append(100 * abs(pdi - mdi) / denom)
    if len(dxs) < period:
        return 0.0
    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period
    return round(adx, 2)


def calc_supertrend(df: list, period: int = 10, mult: float = 3.0) -> int:
    """Supertrend：1=多頭 / -1=空頭 / 0=震盪"""
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


def calc_ema(df: list, period: int) -> list:
    """計算全序列 EMA，回傳與 df 等長的列表（前 period-1 根為 None）"""
    closes = [r["c"] for r in df]
    result: list = [None] * len(closes)
    if len(closes) < period:
        return result
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    result[period - 1] = ema
    for i in range(period, len(closes)):
        ema = closes[i] * k + ema * (1 - k)
        result[i] = ema
    return result


def calc_ema_last(df: list, period: int) -> float | None:
    """只取最後一根的 EMA 值"""
    series = calc_ema(df, period)
    vals = [v for v in series if v is not None]
    return vals[-1] if vals else None


# ═════════════════════════════════════════════════════════
# 5.5 v15 新增指標：Pivot S/R、Fibonacci、OBV、VWAP、BB
# ═════════════════════════════════════════════════════════

def calc_pivot_sr(df: list) -> dict:
    """Classic Pivot Points（以最後完整交易段計算）
    使用最近 period 根 K 線的最高/最低/收盤作為前日 HLC
    回傳 dict: pp, r1, r2, r3, s1, s2, s3
    """
    if len(df) < 20:
        return {}
    seg  = df[-20:]
    high = max(r["h"] for r in seg)
    low  = min(r["l"] for r in seg)
    close = df[-1]["c"]
    pp = (high + low + close) / 3
    r1 = 2 * pp - low
    s1 = 2 * pp - high
    r2 = pp + (high - low)
    s2 = pp - (high - low)
    r3 = high + 2 * (pp - low)
    s3 = low  - 2 * (high - pp)
    return {"pp": pp, "r1": r1, "r2": r2, "r3": r3, "s1": s1, "s2": s2, "s3": s3}


def calc_fibonacci_sr(df: list, lookback: int = 100) -> dict:
    """自動 Fibonacci 回調水位
    在 lookback 根 K 線內找最明顯的 swing high / swing low，
    然後計算六條回調線：0/23.6/38.2/50/61.8/78.6/100
    多頭：從 swing low 往上量；空頭：從 swing high 往下量
    """
    seg = df[-lookback:] if len(df) >= lookback else df
    swing_high = max(r["h"] for r in seg)
    swing_low  = min(r["l"] for r in seg)
    diff = swing_high - swing_low
    if diff == 0:
        return {}
    levels = {}
    for ratio, label in [
        (0.0, "f0"), (0.236, "f236"), (0.382, "f382"),
        (0.500, "f500"), (0.618, "f618"), (0.786, "f786"), (1.0, "f100"),
    ]:
        levels[label] = round(swing_high - diff * ratio, 6)
    levels["swing_high"] = swing_high
    levels["swing_low"]  = swing_low
    return levels


def nearest_sr_levels(price: float, pivot: dict, fib: dict, n: int = 3) -> dict:
    """把 Pivot 與 Fibonacci 所有水位合併，找最近的支撐與阻力各 n 個"""
    all_levels = []
    for v in pivot.values():
        if isinstance(v, float):
            all_levels.append(v)
    for k, v in fib.items():
        if k not in ("swing_high", "swing_low") and isinstance(v, float):
            all_levels.append(v)
    all_levels = sorted(set(round(v, 6) for v in all_levels))
    supports  = [v for v in all_levels if v < price * 0.9998]
    resists   = [v for v in all_levels if v > price * 1.0002]
    return {
        "nearest_sup": supports[-n:]  if supports else [],
        "nearest_res": resists[:n]    if resists  else [],
    }


def calc_obv(df: list) -> float:
    """OBV（On-Balance Volume）— 回傳趨勢方向 +1/0/-1
    最後 5 根 OBV 斜率：上升=多頭量能，下降=空頭量能
    """
    if len(df) < 10:
        return 0.0
    obv = 0.0
    obvs = []
    for i in range(1, len(df)):
        if df[i]["c"] > df[i - 1]["c"]:
            obv += df[i]["v"]
        elif df[i]["c"] < df[i - 1]["c"]:
            obv -= df[i]["v"]
        obvs.append(obv)
    if len(obvs) < 5:
        return 0.0
    slope = obvs[-1] - obvs[-5]
    if slope > 0:
        return 1.0
    if slope < 0:
        return -1.0
    return 0.0


def calc_vwap(df: list) -> float:
    """VWAP（成交量加權均價）— 使用全部傳入 K 線"""
    total_vol = sum(r["v"] for r in df)
    if total_vol == 0:
        return df[-1]["c"] if df else 0.0
    tp_vol = sum(((r["h"] + r["l"] + r["c"]) / 3) * r["v"] for r in df)
    return tp_vol / total_vol


def calc_bollinger(df: list, period: int = 20, std_mult: float = 2.0) -> dict:
    """Bollinger Bands + Squeeze 偵測
    Squeeze = 帶寬 < 過去 125 根帶寬的最低值（KB Squeeze 簡化版）
    回傳 dict: mid, upper, lower, bandwidth, squeeze(bool), pct_b
    """
    if len(df) < period:
        return {}
    closes = [r["c"] for r in df]
    mid    = sum(closes[-period:]) / period
    var    = sum((c - mid) ** 2 for c in closes[-period:]) / period
    std    = var ** 0.5
    upper  = mid + std_mult * std
    lower  = mid - std_mult * std
    bw     = (upper - lower) / mid if mid else 0
    # 歷史最窄帶寬（用來判斷 squeeze）
    hist_bws = []
    for i in range(period, min(len(df), period + 125)):
        seg = closes[-(period + i):(-i) if i else None]
        if len(seg) < period:
            break
        m = sum(seg[-period:]) / period
        v = sum((c - m) ** 2 for c in seg[-period:]) / period
        s = v ** 0.5
        if m:
            hist_bws.append((m + std_mult * s - (m - std_mult * s)) / m)
    squeeze = bool(hist_bws and bw <= min(hist_bws))
    cur = closes[-1]
    pct_b = (cur - lower) / (upper - lower) if (upper - lower) else 0.5
    return {
        "mid": mid, "upper": upper, "lower": lower,
        "bandwidth": round(bw, 5),
        "squeeze": squeeze,
        "pct_b": round(pct_b, 3),
    }


def detect_rsi_divergence(df: list, side: str, rsi_period: int = 14) -> dict:
    """RSI 背離偵測（Regular + Hidden）
    方法：在最近 50 根中找連續兩個局部低點（多頭）或高點（空頭）
          比較 Price 與 RSI 各自的方向是否相反

    回傳 dict:
        regular  (bool) — 正規背離：趨勢反轉訊號
        hidden   (bool) — 隱藏背離：趨勢延續訊號
        desc     (str)  — 文字說明
    """
    if len(df) < rsi_period + 20:
        return {"regular": False, "hidden": False, "desc": ""}

    # 計算全段 RSI 序列（只取有值部分）
    closes = [r["c"] for r in df]
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(ch if ch > 0 else 0.0)
        losses.append(-ch if ch < 0 else 0.0)
    if len(gains) < rsi_period:
        return {"regular": False, "hidden": False, "desc": ""}
    avg_g = sum(gains[:rsi_period]) / rsi_period
    avg_l = sum(losses[:rsi_period]) / rsi_period
    rsi_series = []
    for i in range(rsi_period, len(gains)):
        avg_g = (avg_g * (rsi_period - 1) + gains[i]) / rsi_period
        avg_l = (avg_l * (rsi_period - 1) + losses[i]) / rsi_period
        rs = avg_g / avg_l if avg_l else 100
        rsi_series.append(100 - 100 / (1 + rs))

    if len(rsi_series) < 10:
        return {"regular": False, "hidden": False, "desc": ""}

    lookback = min(50, len(rsi_series) - 1)
    rsi_seg  = rsi_series[-lookback:]
    price_seg = [r["c"] for r in df[-lookback:]]

    def find_pivots_low(series, w=3):
        pivots = []
        for i in range(w, len(series) - w):
            if all(series[i] <= series[i - j] for j in range(1, w + 1)) and \
               all(series[i] <= series[i + j] for j in range(1, w + 1)):
                pivots.append((i, series[i]))
        return pivots

    def find_pivots_high(series, w=3):
        pivots = []
        for i in range(w, len(series) - w):
            if all(series[i] >= series[i - j] for j in range(1, w + 1)) and \
               all(series[i] >= series[i + j] for j in range(1, w + 1)):
                pivots.append((i, series[i]))
        return pivots

    regular = False
    hidden  = False
    desc    = ""

    if side == "LONG":
        # 多頭：找價格低點
        price_lows = find_pivots_low(price_seg)
        rsi_lows   = find_pivots_low(rsi_seg)
        if len(price_lows) >= 2 and len(rsi_lows) >= 2:
            p1, p2 = price_lows[-2], price_lows[-1]
            r1_idx = min(rsi_lows, key=lambda x: abs(x[0] - p1[0]))
            r2_idx = min(rsi_lows, key=lambda x: abs(x[0] - p2[0]))
            price_down = p2[1] < p1[1]   # 價格更低低點
            rsi_up     = r2_idx[1] > r1_idx[1]  # RSI 更高低點
            rsi_down   = r2_idx[1] < r1_idx[1]  # RSI 更低低點
            price_up   = p2[1] > p1[1]   # 價格更高低點
            if price_down and rsi_up:
                regular = True
                desc = "📈 正規多頭背離（價格新低但 RSI 不新低）→ 底部反轉"
            elif price_up and rsi_down:
                hidden = True
                desc = "🔒 隱藏多頭背離（RSI 新低但價格未創新低）→ 趨勢延續"
    else:
        # 空頭：找價格高點
        price_highs = find_pivots_high(price_seg)
        rsi_highs   = find_pivots_high(rsi_seg)
        if len(price_highs) >= 2 and len(rsi_highs) >= 2:
            p1, p2 = price_highs[-2], price_highs[-1]
            r1_idx = min(rsi_highs, key=lambda x: abs(x[0] - p1[0]))
            r2_idx = min(rsi_highs, key=lambda x: abs(x[0] - p2[0]))
            price_up  = p2[1] > p1[1]
            rsi_down  = r2_idx[1] < r1_idx[1]
            price_down = p2[1] < p1[1]
            rsi_up     = r2_idx[1] > r1_idx[1]
            if price_up and rsi_down:
                regular = True
                desc = "📉 正規空頭背離（價格新高但 RSI 不新高）→ 頂部反轉"
            elif price_down and rsi_up:
                hidden = True
                desc = "🔒 隱藏空頭背離（RSI 新高但價格未創新高）→ 趨勢延續"

    return {"regular": regular, "hidden": hidden, "desc": desc}


# ═════════════════════════════════════════════════════════
# 6. SMC / ICT / SNR / PA / 流動性 / 動能（保留 v14）
# ═════════════════════════════════════════════════════════

def find_order_block(df: list, side: str, lookback: int = 30) -> dict | None:
    n = len(df)
    if n < lookback + 5:
        return None
    start = max(0, n - lookback)
    if side == "LONG":
        for i in range(n - 4, start, -1):
            if df[i]["c"] < df[i]["o"]:
                for j in range(i + 1, min(i + 4, n)):
                    if df[j]["c"] > df[i]["h"]:
                        return {"low": df[i]["l"], "high": df[i]["h"]}
    else:
        for i in range(n - 4, start, -1):
            if df[i]["c"] > df[i]["o"]:
                for j in range(i + 1, min(i + 4, n)):
                    if df[j]["c"] < df[i]["l"]:
                        return {"low": df[i]["l"], "high": df[i]["h"]}
    return None


def find_fvg(df: list, side: str, lookback: int = 30) -> dict | None:
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
    """v15：優先用 Fibonacci 水位；fallback 用極值"""
    fib = calc_fibonacci_sr(df, lookback)
    if fib and "swing_low" in fib and "swing_high" in fib:
        return fib["swing_low"], fib["swing_high"]
    seg = df[-lookback:] if len(df) >= lookback else df
    return min(r["l"] for r in seg), max(r["h"] for r in seg)


def detect_price_action(df: list, side: str) -> bool:
    if len(df) < 2:
        return False
    last, prev = df[-1], df[-2]
    body  = abs(last["c"] - last["o"])
    upper = last["h"] - max(last["c"], last["o"])
    lower = min(last["c"], last["o"]) - last["l"]
    if body > 0:
        if side == "LONG"  and lower > body * 2 and lower > upper: return True
        if side == "SHORT" and upper > body * 2 and upper > lower: return True
    if side == "LONG":
        if (prev["c"] < prev["o"] and last["c"] > last["o"] and
                last["c"] > prev["o"] and last["o"] < prev["c"]):
            return True
    else:
        if (prev["c"] > prev["o"] and last["c"] < last["o"] and
                last["c"] < prev["o"] and last["o"] > prev["c"]):
            return True
    return False


def detect_liquidity_sweep(df: list, side: str, lookback: int = 20) -> bool:
    if len(df) < lookback + 1:
        return False
    seg  = df[-(lookback + 1): -1]
    last = df[-1]
    prev_low  = min(r["l"] for r in seg)
    prev_high = max(r["h"] for r in seg)
    mid = (prev_low + prev_high) / 2
    if side == "LONG":
        return last["l"] < prev_low  and last["c"] > mid
    return last["h"] > prev_high and last["c"] < mid


def calc_momentum_ratio(df: list, side: str, n: int = 5) -> bool:
    seg  = df[-n:]
    bull = sum(1 for r in seg if r["c"] > r["o"])
    ratio = bull / max(1, len(seg))
    return ratio >= 0.6 if side == "LONG" else ratio <= 0.4

# ═════════════════════════════════════════════════════════
# 6.5 v14 市場狀態 / MTF（保留）
# ═════════════════════════════════════════════════════════

def detect_market_regime(df: list) -> dict:
    adx     = calc_adx(df)
    atr     = calc_atr(df)
    price   = df[-1]["c"] if df else 1
    atr_pct = atr / price * 100 if price else 0
    if adx > 25:   regime = "trend"
    elif adx < 18: regime = "range"
    else:          regime = "transitional"
    return {
        "regime": regime,
        "adx": round(adx, 1),
        "atr_pct": round(atr_pct, 3),
        "volatile": atr_pct > 2.5,
    }

_mtf_cache: dict = {}

def fetch_mtf_trend(instId: str) -> dict:
    now = time.time()
    if instId in _mtf_cache:
        data, t = _mtf_cache[instId]
        if now - t < 30:
            return data
    out = {}
    for tf in ("1H", "4H", "1W"):
        limit = 50 if tf != "1W" else 30
        df = fetch_candles(instId, tf=tf, limit=limit)
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
    expect = 1 if side == "LONG" else -1
    h1 = mtf.get("1H", {}).get("supertrend", 0)
    h4 = mtf.get("4H", {}).get("supertrend", 0)
    score = 0
    if h1 == expect:   score += 8
    elif h1 == -expect: score -= 5
    if h4 == expect:   score += 7
    elif h4 == -expect: score -= 5
    score = max(-15, min(15, score))
    align_desc = [
        f"1H={'順' if h1 == expect else '反' if h1 == -expect else '中'}",
        f"4H={'順' if h4 == expect else '反' if h4 == -expect else '中'}",
    ]
    return score, " / ".join(align_desc)


def calc_volume_quality(df: list, lookback: int = 20) -> tuple[float, int]:
    if len(df) < lookback + 1:
        return 1.0, 0
    seg = df[-(lookback + 1):-1]
    avg = sum(c["v"] for c in seg) / lookback
    if avg <= 0:
        return 1.0, 0
    ratio = df[-1]["v"] / avg
    if ratio >= 2.0:   s = 8
    elif ratio >= 1.5: s = 5
    elif ratio >= 1.0: s = 2
    elif ratio >= 0.5: s = 0
    else:              s = -10
    return round(ratio, 2), s


def adjust_tp_by_sr(entry: float, side: str, tp_levels: list, df: list) -> tuple[list, list]:
    """v15：使用精準 Pivot + Fib S/R 校正 TP"""
    pivot = calc_pivot_sr(df)
    fib   = calc_fibonacci_sr(df)
    sr    = nearest_sr_levels(entry, pivot, fib)
    sup   = sr["nearest_sup"]
    res   = sr["nearest_res"]
    out   = list(tp_levels)
    notes = []
    if side == "LONG" and res:
        nearest_r = res[0]
        for i, tp in enumerate(out):
            if tp > nearest_r * 1.001:
                new_tp = nearest_r * 0.998
                if new_tp > entry:
                    notes.append(f"TP{i+1} {tp:.4f}→{new_tp:.4f}（阻力 {nearest_r:.4f}）")
                    out[i] = new_tp
    elif side == "SHORT" and sup:
        nearest_s = sup[-1]
        for i, tp in enumerate(out):
            if tp < nearest_s * 0.999:
                new_tp = nearest_s * 1.002
                if new_tp < entry:
                    notes.append(f"TP{i+1} {tp:.4f}→{new_tp:.4f}（支撐 {nearest_s:.4f}）")
                    out[i] = new_tp
    return out, notes


def detect_pullback(df: list, side: str) -> bool:
    if len(df) < 3:
        return False
    last = df[-1]
    body  = abs(last["c"] - last["o"])
    if body == 0:
        return False
    upper = last["h"] - max(last["c"], last["o"])
    lower = min(last["c"], last["o"]) - last["l"]
    if side == "LONG":
        return lower > body * 1.2 and last["c"] > last["o"]
    return upper > body * 1.2 and last["c"] < last["o"]


# ═════════════════════════════════════════════════════════
# 7. 評分系統（v15 升級：OBV + VWAP + BB Squeeze + RSI 背離）
# ═════════════════════════════════════════════════════════

def calc_score(
    df: list, side: str, current_price: float,
    mtf: dict | None = None, instId: str | None = None,
) -> tuple[int, str, dict]:
    """
    評分組成（最高基礎分約 155）：
      趨勢30 + RSI25 + OB20 + FVG15 + SNR5 + PA5 + 流動性5 + 動能5
      + MTF15 + Volume8
      + OBV5 + VWAP5 + BB_Squeeze8 + RSI_Regular_Div12 + RSI_Hidden_Div6
      = 基礎 ~169（門檻仍為 68，高分更稀有）
    """
    detail = {}
    score  = 0

    # ── 趨勢 (30) ──
    st = calc_supertrend(df)
    if (side == "LONG" and st == 1) or (side == "SHORT" and st == -1):
        score += 30; detail["trend"] = 30
    elif st == 0:
        score += 15; detail["trend"] = 15
    else:
        detail["trend"] = 0

    # ── RSI (25) ──
    rsi = calc_rsi(df)
    detail["rsi_value"] = round(rsi, 1)
    if side == "LONG":
        if   30 <= rsi <= 50: score += 25; detail["rsi"] = 25
        elif 50 < rsi  < 70: score += 15; detail["rsi"] = 15
        else:                              detail["rsi"] = 0
    else:
        if   50 <= rsi <= 70: score += 25; detail["rsi"] = 25
        elif 30 < rsi  < 50: score += 15; detail["rsi"] = 15
        else:                              detail["rsi"] = 0

    # ── OB (20) ──
    ob = find_order_block(df, side)
    if ob and ob["low"] * 0.995 <= current_price <= ob["high"] * 1.005:
        score += 20; detail["ob"] = 20
    else:
        detail["ob"] = 0

    # ── FVG (15) ──
    fvg = find_fvg(df, side)
    if fvg and fvg["low"] * 0.997 <= current_price <= fvg["high"] * 1.003:
        score += 15; detail["fvg"] = 15
    else:
        detail["fvg"] = 0

    # ── SNR / Fibonacci (5) ── v15：用 Fib 水位判斷
    fib = calc_fibonacci_sr(df)
    pivot = calc_pivot_sr(df)
    sr_info = nearest_sr_levels(current_price, pivot, fib)
    fib_bonus = 0
    if side == "LONG" and sr_info["nearest_sup"]:
        near_sup = sr_info["nearest_sup"][-1]
        if abs(current_price - near_sup) / current_price < 0.005:
            fib_bonus = 5
    elif side == "SHORT" and sr_info["nearest_res"]:
        near_res = sr_info["nearest_res"][0]
        if abs(current_price - near_res) / current_price < 0.005:
            fib_bonus = 5
    # fallback 舊極值 SNR
    if fib_bonus == 0:
        sup, res = calc_snr(df)
        if side == "LONG" and current_price <= sup * 1.01:   fib_bonus = 5
        elif side == "SHORT" and current_price >= res * 0.99: fib_bonus = 5
    detail["snr"] = fib_bonus
    score += fib_bonus

    # ── PA (5) ──
    detail["pa"] = 5 if detect_price_action(df, side) else 0
    score += detail["pa"]

    # ── 流動性掃蕩 (5) ──
    detail["liq"] = 5 if detect_liquidity_sweep(df, side) else 0
    score += detail["liq"]

    # ── 動能 (5) ──
    detail["mom"] = 5 if calc_momentum_ratio(df, side) else 0
    score += detail["mom"]

    # ── MTF (-15~+15) ──
    if mtf is None and instId:
        mtf = fetch_mtf_trend(instId)
    if mtf:
        mtf_score, mtf_desc = calc_mtf_alignment(mtf, side)
        score += mtf_score
        detail["mtf"] = mtf_score
        detail["mtf_desc"] = mtf_desc
        # ── 週線 Supertrend 逆勢懲罰（-12 分）──
        expect_w = 1 if side == "LONG" else -1
        w1_st = mtf.get("1W", {}).get("supertrend", 0)
        if w1_st == -expect_w:
            score -= 12
            detail["weekly_penalty"] = -12
            detail["weekly_trend"] = "逆週線"
        elif w1_st == expect_w:
            detail["weekly_penalty"] = 0
            detail["weekly_trend"] = "順週線 ✅"
        else:
            detail["weekly_penalty"] = 0
            detail["weekly_trend"] = "週線中性"

    # ── Volume (-10~+8) ──
    vol_ratio, vol_score = calc_volume_quality(df)
    score += vol_score
    detail["volume"] = vol_score
    detail["volume_ratio"] = vol_ratio

    # ══ v15 新增指標 ══

    # ── OBV (+5) ──
    obv_dir = calc_obv(df)
    expect  = 1.0 if side == "LONG" else -1.0
    obv_score = 5 if obv_dir == expect else (-3 if obv_dir == -expect else 0)
    score += obv_score
    detail["obv"] = obv_score

    # ── VWAP (+5) ── 價格在 VWAP 正確方向
    vwap = calc_vwap(df)
    detail["vwap"] = round(vwap, 4)
    vwap_score = 0
    if side == "LONG"  and current_price > vwap: vwap_score = 5
    elif side == "SHORT" and current_price < vwap: vwap_score = 5
    elif side == "LONG"  and current_price < vwap * 0.995: vwap_score = -3
    elif side == "SHORT" and current_price > vwap * 1.005: vwap_score = -3
    score += vwap_score
    detail["vwap_score"] = vwap_score

    # ── BB Squeeze (+8) ── 帶寬收窄後即將爆發
    bb = calc_bollinger(df)
    bb_squeeze_score = 0
    if bb:
        detail["bb_bandwidth"] = bb.get("bandwidth", 0)
        detail["bb_pct_b"]     = bb.get("pct_b", 0.5)
        if bb.get("squeeze"):
            bb_squeeze_score = 8
            detail["bb_squeeze"] = True
        else:
            detail["bb_squeeze"] = False
        # 價格貼近 BB 邊緣確認方向
        pct_b = bb.get("pct_b", 0.5)
        if side == "LONG"  and pct_b < 0.2:  bb_squeeze_score += 2
        elif side == "SHORT" and pct_b > 0.8: bb_squeeze_score += 2
    score += bb_squeeze_score
    detail["bb_score"] = bb_squeeze_score

    # ── RSI 背離 (+12 正規 / +6 隱藏) ──
    div = detect_rsi_divergence(df, side)
    div_score = 0
    if div.get("regular"):
        div_score = 12
        detail["rsi_div"] = "regular"
        detail["rsi_div_desc"] = div.get("desc", "")
    elif div.get("hidden"):
        div_score = 6
        detail["rsi_div"] = "hidden"
        detail["rsi_div_desc"] = div.get("desc", "")
    else:
        detail["rsi_div"] = "none"
    score += div_score
    detail["rsi_div_score"] = div_score

    grade = (
        "A+ 極強 🔥" if score >= 85 else
        "A 強力 ⭐"  if score >= 70 else
        "B+ 合格 ✅" if score >= 68 else
        "觀望 ⚪"
    )
    return score, grade, detail


# ═════════════════════════════════════════════════════════
# 8. 訊號生成
# ═════════════════════════════════════════════════════════

def generate_signal(
    instId: str, df: list, current_price: float,
    funding_rate: float | None = None,
    score_threshold: int | None = None,
    atr_max_pct: float = 0.04,
    signal_expire_hours: int = SIGNAL_EXPIRE_HOURS,
) -> dict | None:
    if df is None or len(df) < 50:
        return None
    threshold = score_threshold if score_threshold is not None else SCORE_THRESHOLD
    atr = calc_atr(df)
    if atr / current_price > atr_max_pct:
        return None
    funding_penalty_long  = funding_rate and funding_rate >  0.0008
    funding_penalty_short = funding_rate and funding_rate < -0.0008
    coin = instId.split("-")[0]
    regime_info = detect_market_regime(df)
    if regime_info["regime"] == "range":    threshold += 5
    if regime_info["volatile"]:             threshold += 3
    mtf = fetch_mtf_trend(instId)
    candidates = []
    for side in ("LONG", "SHORT"):
        score, grade, detail = calc_score(df, side, current_price, mtf=mtf)
        if side == "LONG"  and funding_penalty_long:  score -= 5
        if side == "SHORT" and funding_penalty_short: score -= 5
        detail["regime"]   = regime_info["regime"]
        detail["adx"]      = regime_info["adx"]
        detail["atr_pct"]  = regime_info["atr_pct"]
        if detect_pullback(df, side):
            score += 3; detail["pullback"] = True
        adj_simple, notes_simple = apply_learning_adjustment(score, side, detail, funding_rate, coin)
        adj_knn,    notes_knn    = apply_knn_learning(score, side, detail, funding_rate, coin, mtf, regime_info)
        adjusted_score = adj_simple + (adj_knn - score)
        learning_notes = notes_simple + notes_knn
        if learning_notes:
            detail["learning_notes"]  = learning_notes
            detail["learning_adjust"] = adjusted_score - score
        score = adjusted_score
        if score < threshold:
            continue
        entry    = current_price
        sl_dist  = atr * 1.5
        sl       = entry - sl_dist if side == "LONG" else entry + sl_dist
        risk     = abs(entry - sl)
        if side == "LONG":
            tp_levels = [entry + risk * 1.5, entry + risk * 3.0, entry + risk * 5.0]
        else:
            tp_levels = [entry - risk * 1.5, entry - risk * 3.0, entry - risk * 5.0]
        tp_levels, tp_notes = adjust_tp_by_sr(entry, side, tp_levels, df)
        if tp_notes:
            detail["tp_adjust_notes"] = tp_notes
        candidates.append({
            "instId":         instId,
            "side":           side,
            "tf":             "15m",
            "entry":          round(entry, 4),
            "sl":             round(sl, 4),
            "tp1":            round(tp_levels[0], 4),
            "tp2":            round(tp_levels[1], 4),
            "tp3":            round(tp_levels[2], 4),
            "score":          score,
            "grade":          grade,
            "detail":         detail,
            "funding_rate":   funding_rate,
            "mtf_snapshot":   mtf,
            "regime_snapshot":regime_info,
            "created":        time.time(),
            "expires":        time.time() + signal_expire_hours * 3600,
        })
    return max(candidates, key=lambda x: x["score"]) if candidates else None

# ═════════════════════════════════════════════════════════
# 9. 持久化
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
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out

def _validate_config(cfg: dict) -> list:
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
    user_cfg = _load_json(CONFIG_FILE, {})
    merged   = _deep_merge(DEFAULT_CONFIG, user_cfg) if user_cfg else dict(DEFAULT_CONFIG)
    errs = _validate_config(merged)
    if errs:
        logging.warning("⚠️ 配置驗證失敗，fallback 預設值：" + "; ".join(errs))
        return dict(DEFAULT_CONFIG)
    return merged

def is_cooling(instId: str, cooldown_hours: float = COOLDOWN_HOURS) -> bool:
    cd   = _load_json(COOLDOWN_FILE, {})
    last = cd.get(instId)
    if last is None:
        return False
    return (time.time() - float(last)) < cooldown_hours * 3600

def mark_cooldown(instId: str, cooldown_hours: float = COOLDOWN_HOURS) -> None:
    cd = _load_json(COOLDOWN_FILE, {})
    cd[instId] = time.time()
    cutoff = time.time() - cooldown_hours * 3600 * 3
    cd = {k: v for k, v in cd.items() if float(v) > cutoff}
    _save_json(COOLDOWN_FILE, cd)

def record_trade(
    coin: str, side: str, order_id: str,
    entry: float, close_price: float, close_type: str, score: int,
    sig_snapshot: dict | None = None,
) -> None:
    is_win = close_type in ("TP1", "TP2", "TP3", "LOCK")
    is_be  = close_type == "BE"
    pnl    = (
        (close_price - entry) / entry * 100 if side == "LONG"
        else (entry - close_price) / entry * 100
    )
    # 加權實際 RR（依出場類型 × 建議出場比例）
    _rr_map = {"TP1": (1.5, 0.30), "TP2": (3.0, 0.30), "TP3": (5.0, 0.40),
               "LOCK": (1.5, 1.0), "BE": (0.0, 1.0), "SL": (-1.0, 1.0)}
    exit_rr_val, exit_wt = _rr_map.get(close_type, (-1.0, 1.0))
    snap        = sig_snapshot or {}
    detail      = snap.get("detail", {}) or {}
    funding_rate = snap.get("funding_rate")
    mtf         = snap.get("mtf_snapshot")
    regime      = snap.get("regime_snapshot")
    features    = vectorize_signal(score, side, detail, funding_rate, mtf, regime)
    trade = {
        "time":       tw_now().strftime("%Y-%m-%d %H:%M"),
        "date":       tw_now().strftime("%Y-%m-%d"),
        "order_id":   order_id,
        "coin":       coin,
        "side":       side,
        "entry":      entry,
        "close":      close_price,
        "close_type": close_type,
        "pnl":        round(pnl, 2),
        "exit_rr":    exit_rr_val,
        "exit_weight":exit_wt,
        "is_win":     is_win,
        "is_be":      is_be,
        "score":      score,
        "funding_rate": funding_rate,
        "detail":     detail,
        "features":   features,
        "mtf":        mtf,
        "regime":     regime,
    }
    history = _load_json(TRADE_HISTORY_FILE, [])
    history.append(trade)
    _save_json(TRADE_HISTORY_FILE, history)
    logging.info(f"📝 記錄交易：{coin} {order_id} {close_type}")
    try:
        update_learning(trade, sig_snapshot)
    except Exception as e:
        logging.warning(f"⚠️ 更新學習狀態失敗：{e}")


# ═════════════════════════════════════════════════════════
# 9.6 學習機制
# ═════════════════════════════════════════════════════════

def _bucket_score(score: int) -> str:
    if score >= 90: return "score:90+"
    if score >= 80: return "score:80-89"
    if score >= 70: return "score:70-79"
    return "score:60-69"

def _bucket_rsi(rsi: float, side: str) -> str:
    bucket = int(rsi // 10) * 10
    return f"rsi{side.lower()}:{bucket}-{bucket + 9}"

def _bucket_funding(fr) -> str:
    if fr is None:      return "fund:none"
    if fr >  0.0008:    return "fund:very_pos"
    if fr >  0.0001:    return "fund:pos"
    if fr > -0.0001:    return "fund:neutral"
    if fr > -0.0008:    return "fund:neg"
    return "fund:very_neg"

def _bucket_session_tw() -> str:
    h = tw_now().hour
    if  0 <= h <  6: return "sess:asia_dawn"
    if  6 <= h < 14: return "sess:asia_day"
    if 14 <= h < 21: return "sess:europe"
    return "sess:us"

def _signal_buckets(score: int, side: str, detail: dict, funding_rate, coin: str) -> list:
    rsi = (detail or {}).get("rsi_value", 50)
    return [
        _bucket_score(score),
        _bucket_rsi(rsi, side),
        _bucket_funding(funding_rate),
        _bucket_session_tw(),
        f"coin:{coin}",
        f"coin_side:{coin}{side}",
    ]

def update_learning(trade: dict, sig_snapshot: dict | None = None) -> None:
    state = _load_json(LEARNING_FILE, {})
    state.setdefault("buckets", {})
    state.setdefault("by_coin", {})
    state.setdefault("loss_reasons", [])
    state.setdefault("updated_at", 0)
    score        = trade.get("score", 0)
    coin         = trade.get("coin", "?")
    side         = trade.get("side", "?")
    close_type   = trade.get("close_type", "?")
    funding_rate = trade.get("funding_rate")
    detail       = trade.get("detail") or (sig_snapshot.get("detail") if sig_snapshot else {})
    is_win  = close_type in ("TP1", "TP2", "TP3", "LOCK")
    is_be   = close_type == "BE"
    is_loss = close_type == "SL"
    for b in _signal_buckets(score, side, detail, funding_rate, coin):
        bd = state["buckets"].setdefault(b, {"win": 0, "loss": 0, "be": 0, "total": 0})
        bd["total"] += 1
        if is_win:   bd["win"]  += 1
        elif is_loss: bd["loss"] += 1
        elif is_be:   bd["be"]   += 1
    cd = state["by_coin"].setdefault(coin, {"win": 0, "loss": 0, "be": 0, "total": 0})
    cd["total"] += 1
    if is_win:   cd["win"]  += 1
    elif is_loss: cd["loss"] += 1
    elif is_be:   cd["be"]   += 1
    state["updated_at"] = time.time()
    _save_json(LEARNING_FILE, state)

def apply_learning_adjustment(
    score: int, side: str, detail: dict, funding_rate, coin: str,
) -> tuple[int, list]:
    cfg  = load_config()
    lcfg = cfg.get("learning", {})
    if not lcfg.get("enabled", True):
        return score, []
    state       = _load_json(LEARNING_FILE, {})
    buckets     = state.get("buckets", {})
    min_samples = lcfg.get("min_samples", 5)
    max_adj     = lcfg.get("max_score_adjust", 10)
    notes       = []
    adj_total   = 0
    for b in _signal_buckets(score, side, detail, funding_rate, coin):
        bd = buckets.get(b)
        if not bd or bd.get("total", 0) < min_samples:
            continue
        wr = bd["win"] / bd["total"]
        if   wr < 0.30: d = -3
        elif wr < 0.40: d = -2
        elif wr > 0.70: d = +2
        elif wr > 0.60: d = +1
        else:           continue
        adj_total += d
        notes.append(f"{b} (n={bd['total']}, 勝率 {wr:.0%}) → {d:+d}")
    adj_total = max(-max_adj, min(max_adj, adj_total))
    return score + adj_total, notes

def vectorize_signal(
    score: int, side: str, detail: dict, funding_rate,
    mtf: dict | None = None, regime: dict | None = None,
) -> dict:
    rsi = (detail or {}).get("rsi_value", 50)
    return {
        "score":    float(score),
        "rsi":      float(rsi),
        "atr_pct":  float((detail or {}).get("atr_pct", 1.0)),
        "funding":  float(funding_rate or 0) * 1000,
        "vol_ratio":float((detail or {}).get("volume_ratio", 1.0)),
        "adx":      float((regime or {}).get("adx", 20)),
        "mtf_h1":   1.0 if (mtf or {}).get("1H", {}).get("supertrend") == (1 if side == "LONG" else -1) else 0.0,
        "mtf_h4":   1.0 if (mtf or {}).get("4H", {}).get("supertrend") == (1 if side == "LONG" else -1) else 0.0,
        "side":     1.0 if side == "LONG" else 0.0,
    }

_FEATURE_SCALE = {
    "score": 30, "rsi": 50, "atr_pct": 3, "funding": 2,
    "vol_ratio": 3, "adx": 50, "mtf_h1": 1, "mtf_h4": 1, "side": 1,
}

def find_similar_trades(features: dict, history: list, k: int = 10) -> list:
    candidates = []
    for t in history:
        f = t.get("features")
        if not f:
            continue
        d2 = 0.0
        for key, scale in _FEATURE_SCALE.items():
            diff = (features.get(key, 0) - f.get(key, 0)) / max(scale, 1)
            d2  += diff * diff
        candidates.append((d2, t))
    candidates.sort(key=lambda x: x[0])
    return [t for _, t in candidates[:k]]

def apply_knn_learning(
    score: int, side: str, detail: dict, funding_rate, coin: str,
    mtf: dict | None, regime: dict | None,
) -> tuple[int, list]:
    cfg = load_config()
    if not cfg.get("learning", {}).get("knn_enabled", True):
        return score, []
    history = _load_json(TRADE_HISTORY_FILE, [])
    if len(history) < 10:
        return score, []
    feat    = vectorize_signal(score, side, detail, funding_rate, mtf, regime)
    similar = find_similar_trades(feat, history, k=10)
    if len(similar) < 3:
        return score, []
    wins   = sum(1 for t in similar if t.get("close_type") in ("TP1","TP2","TP3","LOCK"))
    losses = sum(1 for t in similar if t.get("close_type") == "SL")
    n      = len(similar)
    wr     = wins / n
    notes  = [f"🧬 KNN：{n} 筆最相似訊號 → 勝 {wins} / 敗 {losses} (勝率 {wr:.0%})"]
    if   wr < 0.30: return score - 8, notes + ["KNN 低勝率 → -8"]
    elif wr < 0.40: return score - 4, notes + ["KNN 偏低勝率 → -4"]
    elif wr > 0.70: return score + 5, notes + ["KNN 高勝率 → +5"]
    elif wr > 0.60: return score + 3, notes + ["KNN 中高勝率 → +3"]
    return score, notes

def record_loss_reason(coin: str, side: str, reasons: list) -> None:
    state = _load_json(LEARNING_FILE, {})
    state.setdefault("loss_reasons", [])
    for r in reasons[:1]:
        state["loss_reasons"].append({
            "ts": time.time(), "coin": coin, "side": side,
            "code": r.get("code"), "title": r.get("title"),
        })
    state["loss_reasons"] = state["loss_reasons"][-100:]
    _save_json(LEARNING_FILE, state)

def _summarize_trades(trades: list) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0}
    win  = sum(1 for t in trades if t.get("close_type") in ("TP1","TP2","TP3","LOCK"))
    loss = sum(1 for t in trades if t.get("close_type") == "SL")
    be   = sum(1 for t in trades if t.get("close_type") == "BE")
    pnl  = sum(t.get("pnl", 0) for t in trades)
    pnls = [t.get("pnl", 0) for t in trades]
    return {
        "n": n, "win": win, "loss": loss, "be": be,
        "wr": win / n * 100 if n else 0,
        "pnl": pnl, "avg": pnl / n if n else 0,
        "max_win": max(pnls) if pnls else 0,
        "max_loss": min(pnls) if pnls else 0,
    }

def _calc_profit_factor(trades: list) -> float:
    """利潤因子 = 總獲利 / 總虧損（>1.5 為健康）"""
    gross_win  = sum(t.get("pnl",0) for t in trades if t.get("pnl",0) > 0)
    gross_loss = abs(sum(t.get("pnl",0) for t in trades if t.get("pnl",0) < 0))
    return gross_win / gross_loss if gross_loss > 0 else float("inf")


def _calc_max_drawdown(trades: list) -> float:
    """最大回撤（連續虧損累積最大值）"""
    peak = 0.0; dd = 0.0; cum = 0.0
    for t in trades:
        cum += t.get("pnl", 0)
        if cum > peak: peak = cum
        drawdown = peak - cum
        if drawdown > dd: dd = drawdown
    return dd


def _wr_status(wr: float) -> str:
    if wr >= 75: return "🏆 優秀"
    if wr >= 70: return "✅ 達標"
    if wr >= 60: return "⚠️ 略低"
    return "🚨 警示"


def format_daily_report(date: str | None = None) -> str:
    if date is None:
        date = tw_now().strftime("%Y-%m-%d")
    history = _load_json(TRADE_HISTORY_FILE, [])
    today   = [t for t in history if t.get("date") == date]
    s = _summarize_trades(today)
    if s["n"] == 0:
        return f"📭 日報 {date}\n當日尚無交易紀錄"
    pf  = _calc_profit_factor(today)
    mdd = _calc_max_drawdown(today)
    pf_str = f"`{pf:.2f}`" if pf != float("inf") else "`∞`"
    rr_stats = calc_avg_actual_rr(today)
    rr_line = ""
    if rr_stats["avg"] is not None:
        rr_line = (
            f"實際加權RR：`{rr_stats['avg']:+.2f}R`"
            f"（贏 `{rr_stats['win_rr']:+.2f}R` / 輸 `{rr_stats['loss_rr']:+.2f}R`）"
        ) if rr_stats["win_rr"] and rr_stats["loss_rr"] else f"實際RR：`{rr_stats['avg']:+.2f}R`"
    daily_pnl_now = get_daily_pnl(date)
    lines = [
        f"📊 *每日績效報告 {date}*",
        "━━━━━━━━━━━━━━",
        f"交易筆數：{s['n']} / 上限 15",
        f"勝 / 平 / 敗：{s['win']} / {s['be']} / {s['loss']}",
        f"勝率：`{s['wr']:.0f}%` {_wr_status(s['wr'])}",
        f"總 PnL：`{s['pnl']:+.2f}%`（加權：`{daily_pnl_now:+.2f}%`）",
        f"平均：`{s['avg']:+.2f}%/筆`",
        f"最大獲利：`{s['max_win']:+.2f}%`  最大虧損：`{s['max_loss']:+.2f}%`",
        f"利潤因子：{pf_str}（>1.5 健康）",
        f"最大回撤：`{mdd:.2f}%`",
    ] + ([rr_line] if rr_line else []) + [""]
    by_coin: dict = {}
    for t in today:
        c = t.get("coin","?"); by_coin.setdefault(c,[]).append(t)
    if by_coin:
        lines.append("💎 各幣種表現：")
        for c, ts in sorted(by_coin.items(), key=lambda x: -sum(t.get("pnl",0) for t in x[1])):
            sub = _summarize_trades(ts)
            lines.append(f"  {c}: {sub['n']} 筆 (勝 {sub['win']}/敗 {sub['loss']}) PnL `{sub['pnl']:+.2f}%`")
    lines.append("")
    lines.append(f"⏰ 報告生成：{tw_ts()}")
    return "\n".join(lines)

def format_monthly_report(year_month: str | None = None) -> str:
    if year_month is None:
        year_month = tw_now().strftime("%Y-%m")
    history = _load_json(TRADE_HISTORY_FILE, [])
    month   = [t for t in history if t.get("date","").startswith(year_month)]
    s = _summarize_trades(month)
    if s["n"] == 0:
        return f"📭 月報 {year_month}\n本月尚無交易紀錄"
    lines = [
        f"📈 月報 {year_month}", "━━━━━━━━━━━━━━",
        f"總交易：{s['n']} 筆",
        f"勝 / 平 / 敗：{s['win']} / {s['be']} / {s['loss']}",
        f"勝率：`{s['wr']:.0f}%`",
        f"總 PnL：`{s['pnl']:+.2f}%`",
        f"平均：`{s['avg']:+.2f}%/筆`",
        f"最大獲利：`{s['max_win']:+.2f}%`　最大虧損：`{s['max_loss']:+.2f}%`", "",
    ]
    cur_streak = 0; streak_type = None
    max_win_streak = 0; max_loss_streak = 0
    for t in month:
        ct = t.get("close_type")
        is_w = ct in ("TP1","TP2","TP3","LOCK")
        is_l = ct == "SL"
        if is_w:
            if streak_type == "win": cur_streak += 1
            else: streak_type = "win"; cur_streak = 1
            max_win_streak = max(max_win_streak, cur_streak)
        elif is_l:
            if streak_type == "loss": cur_streak += 1
            else: streak_type = "loss"; cur_streak = 1
            max_loss_streak = max(max_loss_streak, cur_streak)
    lines.append(f"🔥 最大連勝：{max_win_streak}　❄️ 最大連敗：{max_loss_streak}")
    lines.append("")
    by_coin: dict = {}
    for t in month:
        c = t.get("coin","?"); by_coin.setdefault(c,[]).append(t)
    if by_coin:
        lines.append("💎 各幣種表現：")
        for c, ts in sorted(by_coin.items(), key=lambda x: -sum(t.get("pnl",0) for t in x[1])):
            sub = _summarize_trades(ts)
            lines.append(f"  {c}: {sub['n']} 筆 · 勝率 `{sub['wr']:.0f}%` · PnL `{sub['pnl']:+.2f}%`")
    # 月度綜合評估
    pf      = _calc_profit_factor(month)
    mdd     = _calc_max_drawdown(month)
    pf_str  = f"`{pf:.2f}`" if pf != float("inf") else "`∞`"
    rr_mo   = calc_avg_actual_rr(month)
    rr_mo_line = ""
    if rr_mo["avg"] is not None:
        rr_mo_line = f"平均加權RR：`{rr_mo['avg']:+.2f}R`（{rr_mo['n_orders']} 筆訂單）"
    lines += [
        "",
        "📐 *月度綜合評估*",
        f"利潤因子：{pf_str}",
        f"最大回撤：`{mdd:.2f}%`",
        f"勝率狀態：{_wr_status(s['wr'])}",
    ] + ([rr_mo_line] if rr_mo_line else []) + [
        "",
        f"⏰ 報告生成：{tw_ts()}",
    ]
    return "\n".join(lines)

def format_learning_report() -> str:
    state   = _load_json(LEARNING_FILE, {})
    buckets = state.get("buckets", {})
    by_coin = state.get("by_coin", {})
    loss_reasons = state.get("loss_reasons", [])
    if not buckets and not by_coin:
        return "🧠 機器人學習狀態\n\n📭 至少需要 5 筆已結束交易才會開始套用學習調整"
    lines = ["🧠 機器人學習狀態", "━━━━━━━━━━━━━━", ""]
    if by_coin:
        lines.append("📊 各幣種戰績：")
        for coin, d in sorted(by_coin.items(), key=lambda x: -x[1].get("total",0))[:12]:
            n = d.get("total",0); w = d.get("win",0); l = d.get("loss",0); be = d.get("be",0)
            wr = w/n*100 if n else 0
            lines.append(f"  {coin}: {n} 筆（勝 {w} / 平 {be} / 敗 {l}，勝率 `{wr:.0f}%`）")
        lines.append("")
    high_wr = [(b,d) for b,d in buckets.items() if d.get("total",0)>=5 and d["win"]/d["total"]>0.6]
    if high_wr:
        lines.append("✅ 高勝率組合（>60%）：")
        for b,d in sorted(high_wr, key=lambda x: -x[1]["win"]/x[1]["total"])[:5]:
            wr = d["win"]/d["total"]*100
            lines.append(f"  `{b}` → {d['total']} 筆，勝率 `{wr:.0f}%`")
        lines.append("")
    low_wr = [(b,d) for b,d in buckets.items() if d.get("total",0)>=5 and d["win"]/d["total"]<0.4]
    if low_wr:
        lines.append("⚠️ 低勝率組合（<40%）：")
        for b,d in sorted(low_wr, key=lambda x: x[1]["win"]/x[1]["total"])[:5]:
            wr = d["win"]/d["total"]*100
            lines.append(f"  `{b}` → {d['total']} 筆，勝率 `{wr:.0f}%`")
        lines.append("")
    if loss_reasons:
        from collections import Counter
        cnt = Counter(r.get("title","?") for r in loss_reasons[-30:])
        lines.append("🔍 最近 30 筆止損主因 TOP3：")
        for title, c in cnt.most_common(3):
            lines.append(f"  {title} × {c}")
        lines.append("")
    lines.append("💡 這些統計每筆交易結算後自動更新；下次相似情境的訊號評分會自動微調")
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════
# 9.65 覆盤分析
# ═════════════════════════════════════════════════════════

def analyze_loss(sig: dict, df_at_loss: list) -> list:
    if not df_at_loss or len(df_at_loss) < 20:
        return [{"code":"INSUFFICIENT","title":"📋 資料不足","detail":"進場後 K 線太少","severity":0}]
    side   = sig["side"]
    expect = 1 if side == "LONG" else -1
    n      = len(df_at_loss)
    df_then = df_at_loss[:max(20, n//3)]
    df_now  = df_at_loss
    reasons = []
    st_then = calc_supertrend(df_then); st_now = calc_supertrend(df_now)
    if st_then == expect and st_now == -expect:
        reasons.append({"code":"TREND_REVERSAL","title":"🔄 趨勢反轉","detail":"Supertrend 進場時順勢，止損前已翻向反向","severity":30})
    rsi_then = calc_rsi(df_then); rsi_now = calc_rsi(df_now)
    if side == "LONG" and rsi_then > 45 and rsi_now < 35 and (rsi_then - rsi_now) > 12:
        reasons.append({"code":"RSI_COLLAPSE","title":"📉 多頭動能瓦解","detail":f"RSI {rsi_then:.0f}→{rsi_now:.0f}（-{rsi_then-rsi_now:.0f}）","severity":25})
    elif side == "SHORT" and rsi_then < 55 and rsi_now > 65 and (rsi_now - rsi_then) > 12:
        reasons.append({"code":"RSI_REBOUND","title":"📈 空頭動能反轉","detail":f"RSI {rsi_then:.0f}→{rsi_now:.0f}（+{rsi_now-rsi_then:.0f}）","severity":25})
    sweep_dir = "SHORT" if side == "LONG" else "LONG"
    if detect_liquidity_sweep(df_now[-12:], sweep_dir):
        reasons.append({"code":"LIQ_SWEEP","title":"🌊 流動性掃蕩","detail":"止損前出現反向假突破插針後快速收回","severity":22})
    atr_then = calc_atr(df_then); atr_now = calc_atr(df_now)
    if atr_then > 0 and atr_now / atr_then > 1.5:
        reasons.append({"code":"VOL_SPIKE","title":"🌪 波動率激增","detail":f"ATR {atr_then:.4f}→{atr_now:.4f}（+{(atr_now/atr_then-1)*100:.0f}%）","severity":18})
    last10 = df_now[-10:]
    against = sum(1 for b in last10 if (side=="LONG" and b["c"]<b["o"]) or (side=="SHORT" and b["c"]>b["o"]))
    if against >= 7:
        reasons.append({"code":"AGAINST_MOMENTUM","title":"💪 持續反向動能","detail":f"出場前 10 根 K 中 {against} 根反向收線","severity":15})
    ob = find_order_block(df_then, side)
    if ob:
        breached = (side=="LONG" and df_now[-1]["c"]<ob["low"]) or (side=="SHORT" and df_now[-1]["c"]>ob["high"])
        if breached:
            reasons.append({"code":"OB_BROKEN","title":"🧱 訂單塊跌破","detail":"進場依據的 SMC 訂單塊已被收盤跌破","severity":20})
    if not reasons:
        reasons.append({"code":"NORMAL_NOISE","title":"📊 正常波動","detail":"未偵測到明確反轉，可能是 ATR 範圍內正常雜訊","severity":5})
    reasons.sort(key=lambda x: -x["severity"])
    return reasons[:3]

def _generate_lessons(reasons: list) -> list:
    advice_map = {
        "TREND_REVERSAL":    "進場後若 Supertrend 翻向反向，建議立即減倉或主動出場",
        "RSI_COLLAPSE":      "RSI 從中性區急跌到超賣（<35）通常代表動能轉換，可作為提前離場信號",
        "RSI_REBOUND":       "RSI 從中性區反彈到超買（>65）代表空頭瓦解，提早平倉避損",
        "LIQ_SWEEP":         "插針型止損若反向 K 隨後出現，多半是主力誘多/誘空，下次 SL 拉遠 0.2 ATR",
        "VOL_SPIKE":         "ATR 突然擴張代表高波動區，建議暫停 1–2 小時或縮小倉位",
        "AGAINST_MOMENTUM":  "反向 K 連續 7 根以上應比原訂 SL 更早主動止損",
        "OB_BROKEN":         "SMC 訂單塊收盤跌破代表結構失效，應立即出場",
        "NORMAL_NOISE":      "本次屬正常雜訊，SL 可能設得太緊，下次 ATR×1.5 → ATR×1.8",
        "INSUFFICIENT":      "進場後資料不足，無法詳細歸因",
    }
    out = []; seen = set()
    for r in reasons[:2]:
        code = r.get("code")
        if code in seen or code not in advice_map: continue
        seen.add(code); out.append(advice_map[code])
    return out

def get_similar_stats(score: int, side: str, detail: dict, funding_rate, coin: str) -> tuple:
    state   = _load_json(LEARNING_FILE, {})
    buckets = state.get("buckets", {})
    key     = f"coin_side:{coin}{side}"
    bd      = buckets.get(key, {})
    return bd.get("total",0), bd.get("win",0), bd.get("loss",0), bd.get("be",0)

def _fmt_postmortem(sig: dict, mode: str, reasons: list, lessons: list, similar_stats: tuple | None = None) -> str:
    coin      = sig["instId"].split("-")[0]
    order_id  = sig.get("order_id","N/A")
    direction = "做多" if sig["side"]=="LONG" else "做空"
    label = {"LOSS":"❌ 止損","BE":"🔒 保本","LOCK":"🔐 鎖利"}.get(mode, "🎯 止盈")
    lines = [
        f"🔍 {coin} 覆盤分析","━━━━━━━━━━━━━━",
        f"🆔 訂單：`{order_id}`",f"⏰ 時間：{tw_ts()}",
        f"方向：{direction}　結算：{label}",f"原始評分：{sig.get('score',0)} 分","",
        "📋 主要原因（依嚴重度）：",
    ]
    for i, r in enumerate(reasons, 1):
        lines.append(f"{i}. {r['title']}"); lines.append(f"   {r['detail']}")
    if lessons:
        lines.append(""); lines.append("💡 下次該怎麼判斷：")
        for l in lessons: lines.append(f"  • {l}")
    if similar_stats:
        n,w,l,be = similar_stats
        if n >= 3:
            lines.append(""); lines.append(f"📊 同類設定歷史：{n} 筆（勝 {w} / 平 {be} / 敗 {l}，勝率 `{w/n*100:.0f}%`）")
    lines.append(""); lines.append("🧠 此次主因已寫入學習資料，下次相似情況評分自動調整")
    return "\n".join(lines)

# ═════════════════════════════════════════════════════════
# 9.7 系統狀態 / 9.8 熔斷 / 9.9 時段過濾
# ═════════════════════════════════════════════════════════

def get_system_state() -> dict:  return _load_json(SYSTEM_STATE_FILE, {})
def set_system_state(state: dict) -> None: _save_json(SYSTEM_STATE_FILE, state)

def check_circuit_breaker(cfg: dict) -> tuple[bool, str, int]:
    cb = cfg.get("circuit_breaker", {})
    if not cb.get("enabled", True):
        return False, "", 0
    history = _load_json(TRADE_HISTORY_FILE, [])
    recent  = [t for t in history if t.get("close_type") in ("SL","BE","LOCK","TP1","TP2","TP3")][-20:]
    if not recent:
        return False, "", 0
    losses = 0; last_loss_time = None
    for t in reversed(recent):
        if t.get("close_type") == "SL":
            losses += 1
            if last_loss_time is None:
                try:
                    last_loss_time = datetime.strptime(t["time"],"%Y-%m-%d %H:%M").replace(tzinfo=TW_TZ)
                except Exception:
                    last_loss_time = tw_now()
        else:
            break
    if losses == 0 or last_loss_time is None:
        return False, "", 0
    elapsed_h = (tw_now() - last_loss_time).total_seconds() / 3600
    hard_n = cb.get("hard_threshold",5); hard_h = cb.get("hard_pause_hours",24)
    soft_n = cb.get("soft_threshold",3); soft_h = cb.get("soft_pause_hours",4)
    if losses >= hard_n and elapsed_h < hard_h:
        return True, f"🚨 硬熔斷觸發\n連續 {losses} 次止損，系統暫停 {hard_h} 小時\n剩餘約 `{hard_h-elapsed_h:.1f}` 小時恢復", losses
    if losses >= soft_n and elapsed_h < soft_h:
        return True, f"⚠️ 軟熔斷觸發\n連續 {losses} 次止損，暫停 {soft_h} 小時\n剩餘約 `{soft_h-elapsed_h:.1f}` 小時恢復", losses
    return False, "", losses

def _in_window(cur_min: int, start_min: int, end_min: int) -> bool:
    if start_min <= end_min: return start_min <= cur_min < end_min
    return cur_min >= start_min or cur_min < end_min

def is_in_news_window(cfg: dict) -> tuple[bool, str]:
    now = tw_now()
    for nb in cfg.get("news_blackouts", []):
        try:
            start = datetime.fromisoformat(nb["start"]); end = datetime.fromisoformat(nb["end"])
            if start.tzinfo is None: start = start.replace(tzinfo=TW_TZ); end = end.replace(tzinfo=TW_TZ)
            if start <= now <= end: return True, nb.get("reason","新聞事件")
        except Exception: continue
    auto = cfg.get("auto_news_blackout", {})
    if auto.get("nfp", True):
        if now.weekday() == 4 and now.day <= 7:
            if 21*60+25 <= now.hour*60+now.minute < 22*60+30:
                return True, "NFP 非農（自動偵測）"
    if auto.get("cpi", True):
        if 10 <= now.day <= 16:
            if 21*60+25 <= now.hour*60+now.minute < 22*60+30:
                return True, "CPI 數據時段（自動偵測）"
    return False, ""

def is_blackout_time(cfg: dict) -> tuple[bool, str]:
    windows = cfg.get("blackout_windows_tw", [])
    now = tw_now(); cur_min = now.hour * 60 + now.minute
    for w in windows:
        try:
            sh, sm = map(int, w["start"].split(":")); eh, em = map(int, w["end"].split(":"))
            if _in_window(cur_min, sh*60+sm, eh*60+em):
                return True, w.get("reason","禁止時段")
        except Exception: continue
    return False, ""


# ═════════════════════════════════════════════════════════
# 10. 訊號追蹤器
# ═════════════════════════════════════════════════════════

class SignalTracker:
    def __init__(self, filepath: str = ACTIVE_SIGNALS_FILE):
        self.filepath   = filepath
        self.signals: dict = _load_json(filepath, {})
        self.transitions = 0

    def _save(self) -> None:
        _save_json(self.filepath, self.signals)

    def add(self, signal: dict, active: bool = False) -> tuple[str, str]:
        order_id = f"{int(time.time())}-{uuid.uuid4().hex[:8].upper()}"
        key      = f"{signal['instId']}{signal['side']}{order_id}"
        now_ts   = time.time()
        self.signals[key] = {
            **signal,
            "order_id":        order_id,
            "status":          "ACTIVE" if active else "PENDING",
            "hit_tp1":         False,
            "hit_tp2":         False,
            "hit_tp3":         False,
            "activated_at":    now_ts if active else None,
            "entry_message_id":None,
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
        try:
            cfg    = load_config()
            pm_cfg = cfg.get("post_mortem", {})
            if not pm_cfg.get("enabled", True): return
            if mode != "LOSS" and pm_cfg.get("loss_only", False): return
            activated_at = sig.get("activated_at") or sig.get("created") or 0
            all_candles  = fetch_candles_full(sig["instId"], limit=100)
            df_at_loss   = [
                {"ts":c["ts"],"o":c["o"],"h":c["h"],"l":c["l"],"c":c["c"],"v":c["v"]}
                for c in all_candles if (c["ts"]/1000) >= (activated_at - 900)
            ]
            if len(df_at_loss) < 10: return
            reasons = analyze_loss(sig, df_at_loss)
            lessons = _generate_lessons(reasons)
            coin    = sig["instId"].split("-")[0]
            similar = get_similar_stats(sig.get("score",0), sig["side"], sig.get("detail",{}), sig.get("funding_rate"), coin)
            msg     = _fmt_postmortem(sig, mode, reasons, lessons, similar)
            send_tg(msg, reply_to_message_id=sig.get("entry_message_id"))
            if mode == "LOSS":
                record_loss_reason(coin, sig["side"], reasons)
        except Exception as e:
            logging.error(f"❌ 覆盤分析失敗：{e}")

    def has_open_position(self, instId: str) -> bool:
        for sig in self.signals.values():
            if sig.get("instId") == instId and sig.get("status") in ("PENDING","ACTIVE","BE","TRAIL"):
                return True
        return False

    def check_all(self) -> None:
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
        try:
            price = fetch_price(sig["instId"])
            if price <= 0: return False
            sig["current_price"] = price
            status = sig["status"]
            if status == "PENDING":
                return self._check_pending(sig, price)
            if status not in ("ACTIVE","BE","TRAIL"):
                return False
            all_candles  = fetch_candles_full(sig["instId"])
            last_ts_s    = sig.get("last_checked_ts") or sig.get("activated_at") or sig.get("created") or 0
            last_ts_ms   = int(last_ts_s * 1000)
            new_candles  = [c for c in all_candles if c["ts"] > last_ts_ms]
            for c in new_candles:
                if self._process_candle(sig, c):
                    return True
            confirmed = [c for c in new_candles if c["confirmed"]]
            if confirmed:
                sig["last_checked_ts"] = max(c["ts"] for c in confirmed) / 1000.0
                self._save()
            return False
        except Exception as e:
            logging.error(f"❌ check_one [{key}] 錯誤：{e}")
            return False

    def _check_pending(self, sig: dict, price: float) -> bool:
        coin     = sig["instId"].split("-")[0]
        order_id = sig.get("order_id","N/A")
        side     = sig["side"]
        entry, sl = sig["entry"], sig["sl"]
        tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
        kb = _order_keyboard(order_id)
        if time.time() > sig["expires"]:
            send_tg(f"⏰ {coin} 訊號過期\n🆔 訂單：`{order_id}`\n進場 `{entry:.4f}` 未觸發，已自動取消")
            self.transitions += 1
            return True
        in_zone = (
            (side=="LONG"  and entry*(1-0.006) <= price <= entry*(1+0.002)) or
            (side=="SHORT" and entry*(1-0.002) <= price <= entry*(1+0.006))
        )
        if in_zone:
            now_ts = time.time()
            sig["status"] = "ACTIVE"; sig["activated_at"] = now_ts; sig["last_checked_ts"] = now_ts
            msg_id = send_tg(
                _fmt_entry(coin, side, order_id, price, entry, sl, tp1, tp2, tp3, sig["score"], sig.get("funding_rate")),
                reply_markup=kb,
            )
            if msg_id: sig["entry_message_id"] = msg_id
            self._save(); self.transitions += 1
        return False

    def _process_candle(self, sig: dict, candle: dict) -> bool:
        side   = sig["side"]
        entry  = sig["entry"]
        sl     = sig["sl"]
        tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
        coin     = sig["instId"].split("-")[0]
        order_id = sig.get("order_id","N/A")
        reply_to = sig.get("entry_message_id")
        kb       = _order_keyboard(order_id)
        ch, cl, cc = candle["h"], candle["l"], candle["c"]
        if side == "LONG":
            favor_hit   = lambda t: ch >= t
            against_hit = lambda t: cl <= t
            wick_favor  = lambda t: cc < t  and ch >= t
            wick_against= lambda t: cc > t  and cl <= t
        else:
            favor_hit   = lambda t: cl <= t
            against_hit = lambda t: ch >= t
            wick_favor  = lambda t: cc > t  and cl <= t
            wick_against= lambda t: cc < t  and ch >= t

        if not sig.get("hit_tp1") and favor_hit(tp1):
            sig["hit_tp1"] = True; sig["sl"] = entry; sig["status"] = "BE"; sl = entry
            pnl = (tp1-entry)/entry*100 if side=="LONG" else (entry-tp1)/entry*100
            send_tg(_fmt_tp(coin,side,order_id,"TP1",tp1,pnl,1.5,wick_triggered=wick_favor(tp1)), reply_markup=kb, reply_to_message_id=reply_to)
            record_trade(coin,side,order_id,entry,tp1,"TP1",sig["score"],sig)
            self._save(); self.transitions += 1

        if not sig.get("hit_tp2") and favor_hit(tp2):
            sig["hit_tp2"] = True; sig["sl"] = tp1; sig["status"] = "TRAIL"; sl = tp1
            pnl = (tp2-entry)/entry*100 if side=="LONG" else (entry-tp2)/entry*100
            send_tg(_fmt_tp(coin,side,order_id,"TP2",tp2,pnl,3.0,wick_triggered=wick_favor(tp2)), reply_markup=kb, reply_to_message_id=reply_to)
            record_trade(coin,side,order_id,entry,tp2,"TP2",sig["score"],sig)
            self._save(); self.transitions += 1

        if not sig.get("hit_tp3") and favor_hit(tp3):
            sig["hit_tp3"] = True
            pnl = (tp3-entry)/entry*100 if side=="LONG" else (entry-tp3)/entry*100
            send_tg(_fmt_tp(coin,side,order_id,"TP3",tp3,pnl,5.0,wick_triggered=wick_favor(tp3)), reply_markup=kb, reply_to_message_id=reply_to)
            record_trade(coin,side,order_id,entry,tp3,"TP3",sig["score"],sig)
            self.transitions += 1
            return True

        if against_hit(sl):
            if sig.get("hit_tp2"):   mode, r_value, close_type = "LOCK", 1.5, "LOCK"
            elif sig.get("hit_tp1"): mode, r_value, close_type = "BE",   0.0, "BE"
            else:                    mode, r_value, close_type = "LOSS",-1.0, "SL"
            pnl = (sl-entry)/entry*100 if side=="LONG" else (entry-sl)/entry*100
            send_tg(_fmt_sl(coin,side,order_id,sl,pnl,mode,r_value,wick_triggered=wick_against(sl)), reply_markup=kb, reply_to_message_id=reply_to)
            record_trade(coin,side,order_id,entry,sl,close_type,sig["score"],sig)
            self._send_postmortem(sig, mode)
            self.transitions += 1
            return True
        return False

    def send_position_updates(self) -> None:
        cnt = 0
        for sig in self.signals.values():
            if sig["status"] not in ("ACTIVE","BE","TRAIL"): continue
            price = fetch_price(sig["instId"])
            if price <= 0: continue
            send_tg(_fmt_position(sig, price), reply_markup=_order_keyboard(sig.get("order_id","")), reply_to_message_id=sig.get("entry_message_id"))
            cnt += 1
        if cnt:
            logging.info(f"📊 已發送 {cnt} 筆持倉更新")

    def get_position_stats(self) -> str:
        positions = list(self.signals.values())
        if not positions:
            return "📭 目前無持倉\n\n🔄 系統持續掃描中..."
        lines = [f"📊 追蹤中訊號（{len(positions)} 筆）", "═" * 22, ""]
        for i, p in enumerate(positions):
            price    = fetch_price(p["instId"]) or p["entry"]
            coin     = p["instId"].split("-")[0]
            coin_emoji = "🟠" if "BTC" in p["instId"] else "🔷" if "ETH" in p["instId"] else "🟣"
            side_emoji = "🟢" if p["side"]=="LONG" else "🔴"
            order_id   = p.get("order_id","N/A")
            pnl = (price-p["entry"])/p["entry"]*100 if p["side"]=="LONG" else (p["entry"]-price)/p["entry"]*100
            pnl_emoji  = "🟢" if pnl >= 0 else "🔴"
            progress   = "🏆 TP3" if p.get("hit_tp3") else "🥈 TP2" if p.get("hit_tp2") else "🥇 TP1" if p.get("hit_tp1") else "⏳ 等待"
            lines.append(
                f"{coin_emoji} #{coin} · {side_emoji} {p['side']} · {p.get('score',0)} 分\n"
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
# 11.1 v17 新增：真實RR / 時段分層 / 日虧熔斷
# ═════════════════════════════════════════════════════════

# ── A. 加權實際 RR 計算 ────────────────────────────────
def calc_order_actual_rr(history: list, order_id: str) -> float | None:
    """
    依訂單 ID 找出所有出場記錄，按比例計算加權 RR。
    分批出場權重：TP1=30%、TP2=30%、TP3=40%；
    一次性出場（SL/BE/LOCK）= 100%。
    """
    events = [t for t in history if t.get("order_id") == order_id]
    if not events:
        return None
    weighted = 0.0; total_wt = 0.0
    for e in events:
        rr = e.get("exit_rr")
        wt = e.get("exit_weight", 1.0)
        if rr is not None:
            weighted += rr * wt
            total_wt  += wt
    return round(weighted / total_wt, 3) if total_wt > 0 else None


def calc_avg_actual_rr(history: list) -> dict:
    """統計一批交易的平均加權 RR（分贏/虧）"""
    order_ids = list({t.get("order_id") for t in history if t.get("order_id")})
    rrs = [r for oid in order_ids if (r := calc_order_actual_rr(history, oid)) is not None]
    if not rrs:
        return {"avg": None, "win_rr": None, "loss_rr": None}
    wins   = [r for r in rrs if r > 0]
    losses = [r for r in rrs if r <= 0]
    return {
        "avg":     round(sum(rrs) / len(rrs), 3),
        "win_rr":  round(sum(wins)   / len(wins),   3) if wins   else None,
        "loss_rr": round(sum(losses) / len(losses), 3) if losses else None,
        "n_orders": len(rrs),
    }


# ── B. 每日最大虧損熔斷 ────────────────────────────────
def get_daily_pnl(date: str | None = None) -> float:
    """當日已結算的累積 PnL（%）"""
    if date is None:
        date = tw_now().strftime("%Y-%m-%d")
    history = _load_json(TRADE_HISTORY_FILE, [])
    # 只計算最終出場（SL/BE/LOCK/TP3）；TP1/TP2 為部分出場，用權重折算
    total = 0.0
    for t in history:
        if t.get("date") != date:
            continue
        pnl = t.get("pnl", 0.0)
        wt  = t.get("exit_weight", 1.0)
        total += pnl * wt
    return round(total, 3)


def is_daily_loss_limit_reached(cfg: dict) -> tuple[bool, str]:
    dlcfg = cfg.get("daily_loss_limit", {})
    if not dlcfg.get("enabled", True):
        return False, ""
    threshold = dlcfg.get("max_loss_pct", -3.0)
    daily_pnl = get_daily_pnl()
    if daily_pnl <= threshold:
        return True, (
            f"🔴 日虧熔斷觸發\n"
            f"今日累積虧損 `{daily_pnl:+.2f}%` ≤ 限額 `{threshold:+.1f}%`\n"
            f"本日停止開新倉，繼續監控既有持倉"
        )
    remaining = abs(threshold) - abs(daily_pnl)
    return False, f"今日PnL `{daily_pnl:+.2f}%`（距熔斷還差 `{remaining:.2f}%`）"


# ── C. 時段勝率分層 ────────────────────────────────────
def get_session_wr_boost(cfg: dict) -> tuple[int, str]:
    """
    依當前台灣時間時段查歷史勝率，樣本足夠時自動提高門檻。
    時段：亞盤黎明(0-6) / 亞盤白天(6-14) / 歐盤(14-21) / 美盤(21-24)
    """
    sf = cfg.get("session_wr_filter", {})
    if not sf.get("enabled", True):
        return 0, ""
    min_samples = sf.get("min_samples", 10)
    low_boost   = sf.get("low_wr_boost",  8)
    mid_boost   = sf.get("mid_wr_boost",  5)

    cur_sess = _bucket_session_tw()   # "sess:asia_dawn" 等
    state    = _load_json(LEARNING_FILE, {})
    bucket   = state.get("buckets", {}).get(cur_sess, {})
    total    = bucket.get("total", 0)

    if total < min_samples:
        return 0, f"時段 {cur_sess}：樣本不足（{total}/{min_samples}），暫不調整"

    wins = bucket.get("win", 0)
    wr   = wins / total

    session_names = {
        "sess:asia_dawn": "亞盤黎明 0–6時",
        "sess:asia_day":  "亞盤白天 6–14時",
        "sess:europe":    "歐盤 14–21時",
        "sess:us":        "美盤 21–24時",
    }
    sname = session_names.get(cur_sess, cur_sess)

    if wr < 0.55:
        return low_boost, f"⚠️ {sname} 歷史勝率 {wr:.0%}（{total}筆），門檻 +{low_boost}"
    if wr < 0.68:
        return mid_boost, f"📊 {sname} 歷史勝率 {wr:.0%}（{total}筆），門檻 +{mid_boost}"
    return 0, f"✅ {sname} 歷史勝率 {wr:.0%}（{total}筆），門檻無調整"


# ═════════════════════════════════════════════════════════
# 11.0 v16 新增：每日交易限制 / 勝率守衛 / 相關性過濾
# ═════════════════════════════════════════════════════════

def count_daily_trades(date: str | None = None) -> int:
    """統計當天已開倉的訊號數（含 PENDING/已結算）"""
    if date is None:
        date = tw_now().strftime("%Y-%m-%d")
    history = _load_json(TRADE_HISTORY_FILE, [])
    return sum(1 for t in history if t.get("date") == date)


def is_daily_limit_reached(cfg: dict) -> tuple[bool, str]:
    """是否已達每日開倉上限"""
    limit = cfg.get("daily_max_trades", 15)
    today_count = count_daily_trades()
    if today_count >= limit:
        return True, f"📊 今日已開 {today_count} 張（上限 {limit}），不再開新倉"
    return False, f"今日已開 {today_count}/{limit} 張"


def check_win_rate_guardian(cfg: dict) -> tuple[int, str]:
    """勝率守衛：最近 N 筆勝率不足時自動提高評分門檻
    回傳 (額外門檻加成, 說明訊息)
    """
    guardian = cfg.get("win_rate_guardian", {})
    if not guardian.get("enabled", True):
        return 0, ""
    lookback = guardian.get("lookback", 20)
    min_wr   = guardian.get("min_wr", 0.70)
    boost    = guardian.get("threshold_boost", 5)
    history  = _load_json(TRADE_HISTORY_FILE, [])
    recent   = [t for t in history if t.get("close_type") in
                ("SL","BE","LOCK","TP1","TP2","TP3")][-lookback:]
    if len(recent) < 5:
        return 0, ""
    wins = sum(1 for t in recent if t.get("close_type") in ("TP1","TP2","TP3","LOCK"))
    wr   = wins / len(recent)
    if wr < min_wr - 0.10:   # 勝率低於目標 10% 以上 → 雙倍提高
        adj = boost * 2
        msg = f"🛡 勝率守衛：近 {len(recent)} 筆勝率 {wr:.0%} 嚴重不足，門檻 +{adj}"
    elif wr < min_wr:         # 輕微不足 → 單倍提高
        adj = boost
        msg = f"🛡 勝率守衛：近 {len(recent)} 筆勝率 {wr:.0%} 略低，門檻 +{adj}"
    else:
        adj = 0
        msg = f"✅ 勝率健康：近 {len(recent)} 筆 {wr:.0%}"
    return adj, msg


# 相關性幣種組（同組不同時開多筆）
_CORR_GROUPS = [
    {"BTC-USDT-SWAP", "ETH-USDT-SWAP"},
    {"SOL-USDT-SWAP", "AVAX-USDT-SWAP", "NEAR-USDT-SWAP"},
    {"XRP-USDT-SWAP", "ADA-USDT-SWAP", "DOT-USDT-SWAP"},
]

def has_correlated_position(tracker, instId: str) -> bool:
    """同組高相關幣種已有持倉時，跳過避免集中風險"""
    for group in _CORR_GROUPS:
        if instId in group:
            for other in group:
                if other != instId and tracker.has_open_position(other):
                    return True
    return False


# ═════════════════════════════════════════════════════════
# 11. 主掃描 + Monitor 模式
# ═════════════════════════════════════════════════════════

def run_monitor(tracker: SignalTracker, in_run_polls: int = 1, poll_interval: int = 30) -> None:
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
    logging.info("🚀 開始掃描...")
    cfg          = load_config()
    coins        = cfg.get("coins", ALL_COINS)
    max_signals  = cfg.get("max_signals", MAX_SIGNALS)
    score_thr    = cfg.get("score_threshold", SCORE_THRESHOLD)
    cooldown_h   = cfg.get("cooldown_hours", COOLDOWN_HOURS)
    expire_h     = cfg.get("signal_expire_hours", SIGNAL_EXPIRE_HOURS)
    atr_max      = cfg.get("atr_max_pct", 0.04)
    pv_cfg       = cfg.get("price_verification", {})
    pv_enabled        = pv_cfg.get("enabled", True)
    pv_max_dev        = pv_cfg.get("max_deviation_pct", 0.5)
    pv_block_unverified = pv_cfg.get("block_on_unverified", False)
    state = get_system_state()

    # 1. 熔斷
    paused, msg, losses = check_circuit_breaker(cfg)
    if paused:
        if not state.get("circuit_active"):
            send_tg(msg)
            state["circuit_active"] = True; state["circuit_since"] = time.time()
            set_system_state(state)
        logging.warning(f"🛑 熔斷中（連敗 {losses}）→ 仍持續監控既有訊號")
        tracker.check_all(); tracker.send_position_updates()
        return 0
    else:
        if state.get("circuit_active"):
            send_tg("✅ 熔斷已解除\n系統恢復正常掃描，繼續加油 🚀")
            state["circuit_active"] = False; state["circuit_since"] = None
            set_system_state(state)

    # 2. 關鍵時段
    blocked, btime_reason = is_blackout_time(cfg)
    if blocked:
        logging.info(f"🕒 禁止交易時段（{btime_reason}），不開新單但繼續監控")
        tracker.check_all(); tracker.send_position_updates()
        return 0

    # 2.5 新聞事件
    in_news, news_reason = is_in_news_window(cfg)
    if in_news:
        logging.info(f"📰 新聞事件時段（{news_reason}），不開新單但繼續監控")
        tracker.check_all(); tracker.send_position_updates()
        return 0

    # 2.7 每日開倉上限
    daily_reached, daily_msg = is_daily_limit_reached(cfg)
    if daily_reached:
        logging.info(daily_msg)
        tracker.check_all(); tracker.send_position_updates()
        return 0

    # 2.75 每日最大虧損熔斷
    loss_reached, loss_msg = is_daily_loss_limit_reached(cfg)
    if loss_reached:
        logging.warning(loss_msg)
        send_tg(loss_msg) if not get_system_state().get("daily_loss_notified") else None
        st2 = get_system_state(); st2["daily_loss_notified"] = True; set_system_state(st2)
        tracker.check_all(); tracker.send_position_updates()
        return 0
    else:
        st2 = get_system_state()
        if st2.get("daily_loss_notified"):
            st2["daily_loss_notified"] = False; set_system_state(st2)

    # 2.8 勝率守衛：動態提升門檻
    wr_boost, wr_msg = check_win_rate_guardian(cfg)
    if wr_boost > 0:
        logging.info(wr_msg)
        score_thr += wr_boost

    # 2.9 時段勝率分層：依當前時段歷史表現微調門檻
    sess_boost, sess_msg = get_session_wr_boost(cfg)
    if sess_boost > 0:
        logging.info(sess_msg)
        score_thr += sess_boost
    else:
        logging.info(sess_msg)

    # 3. 掃描每個幣種
    sent = 0
    for instId in coins:
        if sent >= max_signals:
            break
        if tracker.has_open_position(instId):
            logging.info(f"[{instId}] 已有未平倉訊號，跳過")
            continue
        if has_correlated_position(tracker, instId):
            logging.info(f"[{instId}] 同組相關幣種已有持倉，跳過集中風險")
            continue
        if is_cooling(instId, cooldown_h):
            logging.info(f"[{instId}] 冷卻中，跳過")
            continue
        try:
            okx_price = fetch_price(instId)
            if okx_price <= 0:
                logging.warning(f"[{instId}] 無法取得 OKX 價格")
                continue
            if pv_enabled:
                ok, tv_price, diff = verify_price(instId, okx_price, pv_max_dev, pv_block_unverified)
                if not ok:
                    if tv_price is not None:
                        send_tg(
                            f"⚠️ {instId.split('-')[0]} 價格異常\n"
                            f"OKX `{okx_price:.4f}` vs TV `{tv_price:.4f}`\n"
                            f"偏離 `{diff:.3f}%` > 閾值 `{pv_max_dev}%`\n⏸ 本輪跳過"
                        )
                    continue
            df = fetch_candles(instId)
            if df is None:
                continue
            funding = fetch_funding_rate(instId)
            signal  = generate_signal(instId, df, okx_price, funding, score_threshold=score_thr, atr_max_pct=atr_max, signal_expire_hours=expire_h)
            if not signal:
                continue
            in_zone = (
                (signal["side"]=="LONG"  and signal["entry"]*(1-0.006) <= okx_price <= signal["entry"]*(1+0.002)) or
                (signal["side"]=="SHORT" and signal["entry"]*(1-0.002) <= okx_price <= signal["entry"]*(1+0.006))
            )
            key, order_id = tracker.add(signal, active=in_zone)
            if in_zone:
                msg_id = send_tg(
                    _fmt_entry(instId.split("-")[0], signal["side"], order_id, okx_price,
                               signal["entry"], signal["sl"], signal["tp1"], signal["tp2"], signal["tp3"],
                               signal["score"], funding),
                    reply_markup=_order_keyboard(order_id),
                )
                tracker.set_entry_message_id(key, msg_id)
                logging.info(f"✅ {instId} 進場通知已送出，訂單 {order_id}")
            else:
                send_tg(
                    f"📍 {instId.split('-')[0]} 訊號就位\n"
                    f"🆔 訂單：`{order_id}`\n⏰ 時間：{tw_ts()}\n"
                    f"方向：{'做多' if signal['side']=='LONG' else '做空'}\n"
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

    tracker.check_all()
    tracker.send_position_updates()
    logging.info(f"✅ 掃描完成，本輪新增 {sent} 筆訊號")
    return sent

# ═════════════════════════════════════════════════════════
# 12. 主入口
# ═════════════════════════════════════════════════════════

def main() -> None:
    try:
        logging.info("=" * 50)
        logging.info("🤖 Alpha Oracle Pro v17.0 啟動")
        logging.info(f"⏰ 台灣時間：{tw_ts()}")
        logging.info("=" * 50)
        tracker = SignalTracker(ACTIVE_SIGNALS_FILE)
        if len(sys.argv) > 1:
            cmd = sys.argv[1]
            if cmd in ("/stats","/持倉","stats"):
                send_tg(tracker.get_position_stats()); return
            if cmd in ("/learning","/學習","/coach","learning"):
                send_tg(format_learning_report()); return
            if cmd in ("/daily","/日報","daily"):
                send_tg(format_daily_report(sys.argv[2] if len(sys.argv)>2 else None)); return
            if cmd in ("/monthly","/月報","monthly"):
                send_tg(format_monthly_report(sys.argv[2] if len(sys.argv)>2 else None)); return
            if cmd in ("monitor","/monitor","/監控"):
                polls    = int(sys.argv[2]) if len(sys.argv) > 2 else 1
                interval = int(sys.argv[3]) if len(sys.argv) > 3 else 30
                run_monitor(tracker, in_run_polls=polls, poll_interval=interval)
                return
        run_scan(tracker)
        logging.info("🎉 程式執行完成")
    except Exception as e:
        logging.error(f"🔥 系統錯誤：{e}")
        import traceback; traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
