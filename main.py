#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro v14.1 — 高勝率專業交易版（繁體中文）
══════════════════════════════════════════════════════════════════════
✨ v14.1 勝率優化重點：
  🎯 進場門檻提升至 78 分（原 68），過濾低品質訊號
  🕒 MTF 多時框強制順勢：4H 反向直接淘汰，1H+4H 同向才給滿分
  📊 量能硬門檻：低於 1.2 倍均量直接 -99 分淘汰
  🧱 結構確認：至少 2 個核心條件（OB/FVG/流動性/PA）才允許進場
  🕐 活躍時段過濾：僅在亞/歐/美盤高流動時段生成新訊號
  🪙 幣種聚焦模式：可設定只交易 BTC/ETH/SOL 等高流動幣種
  📉 動態冷卻：勝率<60% 時自動延長冷卻時間
  📊 勝率監控：連續 5 筆勝率<50% 自動暫停並通知

✨ v14.0 既有功能（完整保留）：
  🕒 多時框共振 + 📊 量能確認 + 🌐 市場狀態識別 + 🎯 動態 TP
  📰 新聞事件過濾 + 🌀 進場時機偵測 + 🧬 KNN 學習 + 📈 日報/月報
  ⚡ monitor 高頻監控 + 🔍 覆盤分析 + 🧠 學習機制 + 🆕 價格雙源驗證
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
from collections import Counter


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
MAX_SIGNALS = _get_env_int("MAX_SIGNALS", 2)          # 🔺 優化：3→2
SCORE_THRESHOLD = _get_env_int("SETUP_SCORE_THRESHOLD", 78)  # 🔺 優化：68→78

SIGNAL_EXPIRE_HOURS = 24
COOLDOWN_HOURS = 4                                     # 🔺 優化：2→4

ACTIVE_SIGNALS_FILE = "active_signals.json"
TRADE_HISTORY_FILE = "trade_history.json"
COOLDOWN_FILE = "signal_cooldown.json"
CONFIG_FILE = "config.json"
SYSTEM_STATE_FILE = "system_state.json"
LEARNING_FILE = "learning_state.json"
WINRATE_MONITOR_FILE = "winrate_monitor.json"          # 🔺 新增：勝率監控

# 記憶體快取（同一輪執行內共用，跨輪不持久）
_price_cache: dict = {}

