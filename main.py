#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro v12.2 — 歷史 K 線補抓版（繁體中文）
══════════════════════════════════════════════════════════════════════
✨ v12.2 新增（不再漏插針）：
  📜 歷史 K 線補抓：每次 _check_one 抓 last_checked_ts 之後所有 K 線依序處理
     ↳ cron 漏跑、Actions 卡死、訊號活了 3 小時才掃，過去任何插針都不會漏
  🔒 同幣種未平倉嚴格擋（v12 即有，這版加強日誌）
  📦 fetch_candles_full：每輪掃描共用 30 秒快取，避免重複打 API

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

ALL_COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
MAX_SIGNALS = _get_env_int("MAX_SIGNALS", 3)
SCORE_THRESHOLD = _get_env_int("SETUP_SCORE_THRESHOLD", 68)

SIGNAL_EXPIRE_HOURS = 24
COOLDOWN_HOURS = 2

ACTIVE_SIGNALS_FILE = "active_signals.json"
TRADE_HISTORY_FILE = "trade_history.json"
COOLDOWN_FILE = "signal_cooldown.json"
CONFIG_FILE = "config.json"
SYSTEM_STATE_FILE = "system_state.json"

# 記憶體快取（同一輪執行內共用，跨輪不持久）
_price_cache: dict = {}

# ═════════════════════════════════════════════════════════
# 1.5 預設配置（config.json 不存在時的 fallback）
# ═════════════════════════════════════════════════════════
DEFAULT_CONFIG: dict = {
    "max_signals": 3,
    "score_threshold": 68,
    "cooldown_hours": 2,
    "signal_expire_hours": 24,
    "atr_max_pct": 0.04,           # ATR/Price 超過此值視為震盪過大
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
    grade = "🔥 A+ 極強" if score >= 85 else "⭐ A 強力" if score >= 70 else "✅ B+ 合格"

    tp1_pct = (tp1 - entry) / entry * 100 * (1 if side == "LONG" else -1)
    tp2_pct = (tp2 - entry) / entry * 100 * (1 if side == "LONG" else -1)
    tp3_pct = (tp3 - entry) / entry * 100 * (1 if side == "LONG" else -1)
    sl_pct = (sl - entry) / entry * 100  # 帶正負號

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
# 7. 評分系統（規格 100 分制）
# ═════════════════════════════════════════════════════════
def calc_score(df: list, side: str, current_price: float) -> tuple[int, str, dict]:
    """總分 = 趨勢30 + RSI25 + OB20 + FVG15 + SNR5 + PA5 + 流動性5 + 動能5 = 100"""
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

    candidates = []
    for side in ("LONG", "SHORT"):
        score, grade, detail = calc_score(df, side, current_price)
        if side == "LONG" and funding_penalty_long:
            score -= 5
        if side == "SHORT" and funding_penalty_short:
            score -= 5
        if score < threshold:
            continue

        entry = current_price
        sl_dist = atr * 1.5
        sl = entry - sl_dist if side == "LONG" else entry + sl_dist
        risk = abs(entry - sl)

        # ✅ 規格倍率：1.5R / 3.0R / 5.0R
        if side == "LONG":
            tp1 = entry + risk * 1.5
            tp2 = entry + risk * 3.0
            tp3 = entry + risk * 5.0
        else:
            tp1 = entry - risk * 1.5
            tp2 = entry - risk * 3.0
            tp3 = entry - risk * 5.0

        candidates.append(
            {
                "instId": instId,
                "side": side,
                "tf": "15m",
                "entry": round(entry, 4),
                "sl": round(sl, 4),
                "tp1": round(tp1, 4),
                "tp2": round(tp2, 4),
                "tp3": round(tp3, 4),
                "score": score,
                "grade": grade,
                "detail": detail,
                "funding_rate": funding_rate,
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
) -> None:
    """📝 記錄交易歷史"""
    is_win = close_type in ("TP1", "TP2", "TP3")
    is_be = close_type == "BE"
    pnl = (
        (close_price - entry) / entry * 100
        if side == "LONG"
        else (entry - close_price) / entry * 100
    )
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
    }
    history = _load_json(TRADE_HISTORY_FILE, [])
    history.append(trade)
    _save_json(TRADE_HISTORY_FILE, history)
    logging.info(f"📝 記錄交易：{coin} {order_id} {close_type}")


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
# 9.9 關鍵時段過濾
# ═════════════════════════════════════════════════════════
def _in_window(cur_min: int, start_min: int, end_min: int) -> bool:
    """支援跨午夜時段（如 23:50–00:10）"""
    if start_min <= end_min:
        return start_min <= cur_min < end_min
    return cur_min >= start_min or cur_min < end_min


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
            record_trade(coin, side, order_id, entry, tp1, "TP1", sig["score"])
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
            record_trade(coin, side, order_id, entry, tp2, "TP2", sig["score"])
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
            record_trade(coin, side, order_id, entry, tp3, "TP3", sig["score"])
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
            record_trade(coin, side, order_id, entry, sl, close_type, sig["score"])
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
# 11. 主掃描
# ═════════════════════════════════════════════════════════
def run_scan(tracker: SignalTracker) -> int:
    """🔍 執行掃描（整合 v12 全部風控）"""
    logging.info("🚀 開始掃描...")

    # ── 0. 熱載入配置 ──
    cfg = load_config()
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
        # 仍要檢查既有訊號的 SL/TP
        tracker.check_all()
        tracker.send_position_updates()
        return 0

    # ── 3. 掃描每個幣種 ──
    sent = 0
    for instId in ALL_COINS:
        if sent >= max_signals:
            break

        # 3.1 🔒 同幣種未平倉不重複開倉（先擋這個，避免冷卻過期後又開新單）
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
        logging.info("🤖 Alpha Oracle Pro v11.0 啟動")
        logging.info(f"⏰ 台灣時間：{tw_ts()}")
        logging.info("=" * 50)

        tracker = SignalTracker(ACTIVE_SIGNALS_FILE)

        # /stats 或 /持倉 命令
        if len(sys.argv) > 1 and sys.argv[1] in ("/stats", "/持倉", "stats"):
            send_tg(tracker.get_position_stats())
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