# ═════════════════════════════════════════════════════════
# 1.5 預設配置（config.json 不存在時的 fallback）
# ═════════════════════════════════════════════════════════
DEFAULT_CONFIG: dict = {
    "coins": ALL_COINS,
    "focus_coins": ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"],  # 🔺 新增：聚焦幣種
    "max_signals": 2,
    "score_threshold": 78,                              # 🔺 優化：68→78
    "cooldown_hours": 4,                                # 🔺 優化：2→4
    "signal_expire_hours": 24,
    "atr_max_pct": 0.04,
    "post_mortem": {
        "enabled": True,
        "loss_only": False,
    },
    "learning": {
        "enabled": True,
        "knn_enabled": True,
        "min_samples": 5,
        "max_score_adjust": 10,
    },
    "news_blackouts": [],
    "auto_news_blackout": {
        "nfp": True,
        "cpi": True,
    },
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
    # 🔺 新增：活躍交易時段（台灣時間）
    "active_hours_tw": [
        {"start": "09:00", "end": "11:30"},   # 亞洲盤活躍
        {"start": "15:00", "end": "17:30"},   # 歐盤開盤
        {"start": "21:00", "end": "23:30"}    # 美盤開盤
    ],
    "blackout_windows_tw": [
        {"start": "07:50", "end": "08:10", "reason": "資金費率結算（00 UTC）"},
        {"start": "15:50", "end": "16:10", "reason": "資金費率結算（08 UTC）"},
        {"start": "23:50", "end": "00:10", "reason": "資金費率結算（16 UTC）"},
        {"start": "21:25", "end": "21:45", "reason": "美股開盤波動"},
        {"start": "02:00", "end": "02:30", "reason": "FOMC 公布時段"},
    ],
    # 🔺 新增：勝率監控設定
    "winrate_monitor": {
        "enabled": True,
        "sample_size": 5,           # 檢視最近幾筆
        "min_winrate": 0.50,        # 低於此勝率觸發警告
        "pause_hours": 2            # 觸發後暫停幾小時
    }
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
    """📤 發送 Telegram 通知 → 回傳 message_id（失敗回 None）"""
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
            json=payload,
            timeout=8,
        )
        if r.status_code == 200:
            return r.json().get("result", {}).get("message_id")
        logging.error(f"❌ TG API 回應碼 {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logging.error(f"❌ TG 發送失敗：{e}")
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
) -> str:
    """📌 進場通知"""
    direction = "做多" if side == "LONG" else "做空"
    emoji = "🟢" if side == "LONG" else "🔴"
    grade = "🔥 A+ 極強" if score >= 90 else "⭐ A 強力" if score >= 82 else "✅ B+ 合格"

    tp1_pct = (tp1 - entry) / entry * 100 * (1 if side == "LONG" else -1)
    tp2_pct = (tp2 - entry) / entry * 100 * (1 if side == "LONG" else -1)
    tp3_pct = (tp3 - entry) / entry * 100 * (1 if side == "LONG" else -1)
    sl_pct = (sl - entry) / entry * 100

    funding_line = ""
    if funding_rate is not None:
        funding_line = f"💰 資金費率：`{funding_rate * 100:+.4f}%`\n"

    return (
        f"{emoji} *{coin} 進場提醒* {grade}\n"
        f"━━━━━━━━━━━━━━\n"
        f"🆔 訂單編號：`{order_id}`\n"
        f"⏰ 時間：{tw_ts()}\n"
        f"方向：{direction}\n"
        f"進場價：`{entry:.4f}`\n"
        f"當前價：`{price:.4f}`\n"
        f"評分：*{score} 分*\n"
        f"{funding_line}\n"
        f"🎯 止盈目標：\n"
        f"  TP1 `{tp1:.4f}` ({tp1_pct:+.2f}%)\n"
        f"  TP2 `{tp2:.4f}` ({tp2_pct:+.2f}%)\n"
        f"  TP3 `{tp3:.4f}` ({tp3_pct:+.2f}%)\n"
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
    """🪡 抓最近 N 根 K 線（含未收線）並按時間升序排序，每輪掃描共用 30 秒快取"""
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
    """📡 從 TradingView 抓取即時價格（OKX 永續合約）"""
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
    """⚖️ 雙來源價格驗證 → (是否通過，TV 價格，偏離百分比)"""
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
    """🧱 訂單塊（OB）"""
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
    """⚡ 公允價值缺口（FVG）"""
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
    """📊 偵測 Pin Bar 或吞沒形態"""
    if len(df) < 2:
        return False
    last, prev = df[-1], df[-2]
    body = abs(last["c"] - last["o"])
    upper = last["h"] - max(last["c"], last["o"])
    lower = min(last["c"], last["o"]) - last["l"]

    if body > 0:
        if side == "LONG" and lower > body * 2 and lower > upper:
            return True
        if side == "SHORT" and upper > body * 2 and upper > lower:
            return True

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
    """💧 流動性掃蕩"""
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


# 🔥【優化 1】MTF 強制順勢：4H 反向直接淘汰
def calc_mtf_alignment(mtf: dict, side: str) -> tuple[int, str]:
    """🎯 多時框共振評分（最高 +15）→ (分數，說明)
    
    🔥 v14.1 優化：4H 反向直接 -99 分淘汰，確保大週期順勢
    """
    expect = 1 if side == "LONG" else -1
    h1 = mtf.get("1H", {}).get("supertrend", 0)
    h4 = mtf.get("4H", {}).get("supertrend", 0)
    
    # 🔥 4H 反向直接淘汰（-99 分確保不會進場）
    if h4 == -expect:
        return -99, f"4H 反向淘汰 (1H={'順' if h1==expect else '反' if h1==-expect else '中'})"
    
    # 1H+4H 都順勢才給滿分
    if h1 == expect and h4 == expect:
        return 15, "1H+4H 雙順 ✅"
    elif h1 == expect or h4 == expect:
        return 5, f"單一時框順勢 (1H:{'順' if h1==expect else '中'}, 4H:{'順' if h4==expect else '中'})"
    
    # 都震盪或反向
    return -5, "震盪/反向"


# 🔥【優化 2】量能硬門檻：低於 1.2 倍直接淘汰
def calc_volume_quality(df: list, lookback: int = 20) -> tuple[float, int]:
    """📊 成交量確認：最後 K 線量 vs 前 N 期均量 → (倍數，評分 -99~+8)
    
    🔥 v14.1 優化：低於 1.2 倍均量直接 -99 分淘汰，過濾假突破
    """
    if len(df) < lookback + 1:
        return 1.0, 0
    seg = df[-(lookback + 1):-1]
    avg = sum(c["v"] for c in seg) / lookback
    if avg <= 0:
        return 1.0, 0
    ratio = df[-1]["v"] / avg
    
    # 🔥 沒量直接淘汰
    if ratio < 1.2:
        return round(ratio, 2), -99
    
    if ratio >= 2.0:
        s = 8
    elif ratio >= 1.5:
        s = 5
    elif ratio >= 1.2:
        s = 2
    else:
        s = 0
    return round(ratio, 2), s


def adjust_tp_by_sr(
    entry: float, side: str, tp_levels: list, df: list
) -> tuple[list, list]:
    """🎯 動態 TP：若固定 R 倍 TP 落在強 S/R 前方，把 TP 拉到關鍵位前"""
    sup, res = calc_snr(df, lookback=100)
    out = list(tp_levels)
    notes = []
    if side == "LONG":
        for i, tp in enumerate(out):
            if tp > res * 1.001:
                new_tp = res * 0.998
                if new_tp > entry:
                    notes.append(
                        f"TP{i + 1} 由 {tp:.4f} 校正到 {new_tp:.4f}（避開阻力 {res:.4f}）"
                    )
                    out[i] = new_tp
    else:
        for i, tp in enumerate(out):
            if tp < sup * 0.999:
                new_tp = sup * 1.002
                if new_tp < entry:
                    notes.append(
                        f"TP{i + 1} 由 {tp:.4f} 校正到 {new_tp:.4f}（避開支撐 {sup:.4f}）"
                    )
                    out[i] = new_tp
    return out, notes


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
# 7. 評分系統（規格 100 分制 + v14.1 優化）
# ═════════════════════════════════════════════════════════
def calc_score(
    df: list,
    side: str,
    current_price: float,
    mtf: dict | None = None,
    instId: str | None = None,
) -> tuple[int, str, dict]:
    """總分 = 趨勢 30 + RSI25 + OB20 + FVG15 + SNR5 + PA5 + 流動性 5 + 動能 5 + MTF15 + Volume8 = 最高 138
    
    🔥 v14.1 優化：MTF 反向/量能不足直接 -99 分淘汰
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

    # 🎯 MTF 多時框共振 (-99 / -5 ~ +15) 🔥 優化
    if mtf is None and instId:
        mtf = fetch_mtf_trend(instId)
    if mtf:
        mtf_score, mtf_desc = calc_mtf_alignment(mtf, side)
        # 🔥 MTF 反向直接淘汰
        if mtf_score == -99:
            score = -99
            detail["mtf"] = -99
            detail["mtf_desc"] = mtf_desc
        else:
            score += mtf_score
            detail["mtf"] = mtf_score
            detail["mtf_desc"] = mtf_desc

    # 📊 成交量 (-99 ~ +8) 🔥 優化
    vol_ratio, vol_score = calc_volume_quality(df)
    # 🔥 量能不足直接淘汰
    if vol_score == -99:
        score = -99
        detail["volume"] = -99
        detail["volume_ratio"] = vol_ratio
    else:
        score += vol_score
        detail["volume"] = vol_score
        detail["volume_ratio"] = vol_ratio

    # 🔥 被淘汰的訊號直接回傳
    if score < 0:
        grade = "❌ 淘汰"
        return score, grade, detail

    grade = (
        "A+ 極強 🔥"
        if score >= 90
        else "A 強力 ⭐"
        if score >= 82
        else "B+ 合格 ✅"
        if score >= 78
        else "觀望 ⚪"
    )
    return score, grade, detail


# ═════════════════════════════════════════════════════════
# 8. 訊號生成（🔥 v14.1 結構確認優化）
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
        return None

    # 極端資金費率時降分過濾
    funding_penalty_long = funding_rate and funding_rate > 0.0008
    funding_penalty_short = funding_rate and funding_rate < -0.0008

    coin = instId.split("-")[0]

    # 🌐 市場狀態識別
    regime_info = detect_market_regime(df)
    if regime_info["regime"] == "range":
        threshold += 5
    if regime_info["volatile"]:
        threshold += 3

    # 🕒 多時框抓一次給兩個方向共用
    mtf = fetch_mtf_trend(instId)

    candidates = []
    for side in ("LONG", "SHORT"):
        score, grade, detail = calc_score(df, side, current_price, mtf=mtf)
        
        # 🔥 被淘汰的訊號直接跳過
        if score < 0:
            continue
            
        if side == "LONG" and funding_penalty_long:
            score -= 5
        if side == "SHORT" and funding_penalty_short:
            score -= 5

        detail["regime"] = regime_info["regime"]
        detail["adx"] = regime_info["adx"]
        detail["atr_pct"] = regime_info["atr_pct"]

        # 🌀 進場時機：有回測 K 線 +3 分
        if detect_pullback(df, side):
            score += 3
            detail["pullback"] = True

        # 🔥【優化 3】結構確認：至少 2 個核心條件才允許進場
        core_conditions = [
            detail.get("ob", 0) > 0,
            detail.get("fvg", 0) > 0,
            detail.get("liq", 0) > 0,
            detail.get("pa", 0) > 0,
        ]
        if sum(core_conditions) < 2:
            logging.info(f"[{instId}] {side} 結構確認不足 ({sum(core_conditions)}/2)，跳過")
            continue

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

        # 🎯 動態 TP 校正
        tp_levels, tp_notes = adjust_tp_by_sr(entry, side, tp_levels, df)
        if tp_notes:
            detail["tp_adjust_notes"] = tp_notes

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
        "features": features,
        "mtf": mtf,
        "regime": regime,
    }
    history = _load_json(TRADE_HISTORY_FILE, [])
    history.append(trade)
    _save_json(TRADE_HISTORY_FILE, history)
    logging.info(f"📝 記錄交易：{coin} {order_id} {close_type}")

    try:
        update_learning(trade, sig_snapshot)
        # 🔥 更新勝率監控
        update_winrate_monitor(trade)
    except Exception as e:
        logging.warning(f"⚠️ 更新學習狀態失敗：{e}")


# ═════════════════════════════════════════════════════════
# 9.6 學習機制
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
    """🧠 套用學習狀態 → (調整後分數，套用紀錄)"""
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
        f"最大獲利：`{s['max_win']:+.2f}%` 最大虧損：`{s['max_loss']:+.2f}%`",
        "",
    ]

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
        f"最大獲利：`{s['max_win']:+.2f}%` 最大虧損：`{s['max_loss']:+.2f}%`",
        "",
    ]

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

    lines.append(f"🔥 最大連勝：{max_win_streak}  ❄️ 最大連敗：{max_loss_streak}")
    lines.append("")

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

    if loss_reasons:
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
    """🧬 KNN 學習：找最相似的 10 筆歷史交易，看勝率 → (調整後分數，紀錄)"""
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
    for r in reasons[:1]:
        state["loss_reasons"].append({
            "ts": time.time(),
            "coin": coin,
            "side": side,
            "code": r.get("code"),
            "title": r.get("title"),
        })
    state["loss_reasons"] = state["loss_reasons"][-100:]
    _save_json(LEARNING_FILE, state)


# ═════════════════════════════════════════════════════════
# 🔥【新增】勝率監控機制
# ═════════════════════════════════════════════════════════
def update_winrate_monitor(trade: dict) -> None:
    """📊 更新勝率監控狀態"""
    state = _load_json(WINRATE_MONITOR_FILE, {"trades": [], "paused_until": None})
    
    cfg = load_config()
    wm_cfg = cfg.get("winrate_monitor", {})
    if not wm_cfg.get("enabled", True):
        return
    
    sample_size = wm_cfg.get("sample_size", 5)
    min_winrate = wm_cfg.get("min_winrate", 0.50)
    pause_hours = wm_cfg.get("pause_hours", 2)
    
    # 加入新交易
    state["trades"].append({
        "time": trade["time"],
        "coin": trade["coin"],
        "side": trade["side"],
        "is_win": trade["is_win"],
        "is_loss": trade["close_type"] == "SL"
    })
    
    # 保留最近 N 筆
    state["trades"] = state["trades"][-sample_size * 2:]
    
    # 計算最近 sample_size 筆勝率
    recent = state["trades"][-sample_size:]
    if len(recent) >= sample_size:
        wins = sum(1 for t in recent if t["is_win"])
        wr = wins / len(recent)
        
        # 勝率過低觸發暫停
        if wr < min_winrate and not state.get("paused_until"):
            state["paused_until"] = time.time() + pause_hours * 3600
            _save_json(WINRATE_MONITOR_FILE, state)
            send_tg(
                f"⚠️ *勝率監控觸發*\n"
                f"最近 {sample_size} 筆勝率 `{wr:.0%}` < 門檻 `{min_winrate:.0%}`\n"
                f"🔄 系統暫停新訊號 {pause_hours} 小時\n"
                f"💡 建議檢視 /learning 報告調整策略"
            )
            logging.warning(f"📊 勝率監控：{wr:.0%} < {min_winrate:.0%}，暫停 {pause_hours}h")
    
    _save_json(WINRATE_MONITOR_FILE, state)


def check_winrate_monitor(cfg: dict) -> tuple[bool, str]:
    """📊 檢查勝率監控狀態 → (是否暫停，原因)"""
    wm_cfg = cfg.get("winrate_monitor", {})
    if not wm_cfg.get("enabled", True):
        return False, ""
    
    state = _load_json(WINRATE_MONITOR_FILE, {})
    paused_until = state.get("paused_until")
    
    if paused_until and time.time() < paused_until:
        remaining = (paused_until - time.time()) / 3600
        return True, f"勝率監控暫停中（剩餘 {remaining:.1f} 小時）"
    elif paused_until and time.time() >= paused_until:
        # 暫停結束，清除狀態
        state["paused_until"] = None
        _save_json(WINRATE_MONITOR_FILE, state)
        send_tg("✅ *勝率監控已恢復*\n系統恢復正常掃描 🚀")
    
    return False, ""


# ═════════════════════════════════════════════════════════
# 9.65 覆盤分析
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
        f"方向：{direction} 結算：{label}",
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


# ═════════════════════════════════════════════════════════
# 9.8 連續虧損熔斷
# ═════════════════════════════════════════════════════════
def check_circuit_breaker(cfg: dict) -> tuple[bool, str, int]:
    """🛑 檢查連續虧損熔斷 → (是否暫停，訊息，連敗次數)"""
    cb = cfg.get("circuit_breaker", {})
    if not cb.get("enabled", True):
        return False, "", 0

    history = _load_json(TRADE_HISTORY_FILE, [])
    recent = [
        t for t in history
        if t.get("close_type") in ("SL", "BE", "LOCK", "TP1", "TP2", "TP3")
    ][-20:]
    if not recent:
        return False, "", 0

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

    auto = cfg.get("auto_news_blackout", {})
    if auto.get("nfp", True):
        if now.weekday() == 4 and now.day <= 7:
            cur = now.hour * 60 + now.minute
            if 21 * 60 + 25 <= cur < 22 * 60 + 30:
                return True, "NFP 非農（自動偵測）"

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


# 🔥【優化 4】活躍時段檢查
def is_active_hours(cfg: dict) -> bool:
    """🕐 檢查當前是否在允許交易的活躍時段（台灣時間）"""
    active_hours = cfg.get("active_hours_tw", [])
    if not active_hours:
        return True  # 沒設定則允許所有時段
    
    now = tw_now()
    cur_min = now.hour * 60 + now.minute
    
    for w in active_hours:
        try:
            sh, sm = map(int, w["start"].split(":"))
            eh, em = map(int, w["end"].split(":"))
            if _in_window(cur_min, sh * 60 + sm, eh * 60 + em):
                return True
        except Exception:
            continue
    return False


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
        """🔍 SL/BE/LOCK 後送覆盤分析訊息（並寫入 loss_reasons）"""
        try:
            cfg = load_config()
            pm_cfg = cfg.get("post_mortem", {})
            if not pm_cfg.get("enabled", True):
                return
            if mode != "LOSS" and pm_cfg.get("loss_only", False):
                return

            activated_at = sig.get("activated_at") or sig.get("created") or 0
            all_candles = fetch_candles_full(sig["instId"], limit=100)
            df_at_loss = [
                {"ts": c["ts"], "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c["v"]}
                for c in all_candles
                if (c["ts"] / 1000) >= (activated_at - 900)
            ]
            if len(df_at_loss) < 10:
                return

            reasons = analyze_loss(sig, df_at_loss)
            lessons = _generate_lessons(reasons)

            coin = sig["instId"].split("-")[0]
            similar = get_similar_stats(
                sig.get("score", 0),
                sig["side"],
                sig.get("detail", {}),
                sig.get("funding_rate"),
                coin,
            )

            msg = _fmt_postmortem(sig, mode, reasons, lessons, similar)
            send_tg(
                msg,
                reply_to_message_id=sig.get("entry_message_id"),
            )

            if mode == "LOSS":
                record_loss_reason(coin, sig["side"], reasons)
        except Exception as e:
            logging.error(f"❌ 覆盤分析失敗：{e}")

    def has_open_position(self, instId: str) -> bool:
        """🔒 該幣種是否還有未結束的訊號"""
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
        """檢查單一訊號 → True 代表結束（要從追蹤移除）"""
        try:
            price = fetch_price(sig["instId"])
            if price <= 0:
                return False

            sig["current_price"] = price
            status = sig["status"]

            if status == "PENDING":
                return self._check_pending(sig, price)

            if status not in ("ACTIVE", "BE", "TRAIL"):
                return False

            all_candles = fetch_candles_full(sig["instId"])
            last_ts_s = (
                sig.get("last_checked_ts")
                or sig.get("activated_at")
                or sig.get("created")
                or 0
            )
            last_ts_ms = int(last_ts_s * 1000)
            new_candles = [c for c in all_candles if c["ts"] > last_ts_ms]

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
                ),
                reply_markup=kb,
            )
            if msg_id:
                sig["entry_message_id"] = msg_id
            self._save()
            self.transitions += 1
        return False

    def _process_candle(self, sig: dict, candle: dict) -> bool:
        """對單一 K 線檢查 TP1 → TP2 → TP3 → SL → True 代表訊號結束"""
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
            wick_favor = lambda t: cc < t and ch >= t
            wick_against = lambda t: cc > t and cl <= t
        else:
            favor_hit = lambda t: cl <= t
            against_hit = lambda t: ch >= t
            wick_favor = lambda t: cc > t and cl <= t
            wick_against = lambda t: cc < t and ch >= t

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
            self._send_postmortem(sig, mode)
            self.transitions += 1
            return True

        return False

    def send_position_updates(self) -> None:
        """📊 發送所有持倉的進度更新（每輪一次）"""
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
            logging.info(f"📊 已發送 {cnt} 筆持倉更新")

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
# 11. 主掃描（🔥 v14.1 整合所有優化）
# ═════════════════════════════════════════════════════════
def run_monitor(tracker: SignalTracker, in_run_polls: int = 1, poll_interval: int = 30) -> None:
    """🔔 高頻監控模式 — 只檢查既有訊號的 PENDING 進場 / TP / SL，不生成新訊號"""
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
    """🔍 執行掃描（整合 v14.1 全部風控優化）"""
    logging.info("🚀 開始掃描...")

    # ── 0. 熱載入配置 ──
    cfg = load_config()
    coins = cfg.get("coins", ALL_COINS)
    focus_coins = cfg.get("focus_coins", [])  # 🔥 聚焦幣種
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

    # ── 2.6 🔥 勝率監控檢查 ──
    wr_paused, wr_reason = check_winrate_monitor(cfg)
    if wr_paused:
        logging.info(f"📊 {wr_reason}，不開新單但繼續監控")
        tracker.check_all()
        tracker.send_position_updates()
        return 0

    # ── 2.7 🔥 活躍時段檢查（僅影響新訊號生成） ──
    if not is_active_hours(cfg):
        logging.info("🕐 非活躍時段，跳過新訊號生成（仍監控既有持倉）")
        tracker.check_all()
        tracker.send_position_updates()
        return 0

    # ── 3. 掃描每個幣種 ──
    sent = 0
    for instId in coins:
        if sent >= max_signals:
            break

        # 🔥 聚焦幣種模式：若設定聚焦幣種，只交易這些
        if focus_coins and instId not in focus_coins:
            continue

        # 3.1 🔒 同幣種未平倉不重複開倉
        if tracker.has_open_position(instId):
            logging.info(f"[{instId}] 已有未平倉訊號，跳過")
            continue

        # 3.2 冷卻
        if is_cooling(instId, cooldown_h):
            logging.info(f"[{instId}] 冷卻中，跳過")
            continue

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
    return sent


# ═════════════════════════════════════════════════════════
# 12. 主入口
# ═════════════════════════════════════════════════════════
def main() -> None:
    try:
        logging.info("=" * 50)
        logging.info("🤖 Alpha Oracle Pro v14.1 高勝率版 啟動")
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
            if cmd in ("monitor", "/monitor", "/監控"):
                polls = int(sys.argv[2]) if len(sys.argv) > 2 else 1
                interval = int(sys.argv[3]) if len(sys.argv) > 3 else 30
                run_monitor(tracker, in_run_polls=polls, poll_interval=interval)
                return

        run_scan(tracker)
        logging.info("🎉 程式執行完成")

    except Exception as e:
        logging.error(f"🔥 系統錯誤：{e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
