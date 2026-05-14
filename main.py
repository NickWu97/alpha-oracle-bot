#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro v14.1 — 更精準版（繁體中文）
══════════════════════════════════════════════════════════════════════
✨ v14.1 對比 v14.0：6 大精準度改進
   🎯 1. Swing-point SL：用最近 20 根結構低/高點 + 0.3 ATR 緩衝，不是死板 ATR×1.5
   🎯 2. 5m 進場確認：碰到進場區還不夠，要 5m 出現 PA 或量能爆才進場
   🎯 3. Tick check 即時 SL：每次掃描第一件事就是用當前價檢查所有持倉
   🎯 4. 聚類 S/R 動態 TP：找多次測試過的價位才當 TP，不是只看 100 根極值
   🎯 5. record_trade 只記一次：v14.0 同一筆單會記 2~4 次（TP1+TP2+TP3+SL），修正
   🎯 6. 點差/流動性濾波器：高 ATR 或低成交量時段直接跳過
✨ 同時修掉 v14.0 的 markdown 連結化語法 bug（[func.name](http://func.name) 還原成 func.name）
✨ v14.0 既有功能全保留：
   MTF / 量能 / 市場狀態 / 動態 TP / 新聞過濾 / Pullback / KNN / 日月報 / 覆盤分析
══════════════════════════════════════════════════════════════════════
"""
import os
import sys
import json
import time
import uuid
import logging
import requests
from datetime import datetime, timezone, timedelta

# ═════════════════════════════════════════════════════════
# 🇹🇼 台灣時間
# ═════════════════════════════════════════════════════════
TW_TZ = timezone(timedelta(hours=8))


def tw_now() -> datetime:
    return datetime.now(TW_TZ)


def tw_ts() -> str:
    return tw_now().strftime("%Y-%m-%d %H:%M:%S 台灣時間")


def _get_env(key: str, default: str = "") -> str:
    val = os.getenv(key)
    return val.strip() if val and val.strip() else default


def _get_env_int(key: str, default: int) -> int:
    val = os.getenv(key)
    try:
        return int(val.strip()) if val and val.strip() else default
    except Exception:
        return default


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

_price_cache: dict = {}
_candle_full_cache: dict = {}
_mtf_cache: dict = {}
_tv_cache: dict = {}
_funding_cache: dict = {}

# ═════════════════════════════════════════════════════════
# 預設配置
# ═════════════════════════════════════════════════════════
DEFAULT_CONFIG: dict = {
    "coins": ALL_COINS,
    "max_signals": 3,
    "score_threshold": 68,
    "cooldown_hours": 2,
    "signal_expire_hours": 24,
    "atr_max_pct": 0.04,
    "max_hold_hours": 48,

    # 🎯 v14.1 #1：Swing-point SL
    "sl_method": "hybrid",          # "atr" | "swing" | "hybrid"
    "sl_atr_mult": 1.5,
    "sl_swing_lookback": 20,        # 看最近 20 根 K 找結構點
    "sl_swing_buffer_atr": 0.3,     # 結構點外再加 0.3 ATR 緩衝

    # 🎯 v14.1 #2：5m 進場確認
    "entry_confirmation": {
        "enabled": True,
        "tf": "5m",
        "require_pa": True,          # 需要 PA 或...
        "require_volume_spike": True, # ...量能爆（任一即可）
        "volume_spike_ratio": 1.3,   # 1.3x 均量
        "min_close_position": 0.6,   # 最後 K 收盤在實體 60% 以上（多單）/ 以下（空單）
    },

    # 🎯 v14.1 #3：Tick check
    "tick_check": {
        "enabled": True,
        "price_cache_ttl_active": 2,
        "price_cache_ttl_idle": 5,
    },
    "intensive_monitor": {
        "enabled": True,
        "total_seconds": 55,
        "interval_seconds": 3,
    },

    # 🎯 v14.1 #4：聚類 S/R 動態 TP
    "dynamic_tp": {
        "enabled": True,
        "cluster_min_touches": 2,    # 至少被測試 2 次才算 S/R 層
        "cluster_tolerance_pct": 0.3, # 0.3% 內視為同一層
        "tp_safety_buffer_pct": 0.2,  # TP 拉到 S/R 前 0.2%
    },

    # 🎯 v14.1 #6：點差/流動性濾波
    "liquidity_filter": {
        "enabled": True,
        "min_volume_usd_24h": 10_000_000,  # 24h 成交額 $10M 以下跳過
        "max_atr_pct_short_tf": 1.5,       # 5m ATR > 1.5% 跳過（過度震盪）
    },

    "conservative_sl_first": True,  # K 線內 SL 跟 TP 同時觸到時 SL 優先

    "post_mortem": {"enabled": True, "loss_only": False},
    "learning": {
        "enabled": True, "knn_enabled": True,
        "min_samples": 5, "max_score_adjust": 10,
    },

    "news_blackouts": [],
    "auto_news_blackout": {"nfp": True, "cpi": True},

    "price_verification": {
        "enabled": True, "max_deviation_pct": 0.5,
        "block_on_unverified": False,
    },

    "circuit_breaker": {
        "enabled": True,
        "soft_threshold": 3, "soft_pause_hours": 4,
        "hard_threshold": 5, "hard_pause_hours": 24,
    },

    "blackout_windows_tw": [
        {"start": "07:50", "end": "08:10", "reason": "資金費率結算（00 UTC）"},
        {"start": "15:50", "end": "16:10", "reason": "資金費率結算（08 UTC）"},
        {"start": "23:50", "end": "00:10", "reason": "資金費率結算（16 UTC）"},
        {"start": "21:25", "end": "21:45", "reason": "美股開盤波動"},
        {"start": "02:00", "end": "02:30", "reason": "FOMC 公布時段"},
    ],
}

_session = requests.Session()
_session.headers.update({"User-Agent": "alpha-oracle-pro/14.1"})


# ═════════════════════════════════════════════════════════
# 通知系統
# ═════════════════════════════════════════════════════════
def send_tg(msg: str, parse_mode: str = "Markdown",
            reply_markup: dict | None = None,
            reply_to_message_id: int | None = None) -> int | None:
    if not TG_TOKEN or not CHAT_ID:
        logging.warning("⚠️ TG_TOKEN 或 CHAT_ID 未設定")
        return None
    payload = {
        "chat_id": CHAT_ID, "text": msg, "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True
    for attempt in range(3):
        try:
            r = _session.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json=payload, timeout=8,
            )
            if r.status_code == 200:
                return r.json().get("result", {}).get("message_id")
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("parameters", {}).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                time.sleep(min(wait + 0.5, 15))
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            logging.error(f"❌ TG {r.status_code}: {r.text[:200]}")
            return None
        except Exception as e:
            logging.warning(f"⏳ TG 失敗 {2 ** attempt}s 後重試：{e}")
            time.sleep(2 ** attempt)
    return None


def _order_keyboard(order_id: str) -> dict:
    return {"inline_keyboard": [[{
        "text": f"🔍 查詢訂單 {order_id[-8:]}",
        "callback_data": f"order_{order_id}",
    }]]}


# ═════════════════════════════════════════════════════════
# 數據抓取
# ═════════════════════════════════════════════════════════
def _okx_get(url: str, timeout: float = 6) -> dict | None:
    for attempt in range(3):
        try:
            r = _session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(min(2 ** attempt, 8))
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            return None
        except requests.RequestException:
            time.sleep(2 ** attempt)
    return None


def fetch_price(instId: str, force_fresh: bool = False,
                cache_ttl: float = 5) -> float:
    """⚡ v14.1 加 force_fresh 強制重抓"""
    now = time.time()
    if not force_fresh and instId in _price_cache:
        price, t = _price_cache[instId]
        if now - t < cache_ttl:
            return price
    data = _okx_get(f"https://www.okx.com/api/v5/market/ticker?instId={instId}")
    if data and data.get("code") == "0" and data.get("data"):
        try:
            price = float(data["data"][0]["last"])
            if price > 0:
                _price_cache[instId] = (price, now)
                return price
        except Exception:
            pass
    return _price_cache.get(instId, (0.0, 0))[0]


def fetch_candles(instId: str, tf: str = "15m", limit: int = 100) -> list | None:
    """已收線 K 線"""
    data = _okx_get(
        f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}"
    )
    if not data or data.get("code") != "0":
        return None
    rows = data.get("data", [])
    if len(rows) < 30:
        return None
    confirmed = [r for r in rows if r[8] == "1"][::-1]
    return [{
        "ts": int(r[0]), "o": float(r[1]), "h": float(r[2]),
        "l": float(r[3]), "c": float(r[4]), "v": float(r[5]),
    } for r in confirmed]


def fetch_candles_full(instId: str, tf: str = "15m", limit: int = 100) -> list:
    """含未收線 + 30 秒快取"""
    cache_key = f"{instId}_{tf}_{limit}"
    now = time.time()
    if cache_key in _candle_full_cache:
        candles, t = _candle_full_cache[cache_key]
        if now - t < 30:
            return candles
    data = _okx_get(
        f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}",
        timeout=8,
    )
    if not data or data.get("code") != "0":
        return _candle_full_cache.get(cache_key, ([], 0))[0]
    candles = []
    for r in data.get("data", []):
        try:
            candles.append({
                "ts": int(r[0]), "o": float(r[1]), "h": float(r[2]),
                "l": float(r[3]), "c": float(r[4]), "v": float(r[5]),
                "confirmed": r[8] == "1",
            })
        except Exception:
            continue
    candles.sort(key=lambda x: x["ts"])
    _candle_full_cache[cache_key] = (candles, now)
    return candles


def fetch_funding_rate(instId: str) -> float | None:
    now = time.time()
    if instId in _funding_cache:
        v, t = _funding_cache[instId]
        if now - t < 60:
            return v
    data = _okx_get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}")
    if data and data.get("code") == "0" and data.get("data"):
        try:
            v = float(data["data"][0]["fundingRate"])
            _funding_cache[instId] = (v, now)
            return v
        except Exception:
            pass
    return None


def fetch_24h_volume_usd(instId: str) -> float:
    """💰 24h 成交額（USD）→ 流動性過濾用"""
    data = _okx_get(f"https://www.okx.com/api/v5/market/ticker?instId={instId}")
    if data and data.get("code") == "0" and data.get("data"):
        try:
            d = data["data"][0]
            return float(d.get("volCcy24h", 0)) * float(d.get("last", 0))
        except Exception:
            pass
    return 0


def fetch_price_tv(instId: str) -> float | None:
    now = time.time()
    if instId in _tv_cache:
        price, t = _tv_cache[instId]
        if now - t < 10:
            return price
    try:
        from tradingview_ta import TA_Handler, Interval  # type: ignore
    except ImportError:
        return None
    try:
        symbol = instId.replace("-USDT-SWAP", "USDT.P").replace("-", "")
        h = TA_Handler(symbol=symbol, exchange="OKX", screener="crypto",
                       interval=Interval.INTERVAL_1_MINUTE, timeout=8)
        a = h.get_analysis()
        price = float(a.indicators.get("close", 0) or 0)
        if price > 0:
            _tv_cache[instId] = (price, now)
            return price
    except Exception:
        pass
    return None


def verify_price(instId: str, okx_price: float, max_dev_pct: float = 0.5,
                 block_on_unverified: bool = False) -> tuple[bool, float | None, float]:
    tv_price = fetch_price_tv(instId)
    if tv_price is None:
        return (not block_on_unverified, None, 0.0)
    diff = abs(okx_price - tv_price) / okx_price * 100
    if diff > max_dev_pct:
        return False, tv_price, diff
    return True, tv_price, diff


# ═════════════════════════════════════════════════════════
# 技術指標（v14 全套）
# ═════════════════════════════════════════════════════════
def calc_atr(df: list, period: int = 14) -> float:
    if len(df) < period + 1:
        return 0.001
    trs = []
    for i in range(1, len(df)):
        hl = df[i]["h"] - df[i]["l"]
        hc = abs(df[i]["h"] - df[i - 1]["c"])
        lc = abs(df[i]["l"] - df[i - 1]["c"])
        trs.append(max(hl, hc, lc))
    atr = sum(trs[-period:]) / period if len(trs) >= period else 0.001
    return atr if atr > 0 else 0.001


def calc_supertrend(df: list, period: int = 10) -> int:
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
    if len(df) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(df)):
        ch = df[i]["c"] - df[i - 1]["c"]
        gains.append(ch if ch > 0 else 0)
        losses.append(-ch if ch < 0 else 0)
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))


def calc_adx(df: list, period: int = 14) -> float:
    if len(df) < period * 2 + 1:
        return 0.0
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(df)):
        up = df[i]["h"] - df[i - 1]["h"]
        dn = df[i - 1]["l"] - df[i]["l"]
        plus_dm.append(up if (up > dn and up > 0) else 0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0)
        tr = max(df[i]["h"] - df[i]["l"],
                 abs(df[i]["h"] - df[i - 1]["c"]),
                 abs(df[i]["l"] - df[i - 1]["c"]))
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
    return {"regime": regime, "adx": round(adx, 1),
            "atr_pct": round(atr_pct, 3), "volatile": atr_pct > 2.5}


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
    seg = df[-lookback:] if len(df) >= lookback else df
    return min(r["l"] for r in seg), max(r["h"] for r in seg)


def detect_price_action(df: list, side: str) -> bool:
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
        if (prev["c"] < prev["o"] and last["c"] > last["o"]
                and last["c"] > prev["o"] and last["o"] < prev["c"]):
            return True
    else:
        if (prev["c"] > prev["o"] and last["c"] < last["o"]
                and last["c"] < prev["o"] and last["o"] > prev["c"]):
            return True
    return False


def detect_liquidity_sweep(df: list, side: str, lookback: int = 20) -> bool:
    if len(df) < lookback + 1:
        return False
    seg = df[-(lookback + 1):-1]
    last = df[-1]
    pl = min(r["l"] for r in seg)
    ph = max(r["h"] for r in seg)
    mid = (pl + ph) / 2
    if side == "LONG":
        return last["l"] < pl and last["c"] > mid
    return last["h"] > ph and last["c"] < mid


def calc_momentum_ratio(df: list, side: str, n: int = 5) -> bool:
    seg = df[-n:]
    bull = sum(1 for r in seg if r["c"] > r["o"])
    ratio = bull / max(1, len(seg))
    return ratio >= 0.6 if side == "LONG" else ratio <= 0.4


def detect_pullback(df: list, side: str) -> bool:
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


def fetch_mtf_trend(instId: str) -> dict:
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
            out[tf] = {"supertrend": st,
                       "trend": "up" if st == 1 else "down" if st == -1 else "side",
                       "rsi": round(calc_rsi(df), 1)}
        else:
            out[tf] = {"supertrend": 0, "trend": "side", "rsi": 50}
    _mtf_cache[instId] = (out, now)
    return out


def calc_mtf_alignment(mtf: dict, side: str) -> tuple[int, str]:
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
    desc = (f"1H={'順' if h1 == expect else '反' if h1 == -expect else '中'} / "
            f"4H={'順' if h4 == expect else '反' if h4 == -expect else '中'}")
    return score, desc


def calc_volume_quality(df: list, lookback: int = 20) -> tuple[float, int]:
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
        s = -10
    return round(ratio, 2), s


# ═════════════════════════════════════════════════════════
# 🎯 v14.1 #1：Swing-point Structural SL
# ═════════════════════════════════════════════════════════
def calc_swing_sl(df: list, side: str, entry: float, atr: float,
                  cfg: dict) -> tuple[float, str]:
    """🎯 結構性 SL：用最近 N 根 K 的 swing 低/高點 + ATR 緩衝

    比 v14.0 的「entry ± atr × 1.5」精準的地方：
    - 抓真正的支撐/阻力結構，而不是任意距離
    - 避免插針掃損（緩衝放在結構外）
    - 避免 SL 太寬導致 R:R 不理想

    回傳 (sl, method_used)
    """
    method = cfg.get("sl_method", "hybrid")
    sl_mult = cfg.get("sl_atr_mult", 1.5)
    lookback = cfg.get("sl_swing_lookback", 20)
    buffer = cfg.get("sl_swing_buffer_atr", 0.3)

    # 純 ATR
    atr_sl = entry - atr * sl_mult if side == "LONG" else entry + atr * sl_mult

    if method == "atr":
        return atr_sl, "atr"

    # 結構 swing 點
    seg = df[-lookback:] if len(df) >= lookback else df
    if not seg:
        return atr_sl, "atr_fallback"

    if side == "LONG":
        swing_low = min(c["l"] for c in seg)
        structural_sl = swing_low - atr * buffer
    else:
        swing_high = max(c["h"] for c in seg)
        structural_sl = swing_high + atr * buffer

    if method == "swing":
        return structural_sl, "swing"

    # hybrid：取兩者較緊但合理的
    if side == "LONG":
        # 結構 SL 必須在進場價下方，並且不能比 ATR×1.5 還寬太多
        if structural_sl < entry:
            # 取較緊（離 entry 較近）的；但若結構太遠（>2.5×ATR）就用 ATR
            if (entry - structural_sl) / atr > 2.5:
                return atr_sl, "atr_too_far"
            # 但結構不能太緊（< 0.8 ATR）否則容易被掃
            if (entry - structural_sl) / atr < 0.8:
                return entry - atr * 0.8, "swing_too_tight_clamp"
            return structural_sl, "swing"
    else:
        if structural_sl > entry:
            if (structural_sl - entry) / atr > 2.5:
                return atr_sl, "atr_too_far"
            if (structural_sl - entry) / atr < 0.8:
                return entry + atr * 0.8, "swing_too_tight_clamp"
            return structural_sl, "swing"
    return atr_sl, "atr_fallback"


# ═════════════════════════════════════════════════════════
# 🎯 v14.1 #4：聚類 S/R 動態 TP
# ═════════════════════════════════════════════════════════
def find_sr_clusters(df: list, lookback: int = 100,
                     tolerance_pct: float = 0.3,
                     min_touches: int = 2) -> tuple[list, list]:
    """🎯 聚類分析：找多次被測試過的 S/R 層

    比 v14.0 的「找最高 / 最低」精準：
    - 被多次測試過的價位才是真 S/R
    - 容差 0.3% 內視為同一層
    - 回傳 (supports, resistances) 由近到遠排序
    """
    if len(df) < lookback:
        seg = df
    else:
        seg = df[-lookback:]
    if len(seg) < 20:
        return [], []
    current = seg[-1]["c"]
    # 收集所有「轉折點」：swing high / swing low
    swing_highs = []
    swing_lows = []
    for i in range(2, len(seg) - 2):
        if (seg[i]["h"] > seg[i - 1]["h"] and seg[i]["h"] > seg[i - 2]["h"]
                and seg[i]["h"] > seg[i + 1]["h"] and seg[i]["h"] > seg[i + 2]["h"]):
            swing_highs.append(seg[i]["h"])
        if (seg[i]["l"] < seg[i - 1]["l"] and seg[i]["l"] < seg[i - 2]["l"]
                and seg[i]["l"] < seg[i + 1]["l"] and seg[i]["l"] < seg[i + 2]["l"]):
            swing_lows.append(seg[i]["l"])
    # 聚類
    def cluster(points: list, tol: float) -> list:
        if not points:
            return []
        points = sorted(points)
        clusters = []
        cur_cluster = [points[0]]
        for p in points[1:]:
            if abs(p - cur_cluster[-1]) / cur_cluster[-1] < tol:
                cur_cluster.append(p)
            else:
                if len(cur_cluster) >= min_touches:
                    clusters.append(sum(cur_cluster) / len(cur_cluster))
                cur_cluster = [p]
        if len(cur_cluster) >= min_touches:
            clusters.append(sum(cur_cluster) / len(cur_cluster))
        return clusters

    tol = tolerance_pct / 100
    resistance_clusters = cluster(swing_highs, tol)
    support_clusters = cluster(swing_lows, tol)
    # 過濾：阻力必須在當前價上方，支撐必須在下方
    resistances = sorted([r for r in resistance_clusters if r > current])
    supports = sorted([s for s in support_clusters if s < current], reverse=True)
    return supports, resistances


def calc_dynamic_tp(entry: float, side: str, atr: float, risk: float,
                    df: list, cfg: dict) -> tuple[list, list]:
    """🎯 用聚類 S/R 來精準設 TP（拉到 S/R 前安全緩衝）

    流程：
    1. 先算固定 R 倍 TP (1.5R/3R/5R)
    2. 找出聚類 S/R 層
    3. 若 TP 落在強 S/R 後方，拉回該 S/R 前 0.2%
    """
    fixed_tps = ([entry + risk * r for r in (1.5, 3.0, 5.0)]
                 if side == "LONG"
                 else [entry - risk * r for r in (1.5, 3.0, 5.0)])

    dt_cfg = cfg.get("dynamic_tp", {})
    if not dt_cfg.get("enabled", True):
        return fixed_tps, []

    supports, resistances = find_sr_clusters(
        df,
        lookback=100,
        tolerance_pct=dt_cfg.get("cluster_tolerance_pct", 0.3),
        min_touches=dt_cfg.get("cluster_min_touches", 2),
    )
    buffer = dt_cfg.get("tp_safety_buffer_pct", 0.2) / 100
    out = list(fixed_tps)
    notes = []
    if side == "LONG":
        # 對每個 TP，看是否有阻力擋在前面
        for i, tp in enumerate(out):
            for res in resistances:
                # 阻力在 TP 後方 0.5% 以內 → 把 TP 拉回阻力前
                if entry < res < tp * 1.005:
                    new_tp = res * (1 - buffer)
                    if new_tp > entry:
                        notes.append(f"TP{i + 1} {tp:.4f} → {new_tp:.4f}（避開聚類阻力 {res:.4f}）")
                        out[i] = new_tp
                        break
    else:
        for i, tp in enumerate(out):
            for sup in supports:
                if tp * 0.995 < sup < entry:
                    new_tp = sup * (1 + buffer)
                    if new_tp < entry:
                        notes.append(f"TP{i + 1} {tp:.4f} → {new_tp:.4f}（避開聚類支撐 {sup:.4f}）")
                        out[i] = new_tp
                        break
    # TP 順序保險（v14.0 沒有這個檢查 → DOGE TP collapse bug）
    if side == "LONG":
        for i in range(1, 3):
            if out[i] <= out[i - 1]:
                out[i] = out[i - 1] + atr * 0.5
                notes.append(f"TP{i + 1} 順序修正")
    else:
        for i in range(1, 3):
            if out[i] >= out[i - 1]:
                out[i] = out[i - 1] - atr * 0.5
                notes.append(f"TP{i + 1} 順序修正")
    return out, notes


# ═════════════════════════════════════════════════════════
# 🎯 v14.1 #2：5m 進場確認
# ═════════════════════════════════════════════════════════
def confirm_entry_on_5m(instId: str, side: str, cfg: dict) -> tuple[bool, str]:
    """🎯 抓 5m K 線判斷是否真的可以進場（不只是 15m 進場區）

    要求（任一即可）：
    - 5m 有 PA 形態（Pin Bar 或吞沒）方向一致
    - 5m 量能爆（>1.3x 均量）+ K 線方向正確

    回傳 (是否確認, 原因)
    """
    ec_cfg = cfg.get("entry_confirmation", {})
    if not ec_cfg.get("enabled", True):
        return True, "未啟用 5m 確認"
    tf = ec_cfg.get("tf", "5m")
    df = fetch_candles(instId, tf=tf, limit=30)
    if not df or len(df) < 5:
        return True, "5m 資料不足，放行"
    reasons = []
    # 條件 A：PA 確認
    if ec_cfg.get("require_pa", True) and detect_price_action(df, side):
        reasons.append("5m PA 形態確認")
    # 條件 B：量能爆 + 方向對
    if ec_cfg.get("require_volume_spike", True):
        ratio, _ = calc_volume_quality(df, lookback=10)
        vol_thr = ec_cfg.get("volume_spike_ratio", 1.3)
        if ratio >= vol_thr:
            last = df[-1]
            body = abs(last["c"] - last["o"])
            if body > 0:
                # 收盤要在實體 60% 以上（多單）或以下（空單）
                pos = (last["c"] - last["l"]) / (last["h"] - last["l"]) if last["h"] > last["l"] else 0.5
                min_pos = ec_cfg.get("min_close_position", 0.6)
                if side == "LONG" and pos >= min_pos and last["c"] > last["o"]:
                    reasons.append(f"5m 量爆 {ratio}x + 強勢陽 K（收盤位 {pos:.0%}）")
                elif side == "SHORT" and (1 - pos) >= min_pos and last["c"] < last["o"]:
                    reasons.append(f"5m 量爆 {ratio}x + 強勢陰 K（收盤位 {pos:.0%}）")
    if reasons:
        return True, " + ".join(reasons)
    return False, "5m 無 PA 或量能爆"


# ═════════════════════════════════════════════════════════
# 🎯 v14.1 #6：點差/流動性濾波
# ═════════════════════════════════════════════════════════
def passes_liquidity_filter(instId: str, cfg: dict) -> tuple[bool, str]:
    """🎯 過濾低流動性 / 過度震盪幣種

    擋兩種情境：
    - 24h 成交額太低（低於 $10M）→ 點差大、滑點高
    - 5m ATR > 1.5%（超短期震盪過大）→ SL 容易被掃
    """
    lf_cfg = cfg.get("liquidity_filter", {})
    if not lf_cfg.get("enabled", True):
        return True, "未啟用"
    min_vol = lf_cfg.get("min_volume_usd_24h", 10_000_000)
    vol_24h = fetch_24h_volume_usd(instId)
    if vol_24h < min_vol:
        return False, f"24h 成交額 ${vol_24h / 1e6:.1f}M < ${min_vol / 1e6:.0f}M"
    max_atr_pct = lf_cfg.get("max_atr_pct_short_tf", 1.5)
    df_5m = fetch_candles(instId, tf="5m", limit=30)
    if df_5m and len(df_5m) >= 15:
        atr_5m = calc_atr(df_5m)
        atr_pct = atr_5m / df_5m[-1]["c"] * 100
        if atr_pct > max_atr_pct:
            return False, f"5m ATR {atr_pct:.2f}% > {max_atr_pct}%（過度震盪）"
    return True, "OK"


# ═════════════════════════════════════════════════════════
# 評分系統（v14 全套保留）
# ═════════════════════════════════════════════════════════
def calc_score(df: list, side: str, current_price: float,
               mtf: dict | None = None, instId: str | None = None) -> tuple[int, str, dict]:
    detail = {}
    score = 0
    st = calc_supertrend(df)
    if (side == "LONG" and st == 1) or (side == "SHORT" and st == -1):
        score += 30; detail["trend"] = 30
    elif st == 0:
        score += 15; detail["trend"] = 15
    else:
        detail["trend"] = 0
    rsi = calc_rsi(df)
    detail["rsi_value"] = round(rsi, 1)
    if side == "LONG":
        if 30 <= rsi <= 50:
            score += 25; detail["rsi"] = 25
        elif 50 < rsi < 70:
            score += 15; detail["rsi"] = 15
        else:
            detail["rsi"] = 0
    else:
        if 50 <= rsi <= 70:
            score += 25; detail["rsi"] = 25
        elif 30 < rsi < 50:
            score += 15; detail["rsi"] = 15
        else:
            detail["rsi"] = 0
    ob = find_order_block(df, side)
    if ob and ob["low"] * 0.995 <= current_price <= ob["high"] * 1.005:
        score += 20; detail["ob"] = 20
    else:
        detail["ob"] = 0
    fvg = find_fvg(df, side)
    if fvg and fvg["low"] * 0.997 <= current_price <= fvg["high"] * 1.003:
        score += 15; detail["fvg"] = 15
    else:
        detail["fvg"] = 0
    sup, res = calc_snr(df)
    if side == "LONG" and current_price <= sup * 1.01:
        score += 5; detail["snr"] = 5
    elif side == "SHORT" and current_price >= res * 0.99:
        score += 5; detail["snr"] = 5
    else:
        detail["snr"] = 0
    detail["pa"] = 5 if detect_price_action(df, side) else 0
    score += detail["pa"]
    detail["liq"] = 5 if detect_liquidity_sweep(df, side) else 0
    score += detail["liq"]
    detail["mom"] = 5 if calc_momentum_ratio(df, side) else 0
    score += detail["mom"]
    if mtf is None and instId:
        mtf = fetch_mtf_trend(instId)
    if mtf:
        mtf_s, mtf_d = calc_mtf_alignment(mtf, side)
        score += mtf_s
        detail["mtf"] = mtf_s
        detail["mtf_desc"] = mtf_d
    vol_r, vol_s = calc_volume_quality(df)
    score += vol_s
    detail["volume"] = vol_s
    detail["volume_ratio"] = vol_r
    grade = ("A+ 極強 🔥" if score >= 85 else "A 強力 ⭐" if score >= 70
             else "B+ 合格 ✅" if score >= 68 else "觀望 ⚪")
    return score, grade, detail


# ═════════════════════════════════════════════════════════
# 學習機制（v14 KNN 保留）
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


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    user_cfg = _load_json(CONFIG_FILE, {})
    return _deep_merge(DEFAULT_CONFIG, user_cfg) if user_cfg else dict(DEFAULT_CONFIG)


def get_system_state() -> dict:
    return _load_json(SYSTEM_STATE_FILE, {})


def set_system_state(state: dict) -> None:
    _save_json(SYSTEM_STATE_FILE, state)


def is_cooling(instId: str, hours: float = COOLDOWN_HOURS) -> bool:
    cd = _load_json(COOLDOWN_FILE, {})
    last = cd.get(instId)
    return last is not None and (time.time() - float(last)) < hours * 3600


def mark_cooldown(instId: str, hours: float = COOLDOWN_HOURS) -> None:
    cd = _load_json(COOLDOWN_FILE, {})
    cd[instId] = time.time()
    cutoff = time.time() - hours * 3600 * 3
    cd = {k: v for k, v in cd.items() if float(v) > cutoff}
    _save_json(COOLDOWN_FILE, cd)


_FEATURE_SCALE = {
    "score": 30, "rsi": 50, "atr_pct": 3, "funding": 2,
    "vol_ratio": 3, "adx": 50, "mtf_h1": 1, "mtf_h4": 1,
}


def vectorize_signal(score, side, detail, funding_rate, mtf=None, regime=None) -> dict:
    rsi = (detail or {}).get("rsi_value", 50)
    return {
        "score": float(score), "rsi": float(rsi),
        "atr_pct": float((detail or {}).get("atr_pct", 1.0)),
        "funding": float(funding_rate or 0) * 1000,
        "vol_ratio": float((detail or {}).get("volume_ratio", 1.0)),
        "adx": float((regime or {}).get("adx", 20)),
        "mtf_h1": 1.0 if (mtf or {}).get("1H", {}).get("supertrend") == (1 if side == "LONG" else -1) else 0.0,
        "mtf_h4": 1.0 if (mtf or {}).get("4H", {}).get("supertrend") == (1 if side == "LONG" else -1) else 0.0,
        "side": 1.0 if side == "LONG" else 0.0,
    }


def find_similar_trades(features: dict, side: str, history: list, k: int = 10) -> list:
    candidates = []
    for t in history:
        f = t.get("features")
        if not f or t.get("side") != side:
            continue
        d2 = sum(((features.get(key, 0) - f.get(key, 0)) / max(scale, 1)) ** 2
                 for key, scale in _FEATURE_SCALE.items())
        candidates.append((d2, t))
    candidates.sort(key=lambda x: x[0])
    return [t for _, t in candidates[:k]]


def apply_knn_learning(score, side, detail, funding_rate, coin, mtf, regime) -> tuple[int, list]:
    cfg = load_config()
    if not cfg.get("learning", {}).get("knn_enabled", True):
        return score, []
    history = _load_json(TRADE_HISTORY_FILE, [])
    if len(history) < 10:
        return score, []
    feat = vectorize_signal(score, side, detail, funding_rate, mtf, regime)
    similar = find_similar_trades(feat, side, history, k=10)
    if len(similar) < 3:
        return score, []
    wins = sum(1 for t in similar if t.get("close_type") in ("TP3", "LOCK"))
    n = len(similar)
    wr = wins / n
    notes = [f"🧬 KNN：{n} 筆最相似 → 勝率 {wr:.0%}"]
    if wr < 0.30: return score - 8, notes + ["低勝率 -8"]
    if wr < 0.40: return score - 4, notes + ["偏低 -4"]
    if wr > 0.70: return score + 5, notes + ["高勝率 +5"]
    if wr > 0.60: return score + 3, notes + ["中高 +3"]
    return score, notes


def _bucket_score(s: int) -> str:
    if s >= 90: return "score:90+"
    if s >= 80: return "score:80-89"
    if s >= 70: return "score:70-79"
    return "score:60-69"


def _bucket_rsi(rsi: float, side: str) -> str:
    b = int(rsi // 10) * 10
    return f"rsi_{side.lower()}:{b}-{b + 9}"


def _bucket_funding(fr) -> str:
    if fr is None: return "fund:none"
    if fr > 0.0008: return "fund:very_pos"
    if fr > 0.0001: return "fund:pos"
    if fr > -0.0001: return "fund:neutral"
    if fr > -0.0008: return "fund:neg"
    return "fund:very_neg"


def _signal_buckets(score, side, detail, funding_rate, coin) -> list:
    rsi = (detail or {}).get("rsi_value", 50)
    return [_bucket_score(score), _bucket_rsi(rsi, side),
            _bucket_funding(funding_rate),
            f"coin:{coin}", f"coin_side:{coin}_{side}"]


def update_learning(trade: dict, sig_snapshot: dict | None = None) -> None:
    state = _load_json(LEARNING_FILE, {})
    state.setdefault("buckets", {})
    state.setdefault("by_coin", {})
    state.setdefault("loss_reasons", [])
    score = trade.get("raw_score", trade.get("score", 0))
    coin = trade.get("coin", "?")
    side = trade.get("side", "?")
    ct = trade.get("close_type", "?")
    fr = trade.get("funding_rate")
    detail = trade.get("detail") or (sig_snapshot or {}).get("detail", {})
    is_win = ct in ("TP3", "LOCK")
    is_be = ct == "BE"
    is_loss = ct == "SL"
    for b in _signal_buckets(score, side, detail, fr, coin):
        bd = state["buckets"].setdefault(b, {"win": 0, "loss": 0, "be": 0, "total": 0})
        bd["total"] += 1
        if is_win: bd["win"] += 1
        elif is_loss: bd["loss"] += 1
        elif is_be: bd["be"] += 1
    cd = state["by_coin"].setdefault(coin, {"win": 0, "loss": 0, "be": 0, "total": 0})
    cd["total"] += 1
    if is_win: cd["win"] += 1
    elif is_loss: cd["loss"] += 1
    elif is_be: cd["be"] += 1
    state["updated_at"] = time.time()
    _save_json(LEARNING_FILE, state)


def apply_learning_adjustment(raw_score, side, detail, funding_rate, coin) -> tuple[int, list]:
    cfg = load_config()
    lcfg = cfg.get("learning", {})
    if not lcfg.get("enabled", True):
        return raw_score, []
    state = _load_json(LEARNING_FILE, {})
    buckets = state.get("buckets", {})
    min_n = lcfg.get("min_samples", 5)
    max_adj = lcfg.get("max_score_adjust", 10)
    notes, total = [], 0
    for b in _signal_buckets(raw_score, side, detail, funding_rate, coin):
        bd = buckets.get(b)
        if not bd or bd.get("total", 0) < min_n:
            continue
        wr = bd["win"] / bd["total"]
        if wr < 0.30: d = -3
        elif wr < 0.40: d = -2
        elif wr > 0.70: d = 2
        elif wr > 0.60: d = 1
        else: continue
        total += d
        notes.append(f"{b} (n={bd['total']}, 勝率 {wr:.0%}) → {d:+d}")
    total = max(-max_adj, min(max_adj, total))
    return raw_score + total, notes


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


# ═════════════════════════════════════════════════════════
# 訊號生成（含 v14.1 精準改進）
# ═════════════════════════════════════════════════════════
def generate_signal(instId: str, df: list, current_price: float,
                    funding_rate: float | None = None,
                    score_threshold: int | None = None,
                    cfg: dict | None = None) -> dict | None:
    if df is None or len(df) < 50:
        return None
    if cfg is None:
        cfg = load_config()
    threshold = score_threshold if score_threshold is not None else cfg.get("score_threshold", 68)
    atr_max = cfg.get("atr_max_pct", 0.04)
    expire_h = cfg.get("signal_expire_hours", 24)
    atr = calc_atr(df)
    if atr / current_price > atr_max:
        return None

    funding_penalty_long = funding_rate and funding_rate > 0.0008
    funding_penalty_short = funding_rate and funding_rate < -0.0008

    coin = instId.split("-")[0]
    regime_info = detect_market_regime(df)
    if regime_info["regime"] == "range":
        threshold += 5
    if regime_info["volatile"]:
        threshold += 3
    mtf = fetch_mtf_trend(instId)

    candidates = []
    for side in ("LONG", "SHORT"):
        raw_score, grade, detail = calc_score(df, side, current_price, mtf=mtf)
        if side == "LONG" and funding_penalty_long:
            raw_score -= 5
        if side == "SHORT" and funding_penalty_short:
            raw_score -= 5
        detail["regime"] = regime_info["regime"]
        detail["adx"] = regime_info["adx"]
        detail["atr_pct"] = regime_info["atr_pct"]
        if detect_pullback(df, side):
            raw_score += 3
            detail["pullback"] = True
        adj_simple, n1 = apply_learning_adjustment(raw_score, side, detail, funding_rate, coin)
        adj_knn, n2 = apply_knn_learning(raw_score, side, detail, funding_rate, coin, mtf, regime_info)
        score = adj_simple + (adj_knn - raw_score)
        if n1 or n2:
            detail["learning_notes"] = n1 + n2
            detail["learning_adjust"] = score - raw_score
        if score < threshold:
            continue
        entry = current_price

        # 🎯 v14.1 #1：Swing-point SL
        sl, sl_method = calc_swing_sl(df, side, entry, atr, cfg)
        detail["sl_method"] = sl_method
        risk = abs(entry - sl)
        if risk <= 0:
            continue

        # 🎯 v14.1 #4：聚類 S/R 動態 TP
        tp_levels, tp_notes = calc_dynamic_tp(entry, side, atr, risk, df, cfg)
        if tp_notes:
            detail["tp_notes"] = tp_notes

        # TP 順序 + R:R 檢查
        if side == "LONG":
            if not (entry < tp_levels[0] < tp_levels[1] < tp_levels[2]):
                continue
        else:
            if not (entry > tp_levels[0] > tp_levels[1] > tp_levels[2]):
                continue
        if abs(tp_levels[0] - entry) / risk < 1.4:  # TP1 至少 1.4R
            continue

        candidates.append({
            "instId": instId, "side": side, "tf": "15m",
            "entry": round(entry, 6), "sl": round(sl, 6),
            "tp1": round(tp_levels[0], 6),
            "tp2": round(tp_levels[1], 6),
            "tp3": round(tp_levels[2], 6),
            "score": int(score), "raw_score": int(raw_score),
            "grade": grade, "detail": detail,
            "funding_rate": funding_rate, "mtf_snapshot": mtf,
            "regime_snapshot": regime_info,
            "created": time.time(),
            "expires": time.time() + expire_h * 3600,
        })

    return max(candidates, key=lambda x: x["score"]) if candidates else None


# ═════════════════════════════════════════════════════════
# 資金管理
# ═════════════════════════════════════════════════════════
def calc_realized_r(close_type: str) -> float:
    """⅓ 分批假設"""
    if close_type == "TP3": return round((1.5 + 3.0 + 5.0) / 3, 2)
    if close_type == "LOCK": return round((1.5 + 3.0 + 1.5) / 3, 2)
    if close_type == "BE": return round(1.5 / 3, 2)
    return -1.0


# ═════════════════════════════════════════════════════════
# 通知格式
# ═════════════════════════════════════════════════════════
def _fmt_entry(coin: str, side: str, order_id: str, price: float,
               entry: float, sl: float, tp1: float, tp2: float, tp3: float,
               score: int, funding_rate: float | None = None,
               sl_method: str = "", extra_note: str = "") -> str:
    direction = "做多" if side == "LONG" else "做空"
    emoji = "🟢" if side == "LONG" else "🔴"
    grade = "🔥 A+ 極強" if score >= 85 else "⭐ A 強力" if score >= 70 else "✅ B+ 合格"
    tp1_pct = (tp1 - entry) / entry * 100 * (1 if side == "LONG" else -1)
    tp2_pct = (tp2 - entry) / entry * 100 * (1 if side == "LONG" else -1)
    tp3_pct = (tp3 - entry) / entry * 100 * (1 if side == "LONG" else -1)
    sl_pct = (sl - entry) / entry * 100
    funding_line = f"💰 資金費率：`{funding_rate * 100:+.4f}%`\n" if funding_rate is not None else ""
    sl_note = f" _({sl_method})_" if sl_method else ""
    extra = f"\n_{extra_note}_" if extra_note else ""
    return (
        f"{emoji} *{coin} 進場提醒* {grade}\n"
        f"━━━━━━━━━━━━━━\n"
        f"🆔 訂單：`{order_id}`\n"
        f"⏰ {tw_ts()}\n"
        f"方向：{direction}　評分：*{score} 分*\n"
        f"💰 進場：`{entry:.4f}`　當前：`{price:.4f}`\n"
        f"{funding_line}"
        f"🛑 SL：`{sl:.4f}` ({sl_pct:+.2f}%){sl_note}\n"
        f"🥇 TP1：`{tp1:.4f}` ({tp1_pct:+.2f}%)\n"
        f"🥈 TP2：`{tp2:.4f}` ({tp2_pct:+.2f}%)\n"
        f"🏆 TP3：`{tp3:.4f}` ({tp3_pct:+.2f}%){extra}"
    )


def _fmt_tp(coin: str, side: str, order_id: str, tp_level: str,
            price: float, pnl_pct: float, r_mult: float, wick: bool = False) -> str:
    w = " 🪡" if wick else ""
    next_action = ("已平 ⅓ + SL 移到進場" if tp_level == "TP1"
                   else "再平 ⅓ + SL 鎖 TP1" if tp_level == "TP2"
                   else "全平收工 🏆")
    return (f"🎯 *{coin} {tp_level}* `{pnl_pct:+.2f}%` (`{r_mult:.1f}R`){w}\n"
            f"🆔 `{order_id[-8:]}` · {next_action} · {tw_now().strftime('%H:%M')}")


def _fmt_final_close(coin: str, side: str, order_id: str,
                     close_type: str, pnl_pct: float,
                     realized_r: float, wick: bool = False) -> str:
    w = " 🪡" if wick else ""
    if close_type == "TP3":
        return (f"🏆 *{coin} TP3 全部達標* `{pnl_pct:+.2f}%` (`{realized_r:.1f}R`){w}\n"
                f"🆔 `{order_id[-8:]}` · {tw_now().strftime('%H:%M')}")
    if close_type == "LOCK":
        return (f"🔐 *{coin} 鎖利出場* `{pnl_pct:+.2f}%` (`{realized_r:.1f}R`){w}\n"
                f"🆔 `{order_id[-8:]}` · {tw_now().strftime('%H:%M')}")
    if close_type == "BE":
        return (f"🔒 *{coin} 保本出場* `{pnl_pct:+.2f}%` (`{realized_r:.1f}R`){w}\n"
                f"🆔 `{order_id[-8:]}` · {tw_now().strftime('%H:%M')}")
    return (f"❌ *{coin} 止損* `{pnl_pct:+.2f}%` (`-1R`){w}\n"
            f"🆔 `{order_id[-8:]}` · {tw_now().strftime('%H:%M')}")


def _fmt_position(sig: dict, current_price: float) -> str:
    coin = sig["instId"].split("-")[0]
    side = sig["side"]
    direction = "做多" if side == "LONG" else "做空"
    entry = sig["entry"]
    pnl = (((current_price - entry) / entry * 100) if side == "LONG"
           else ((entry - current_price) / entry * 100))
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
        f"🆔 `{sig.get('order_id', 'N/A')}`\n"
        f"方向：{direction}　當前：`{current_price:.4f}` {pnl_emoji}{pnl:+.2f}%\n"
        f"進場：`{entry:.4f}`　SL：`{sig['sl']:.4f}`\n"
        f"TP1 `{sig['tp1']:.4f}`{'✅' if sig.get('hit_tp1') else ''}　"
        f"TP2 `{sig['tp2']:.4f}`{'✅' if sig.get('hit_tp2') else ''}　"
        f"TP3 `{sig['tp3']:.4f}`{'✅' if sig.get('hit_tp3') else ''}\n"
        f"進度：{progress}"
    )


# ═════════════════════════════════════════════════════════
# 覆盤分析（v14 全套保留）
# ═════════════════════════════════════════════════════════
def analyze_loss(sig: dict, df_at_loss: list) -> list:
    if not df_at_loss or len(df_at_loss) < 20:
        return [{"code": "SHORT", "title": "📋 資料不足",
                 "detail": "進場後 K 線太少", "severity": 0}]
    side = sig["side"]
    expect = 1 if side == "LONG" else -1
    n = len(df_at_loss)
    df_then = df_at_loss[: max(20, n // 3)]
    df_now = df_at_loss
    reasons = []
    if calc_supertrend(df_then) == expect and calc_supertrend(df_now) == -expect:
        reasons.append({"code": "TREND_REVERSAL", "title": "🔄 趨勢反轉",
                        "detail": "進場時順勢，止損前已翻向反向", "severity": 30})
    rsi_then, rsi_now = calc_rsi(df_then), calc_rsi(df_now)
    if side == "LONG" and rsi_then > 45 and rsi_now < 35 and (rsi_then - rsi_now) > 12:
        reasons.append({"code": "RSI_COLLAPSE", "title": "📉 多頭動能瓦解",
                        "detail": f"RSI 從 {rsi_then:.0f} 跌至 {rsi_now:.0f}", "severity": 25})
    elif side == "SHORT" and rsi_then < 55 and rsi_now > 65 and (rsi_now - rsi_then) > 12:
        reasons.append({"code": "RSI_REBOUND", "title": "📈 空頭動能反轉",
                        "detail": f"RSI 從 {rsi_then:.0f} 反彈至 {rsi_now:.0f}", "severity": 25})
    sweep_dir = "SHORT" if side == "LONG" else "LONG"
    if detect_liquidity_sweep(df_now[-12:], sweep_dir):
        reasons.append({"code": "LIQ_SWEEP", "title": "🌊 流動性掃蕩",
                        "detail": "止損前出現反向假突破", "severity": 22})
    atr_then, atr_now = calc_atr(df_then), calc_atr(df_now)
    if atr_then > 0 and atr_now / atr_then > 1.5:
        reasons.append({"code": "VOL_SPIKE", "title": "🌪 波動率激增",
                        "detail": f"ATR {atr_then:.4f} → {atr_now:.4f}", "severity": 18})
    ob = find_order_block(df_then, side)
    if ob:
        breached = ((side == "LONG" and df_now[-1]["c"] < ob["low"])
                    or (side == "SHORT" and df_now[-1]["c"] > ob["high"]))
        if breached:
            reasons.append({"code": "OB_BROKEN", "title": "🧱 訂單塊跌破",
                            "detail": "進場依據的 OB 已失效", "severity": 20})
    if not reasons:
        reasons.append({"code": "NORMAL_NOISE", "title": "📊 正常波動雜訊",
                        "detail": "可能 SL 設得太緊", "severity": 5})
    reasons.sort(key=lambda x: -x["severity"])
    return reasons[:3]


def _generate_lessons(reasons: list) -> list:
    advice = {
        "TREND_REVERSAL": "進場後若 Supertrend 翻反向，主動減倉不等止損",
        "RSI_COLLAPSE": "RSI 從中性區急跌到超賣通常代表動能轉換",
        "RSI_REBOUND": "RSI 從中性區反彈到超買代表空頭動能瓦解",
        "LIQ_SWEEP": "插針型止損後反向 K 隨即出現，多半是主力掃損",
        "OB_BROKEN": "SMC OB 收盤跌破代表結構失效",
        "VOL_SPIKE": "ATR 突然擴張代表進入高波動區",
        "NORMAL_NOISE": "本次屬正常波動，可能 SL 太緊（考慮 swing SL）",
        "SHORT": "進場後資料不足無法歸因",
    }
    out, seen = [], set()
    for r in reasons[:2]:
        c = r.get("code")
        if c in seen or c not in advice:
            continue
        seen.add(c)
        out.append(advice[c])
    return out


def get_similar_stats(score, side, detail, funding_rate, coin) -> tuple:
    state = _load_json(LEARNING_FILE, {})
    bd = state.get("buckets", {}).get(f"coin_side:{coin}_{side}", {})
    n = bd.get("total", 0)
    return (n, bd.get("win", 0), bd.get("loss", 0), bd.get("be", 0))


def _fmt_postmortem(sig: dict, mode: str, reasons: list,
                    lessons: list, similar: tuple | None = None) -> str:
    coin = sig["instId"].split("-")[0]
    order_id = sig.get("order_id", "N/A")
    direction = "多" if sig["side"] == "LONG" else "空"
    label = ("❌ 止損" if mode == "LOSS" else "🔒 保本" if mode == "BE"
             else "🔐 鎖利" if mode == "LOCK" else "🎯 止盈")
    lines = [
        f"🔍 *{coin} {direction}單覆盤* `#{order_id[-8:]}`",
        f"━━━━━━━━━━━━━━",
        f"{label} · 原始評分 `{sig.get('score', 0)}`",
        f"",
        f"🎯 *主因（依嚴重度）：*",
    ]
    for i, r in enumerate(reasons[:3], 1):
        lines.append(f"{i}. {r['title']}")
        if r.get("detail"):
            lines.append(f"   _{r['detail']}_")
    if lessons:
        lines.append("")
        lines.append(f"💡 *教訓：* {lessons[0]}")
    if similar and similar[0] >= 3:
        n, w, l, be = similar
        wr = w / n * 100
        lines.append("")
        lines.append(f"📚 同類歷史：{n} 筆，勝率 `{wr:.0f}%`")
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════
# 風控
# ═════════════════════════════════════════════════════════
def check_circuit_breaker(cfg: dict) -> tuple[bool, str, int]:
    cb = cfg.get("circuit_breaker", {})
    if not cb.get("enabled", True):
        return False, "", 0
    history = _load_json(TRADE_HISTORY_FILE, [])
    recent = [t for t in history
              if t.get("close_type") in ("SL", "BE", "LOCK", "TP3")][-20:]
    if not recent:
        return False, "", 0
    losses = 0
    last_loss_time = None
    for t in reversed(recent):
        if t.get("close_type") == "SL":
            losses += 1
            if last_loss_time is None:
                try:
                    last_loss_time = datetime.strptime(
                        t["time"], "%Y-%m-%d %H:%M").replace(tzinfo=TW_TZ)
                except Exception:
                    last_loss_time = tw_now()
        else:
            break
    if losses == 0 or last_loss_time is None:
        return False, "", 0
    elapsed = (tw_now() - last_loss_time).total_seconds() / 3600
    hard_n = cb.get("hard_threshold", 5)
    hard_h = cb.get("hard_pause_hours", 24)
    soft_n = cb.get("soft_threshold", 3)
    soft_h = cb.get("soft_pause_hours", 4)
    if losses >= hard_n and elapsed < hard_h:
        return True, f"🚨 硬熔斷：連 {losses} 敗，剩餘 `{hard_h - elapsed:.1f}h`", losses
    if losses >= soft_n and elapsed < soft_h:
        return True, f"⚠️ 軟熔斷：連 {losses} 敗，剩餘 `{soft_h - elapsed:.1f}h`", losses
    return False, "", losses


def is_blackout_time(cfg: dict) -> tuple[bool, str]:
    now = tw_now()
    cur = now.hour * 60 + now.minute
    for w in cfg.get("blackout_windows_tw", []):
        try:
            sh, sm = map(int, w["start"].split(":"))
            eh, em = map(int, w["end"].split(":"))
            sm_t, em_t = sh * 60 + sm, eh * 60 + em
            in_w = (sm_t <= cur < em_t if sm_t <= em_t
                    else (cur >= sm_t or cur < em_t))
            if in_w:
                return True, w.get("reason", "風險時段")
        except Exception:
            continue
    return False, ""


def is_in_news_window(cfg: dict) -> tuple[bool, str]:
    now = tw_now()
    for nb in cfg.get("news_blackouts", []):
        try:
            s = datetime.fromisoformat(nb["start"])
            e = datetime.fromisoformat(nb["end"])
            if s.tzinfo is None:
                s = s.replace(tzinfo=TW_TZ); e = e.replace(tzinfo=TW_TZ)
            if s <= now <= e:
                return True, nb.get("reason", "新聞")
        except Exception:
            continue
    auto = cfg.get("auto_news_blackout", {})
    if auto.get("nfp", True) and now.weekday() == 4 and now.day <= 7:
        cur = now.hour * 60 + now.minute
        if 21 * 60 + 25 <= cur < 22 * 60 + 30:
            return True, "NFP 非農（自動偵測）"
    if auto.get("cpi", True) and 10 <= now.day <= 16:
        cur = now.hour * 60 + now.minute
        if 21 * 60 + 25 <= cur < 22 * 60 + 30:
            return True, "CPI 時段（自動偵測）"
    return False, ""


# ═════════════════════════════════════════════════════════
# 交易記錄（v14.1 #5：只記一次）
# ═════════════════════════════════════════════════════════
def record_trade_final(sig: dict, close_type: str, close_price: float) -> None:
    """🎯 v14.1 #5：修正 v14.0 同一筆單記 2~4 次的 bug，只記一次最終結算"""
    side = sig["side"]
    entry = sig["entry"]
    if close_type == "TP3":
        avg_close = (sig["tp1"] + sig["tp2"] + sig["tp3"]) / 3
    elif close_type == "LOCK":
        avg_close = (sig["tp1"] + sig["tp2"] + close_price) / 3
    elif close_type == "BE":
        avg_close = (sig["tp1"] + close_price * 2) / 3
    else:
        avg_close = close_price
    pnl_pct = (((avg_close - entry) / entry * 100) if side == "LONG"
               else ((entry - avg_close) / entry * 100))
    realized_r = calc_realized_r(close_type)
    coin = sig["instId"].split("-")[0]
    order_id = sig.get("order_id", "?")
    detail = sig.get("detail", {})
    fr = sig.get("funding_rate")
    mtf = sig.get("mtf_snapshot")
    regime = sig.get("regime_snapshot")
    features = vectorize_signal(sig.get("score", 0), side, detail, fr, mtf, regime)
    trade = {
        "time": tw_now().strftime("%Y-%m-%d %H:%M"),
        "date": tw_now().strftime("%Y-%m-%d"),
        "order_id": order_id, "coin": coin, "side": side,
        "entry": entry, "close": round(avg_close, 6),
        "close_type": close_type,
        "pnl": round(pnl_pct, 2),
        "realized_r": realized_r,
        "tp_hits": {"tp1": bool(sig.get("hit_tp1")),
                    "tp2": bool(sig.get("hit_tp2")),
                    "tp3": bool(sig.get("hit_tp3"))},
        "is_win": close_type in ("TP3", "LOCK"),
        "score": sig.get("score", 0),
        "raw_score": sig.get("raw_score", sig.get("score", 0)),
        "funding_rate": fr, "detail": detail,
        "features": features, "mtf": mtf, "regime": regime,
    }
    history = _load_json(TRADE_HISTORY_FILE, [])
    history.append(trade)
    _save_json(TRADE_HISTORY_FILE, history)
    logging.info(f"📝 記錄：{coin} {order_id} {close_type} ({pnl_pct:+.2f}% / {realized_r:+.1f}R)")
    try:
        update_learning(trade, sig)
    except Exception as e:
        logging.warning(f"⚠️ 學習更新失敗：{e}")


# ═════════════════════════════════════════════════════════
# SignalTracker（含 v14.1 tick check + 只記一次）
# ═════════════════════════════════════════════════════════
class SignalTracker:
    def __init__(self, filepath: str = ACTIVE_SIGNALS_FILE):
        self.filepath = filepath
        self.signals: dict = _load_json(filepath, {})
        self.transitions = 0
        self.cfg = load_config()

    def _save(self) -> None:
        _save_json(self.filepath, self.signals)

    def add(self, signal: dict, active: bool = False) -> tuple[str, str]:
        order_id = f"{int(time.time())}-{uuid.uuid4().hex[:8].upper()}"
        key = f"{signal['instId']}_{signal['side']}_{order_id}"
        now_ts = time.time()
        self.signals[key] = {
            **signal, "order_id": order_id,
            "status": "ACTIVE" if active else "PENDING",
            "hit_tp1": False, "hit_tp2": False, "hit_tp3": False,
            "activated_at": now_ts if active else None,
            "last_checked_ts": now_ts if active else None,
            "entry_message_id": None,
            "sl_original": signal["sl"],
        }
        self._save()
        logging.info(f"📌 新增：{order_id} ({signal['instId']} {signal['side']})")
        return key, order_id

    def has_open_position(self, instId: str) -> bool:
        return any(s for s in self.signals.values()
                   if s.get("instId") == instId
                   and s.get("status") in ("PENDING", "ACTIVE", "BE", "TRAIL"))

    def set_entry_message_id(self, key: str, msg_id: int | None) -> None:
        if key in self.signals and msg_id:
            self.signals[key]["entry_message_id"] = msg_id
            self._save()

    # 🎯 v14.1 #3：Tick check（不等 K 線）
    def quick_tick_check(self, force_fresh: bool = True) -> list:
        """⚡ 對所有 ACTIVE 持倉做純 price vs SL/TP 檢查"""
        ttl = self.cfg.get("tick_check", {}).get("price_cache_ttl_active", 2)
        to_remove = []
        for key, sig in list(self.signals.items()):
            if sig.get("status") not in ("ACTIVE", "BE", "TRAIL"):
                continue
            price = fetch_price(sig["instId"], force_fresh=force_fresh, cache_ttl=ttl)
            if price <= 0:
                continue
            synth = {"ts": int(time.time() * 1000),
                     "o": price, "h": price, "l": price, "c": price,
                     "v": 0, "confirmed": False}
            try:
                if self._process_candle(sig, synth):
                    to_remove.append(key)
                    logging.info(f"⚡ tick 觸發出場：{sig['instId']} {sig['side']} @ {price}")
            except Exception as e:
                logging.error(f"❌ tick_check [{key}]: {e}")
        for key in to_remove:
            del self.signals[key]
        if to_remove:
            self._save()
        return to_remove

    def check_all(self) -> None:
        self.transitions = 0
        self.quick_tick_check(force_fresh=True)  # ⚡ 先做 tick check
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
            if price <= 0:
                return False
            sig["current_price"] = price
            status = sig["status"]
            if status == "PENDING":
                return self._check_pending(sig, price)
            if status not in ("ACTIVE", "BE", "TRAIL"):
                return False
            # max_hold
            max_hold_h = self.cfg.get("max_hold_hours", 48)
            activated = sig.get("activated_at", time.time())
            if (time.time() - activated) / 3600 > max_hold_h:
                self._force_close_by_timeout(sig, price)
                return True
            # K 線處理
            all_candles = fetch_candles_full(sig["instId"])
            last_ts_s = (sig.get("last_checked_ts") or sig.get("activated_at")
                         or sig.get("created") or 0)
            new_candles = [c for c in all_candles if c["ts"] > int(last_ts_s * 1000)]
            if new_candles and price > 0:
                last = dict(new_candles[-1])
                last["h"] = max(last["h"], price)
                last["l"] = min(last["l"], price)
                new_candles[-1] = last
            for c in new_candles:
                if self._process_candle(sig, c):
                    return True
            confirmed = [c for c in new_candles if c.get("confirmed")]
            if confirmed:
                sig["last_checked_ts"] = max(c["ts"] for c in confirmed) / 1000.0
                self._save()
            return False
        except Exception as e:
            logging.error(f"❌ check_one [{key}]：{e}")
            return False

    def _check_pending(self, sig: dict, price: float) -> bool:
        coin = sig["instId"].split("-")[0]
        order_id = sig["order_id"]
        side = sig["side"]
        entry = sig["entry"]
        if time.time() > sig["expires"]:
            send_tg(f"⏰ *{coin} 訊號過期*\n🆔 `{order_id}`")
            self.transitions += 1
            return True
        # 進場區判斷
        if side == "LONG":
            in_zone = entry * 0.994 <= price <= entry * 1.002
        else:
            in_zone = entry * 0.998 <= price <= entry * 1.006
        if not in_zone:
            return False

        # 🎯 v14.1 #2：5m 進場確認
        confirmed, reason = confirm_entry_on_5m(sig["instId"], side, self.cfg)
        if not confirmed:
            logging.info(f"⏸ {coin} 在進場區但 5m 未確認：{reason}")
            return False

        now_ts = time.time()
        sig["status"] = "ACTIVE"
        sig["activated_at"] = now_ts
        sig["last_checked_ts"] = now_ts
        msg_id = send_tg(
            _fmt_entry(coin, side, order_id, price, entry, sig["sl"],
                       sig["tp1"], sig["tp2"], sig["tp3"], sig["score"],
                       sig.get("funding_rate"),
                       sl_method=sig.get("detail", {}).get("sl_method", ""),
                       extra_note=f"5m 確認：{reason}"),
            reply_markup=_order_keyboard(order_id),
        )
        if msg_id:
            sig["entry_message_id"] = msg_id
        self._save()
        self.transitions += 1
        return False

    def _process_candle(self, sig: dict, candle: dict) -> bool:
        side = sig["side"]
        entry = sig["entry"]
        sl = sig["sl"]
        tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
        coin = sig["instId"].split("-")[0]
        order_id = sig["order_id"]
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

        # SL 優先（保守）
        if self.cfg.get("conservative_sl_first", True) and against_hit(sl):
            return self._finalize_close(sig, sl, wick_against(sl))

        # TP1（不記錄，只標記）
        if not sig.get("hit_tp1") and favor_hit(tp1):
            sig["hit_tp1"] = True
            sig["sl"] = entry
            sig["status"] = "BE"
            pnl = ((tp1 - entry) / entry * 100) if side == "LONG" else ((entry - tp1) / entry * 100)
            send_tg(_fmt_tp(coin, side, order_id, "TP1", tp1, pnl, 1.5, wick_favor(tp1)),
                    reply_markup=kb, reply_to_message_id=reply_to)
            self._save()
            self.transitions += 1
        # TP2（不記錄）
        if not sig.get("hit_tp2") and favor_hit(tp2):
            sig["hit_tp2"] = True
            sig["sl"] = tp1
            sig["status"] = "TRAIL"
            pnl = ((tp2 - entry) / entry * 100) if side == "LONG" else ((entry - tp2) / entry * 100)
            send_tg(_fmt_tp(coin, side, order_id, "TP2", tp2, pnl, 3.0, wick_favor(tp2)),
                    reply_markup=kb, reply_to_message_id=reply_to)
            self._save()
            self.transitions += 1
        # TP3 → 結束
        if not sig.get("hit_tp3") and favor_hit(tp3):
            sig["hit_tp3"] = True
            return self._finalize_close(sig, tp3, wick_favor(tp3))
        # SL（非保守模式下走這裡）
        if not self.cfg.get("conservative_sl_first", True) and against_hit(sl):
            return self._finalize_close(sig, sl, wick_against(sl))
        return False

    def _finalize_close(self, sig: dict, exit_price: float, wick: bool) -> bool:
        side = sig["side"]
        entry = sig["entry"]
        coin = sig["instId"].split("-")[0]
        order_id = sig["order_id"]
        if sig.get("hit_tp3"):
            close_type = "TP3"
        elif sig.get("hit_tp2"):
            close_type = "LOCK"
        elif sig.get("hit_tp1"):
            close_type = "BE"
        else:
            close_type = "SL"
        if close_type == "TP3":
            avg_close = (sig["tp1"] + sig["tp2"] + sig["tp3"]) / 3
        elif close_type == "LOCK":
            avg_close = (sig["tp1"] + sig["tp2"] + exit_price) / 3
        elif close_type == "BE":
            avg_close = (sig["tp1"] + exit_price * 2) / 3
        else:
            avg_close = exit_price
        pnl_pct = (((avg_close - entry) / entry * 100) if side == "LONG"
                   else ((entry - avg_close) / entry * 100))
        realized_r = calc_realized_r(close_type)
        send_tg(_fmt_final_close(coin, side, order_id, close_type, pnl_pct, realized_r, wick),
                reply_markup=_order_keyboard(order_id),
                reply_to_message_id=sig.get("entry_message_id"))
        record_trade_final(sig, close_type, exit_price)
        if close_type != "TP3":
            self._send_postmortem(sig, close_type)
        self.transitions += 1
        return True

    def _force_close_by_timeout(self, sig: dict, price: float) -> None:
        side = sig["side"]
        entry = sig["entry"]
        coin = sig["instId"].split("-")[0]
        order_id = sig["order_id"]
        in_profit = (price > entry if side == "LONG" else price < entry)
        if sig.get("hit_tp2"):
            close_type, exit_price = "LOCK", sig["tp1"]
        elif sig.get("hit_tp1"):
            close_type, exit_price = "BE", entry
        else:
            close_type = "BE" if in_profit else "SL"
            exit_price = price if not in_profit else entry
        send_tg(f"⏰ *{coin} 持倉超時自動平倉*\n🆔 `{order_id}` · 當前 `{price:.4f}`")
        pnl_pct = (((exit_price - entry) / entry * 100) if side == "LONG"
                   else ((entry - exit_price) / entry * 100))
        send_tg(_fmt_final_close(coin, side, order_id, close_type, pnl_pct,
                                  calc_realized_r(close_type), False),
                reply_markup=_order_keyboard(order_id),
                reply_to_message_id=sig.get("entry_message_id"))
        record_trade_final(sig, close_type, exit_price)
        if close_type != "TP3":
            self._send_postmortem(sig, close_type)
        self.transitions += 1

    def _send_postmortem(self, sig: dict, mode: str) -> None:
        coin = sig["instId"].split("-")[0]
        order_id = sig.get("order_id", "?")
        try:
            if not self.cfg.get("post_mortem", {}).get("enabled", True):
                return
            activated = sig.get("activated_at") or sig.get("created") or 0
            all_c = fetch_candles_full(sig["instId"])
            df = [c for c in all_c if (c["ts"] / 1000) >= (activated - 900)]
            if len(df) < 10:
                return
            reasons = analyze_loss(sig, df)
            lessons = _generate_lessons(reasons)
            similar = get_similar_stats(sig.get("score", 0), sig["side"],
                                         sig.get("detail", {}),
                                         sig.get("funding_rate"), coin)
            send_tg(_fmt_postmortem(sig, mode, reasons, lessons, similar),
                    reply_to_message_id=sig.get("entry_message_id"))
            if mode == "SL" or mode == "LOSS":
                record_loss_reason(coin, sig["side"], reasons)
        except Exception as e:
            logging.error(f"❌ 覆盤失敗：{e}")

    def send_position_updates(self) -> None:
        state = get_system_state()
        now = time.time()
        if now - state.get("last_position_update_ts", 0) < 15 * 60:
            return
        cnt = 0
        for sig in self.signals.values():
            if sig["status"] not in ("ACTIVE", "BE", "TRAIL"):
                continue
            price = fetch_price(sig["instId"])
            if price <= 0:
                continue
            send_tg(_fmt_position(sig, price),
                    reply_markup=_order_keyboard(sig.get("order_id", "")),
                    reply_to_message_id=sig.get("entry_message_id"))
            cnt += 1
        if cnt:
            state["last_position_update_ts"] = now
            set_system_state(state)

    def get_position_stats(self) -> str:
        positions = list(self.signals.values())
        if not positions:
            return "📭 *目前無持倉*"
        lines = [f"📊 *追蹤中（{len(positions)} 筆）*", "═" * 18, ""]
        for i, p in enumerate(positions):
            price = fetch_price(p["instId"]) or p["entry"]
            coin = p["instId"].split("-")[0]
            side = p["side"]
            pnl = (((price - p["entry"]) / p["entry"] * 100) if side == "LONG"
                   else ((p["entry"] - price) / p["entry"] * 100))
            pnl_e = "🟢" if pnl >= 0 else "🔴"
            progress = ("🏆 TP3" if p.get("hit_tp3") else "🥈 TP2" if p.get("hit_tp2")
                        else "🥇 TP1" if p.get("hit_tp1") else "⏳")
            lines.append(
                f"*{coin}* · {side} · {p.get('score', 0)} 分\n"
                f"🆔 `{p.get('order_id', 'N/A')}`\n"
                f"當前 `{price:.4f}` {pnl_e}{pnl:+.2f}%\n"
                f"進場 `{p['entry']:.4f}` · SL `{p['sl']:.4f}`\n"
                f"進度：{progress}"
            )
            if i < len(positions) - 1:
                lines.append("─" * 18)
        return "\n".join(lines)


# ═════════════════════════════════════════════════════════
# 報表（v14 全套保留）
# ═════════════════════════════════════════════════════════
def _summarize_trades(trades: list) -> dict:
    n = len(trades)
    if n == 0: return {"n": 0}
    win = sum(1 for t in trades if t.get("close_type") in ("TP3", "LOCK"))
    loss = sum(1 for t in trades if t.get("close_type") == "SL")
    be = sum(1 for t in trades if t.get("close_type") == "BE")
    pnl = sum(t.get("pnl", 0) for t in trades)
    pnls = [t.get("pnl", 0) for t in trades]
    return {"n": n, "win": win, "loss": loss, "be": be,
            "wr": win / n * 100 if n else 0,
            "pnl": pnl, "avg": pnl / n if n else 0,
            "max_win": max(pnls) if pnls else 0,
            "max_loss": min(pnls) if pnls else 0}


def format_daily_report(date: str | None = None) -> str:
    if date is None:
        date = tw_now().strftime("%Y-%m-%d")
    history = _load_json(TRADE_HISTORY_FILE, [])
    today = [t for t in history if t.get("date") == date]
    s = _summarize_trades(today)
    if s["n"] == 0:
        return f"📭 *日報 {date}*\n當日尚無交易"
    return (f"📊 *日報 {date}*\n━━━━━━━━━━━━━━\n"
            f"交易：{s['n']} 筆（勝 {s['win']} / 平 {s['be']} / 敗 {s['loss']}）\n"
            f"勝率：`{s['wr']:.0f}%`　PnL：`{s['pnl']:+.2f}%`\n"
            f"平均：`{s['avg']:+.2f}%/筆`")


def format_monthly_report(year_month: str | None = None) -> str:
    if year_month is None:
        year_month = tw_now().strftime("%Y-%m")
    history = _load_json(TRADE_HISTORY_FILE, [])
    month = [t for t in history if t.get("date", "").startswith(year_month)]
    s = _summarize_trades(month)
    if s["n"] == 0:
        return f"📭 *月報 {year_month}*\n本月尚無交易"
    return (f"📈 *月報 {year_month}*\n━━━━━━━━━━━━━━\n"
            f"總交易：{s['n']} 筆（勝 {s['win']} / 平 {s['be']} / 敗 {s['loss']}）\n"
            f"勝率：`{s['wr']:.0f}%`　PnL：`{s['pnl']:+.2f}%`")


def format_learning_report() -> str:
    state = _load_json(LEARNING_FILE, {})
    by_coin = state.get("by_coin", {})
    if not by_coin:
        return "🧠 *學習狀態*\n📭 尚未累積資料"
    lines = ["🧠 *機器人學習狀態*", "━━━━━━━━━━━━━━", "", "📊 *各幣種戰績*："]
    for c, d in sorted(by_coin.items(), key=lambda x: -x[1].get("total", 0))[:12]:
        n = d.get("total", 0); w = d.get("win", 0)
        l = d.get("loss", 0); be = d.get("be", 0)
        wr = w / n * 100 if n else 0
        lines.append(f"  {c}: {n} 筆（勝 {w} / 平 {be} / 敗 {l}，勝率 `{wr:.0f}%`）")
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════
# 主掃描
# ═════════════════════════════════════════════════════════
def run_scan(tracker: SignalTracker) -> int:
    logging.info("🚀 開始掃描...")
    cfg = load_config()
    coins = cfg.get("coins", ALL_COINS)
    max_signals = cfg.get("max_signals", MAX_SIGNALS)
    score_thr = cfg.get("score_threshold", SCORE_THRESHOLD)
    cooldown_h = cfg.get("cooldown_hours", COOLDOWN_HOURS)
    atr_max = cfg.get("atr_max_pct", 0.04)
    pv_cfg = cfg.get("price_verification", {})
    pv_enabled = pv_cfg.get("enabled", True)
    pv_max_dev = pv_cfg.get("max_deviation_pct", 0.5)
    pv_block = pv_cfg.get("block_on_unverified", False)
    state = get_system_state()

    # ⚡ 首要：tick check 所有活躍部位
    if tracker.signals:
        logging.info("⚡ 入口 tick check...")
        tracker.quick_tick_check(force_fresh=True)

    paused, msg, losses = check_circuit_breaker(cfg)
    if paused:
        if not state.get("circuit_active"):
            send_tg(msg)
            state["circuit_active"] = True
            state["circuit_since"] = time.time()
            set_system_state(state)
        tracker.check_all()
        tracker.send_position_updates()
        return 0
    else:
        if state.get("circuit_active"):
            send_tg("✅ *熔斷已解除*")
            state["circuit_active"] = False
            state["circuit_since"] = None
            set_system_state(state)

    blocked, btime_reason = is_blackout_time(cfg)
    if blocked:
        logging.info(f"🕒 {btime_reason}")
        tracker.check_all()
        tracker.send_position_updates()
        return 0

    in_news, news_reason = is_in_news_window(cfg)
    if in_news:
        logging.info(f"📰 {news_reason}")
        tracker.check_all()
        tracker.send_position_updates()
        return 0

    sent = 0
    for instId in coins:
        if sent >= max_signals:
            break
        if tracker.has_open_position(instId):
            continue
        if is_cooling(instId, cooldown_h):
            continue
        try:
            # 🎯 v14.1 #6：流動性過濾
            ok, reason = passes_liquidity_filter(instId, cfg)
            if not ok:
                logging.info(f"[{instId}] 流動性過濾：{reason}")
                continue

            okx_price = fetch_price(instId)
            if okx_price <= 0:
                continue
            if pv_enabled:
                ok2, tv_price, diff = verify_price(instId, okx_price, pv_max_dev, pv_block)
                if not ok2:
                    if tv_price is not None:
                        send_tg(
                            f"⚠️ *{instId.split('-')[0]} 價格異常*\n"
                            f"OKX `{okx_price:.4f}` vs TV `{tv_price:.4f}`\n"
                            f"偏離 `{diff:.3f}%` > `{pv_max_dev}%`"
                        )
                    continue
            df = fetch_candles(instId)
            if df is None:
                continue
            funding = fetch_funding_rate(instId)
            signal = generate_signal(
                instId, df, okx_price, funding,
                score_threshold=score_thr, cfg=cfg,
            )
            if not signal:
                continue
            # 進場區判斷
            side = signal["side"]
            entry = signal["entry"]
            if side == "LONG":
                in_zone = entry * 0.994 <= okx_price <= entry * 1.002
            else:
                in_zone = entry * 0.998 <= okx_price <= entry * 1.006

            # 🎯 v14.1 #2：即使在進場區，也要 5m 確認才送 ACTIVE
            if in_zone:
                confirmed, reason = confirm_entry_on_5m(instId, side, cfg)
                if not confirmed:
                    logging.info(f"[{instId}] 在進場區但 5m 未確認 → 改 PENDING")
                    in_zone = False  # 退到 PENDING 等下次 5m 確認

            key, order_id = tracker.add(signal, active=in_zone)
            if in_zone:
                msg = _fmt_entry(
                    instId.split("-")[0], side, order_id, okx_price,
                    entry, signal["sl"], signal["tp1"], signal["tp2"], signal["tp3"],
                    signal["score"], funding,
                    sl_method=signal.get("detail", {}).get("sl_method", ""),
                    extra_note="5m 已確認進場",
                )
                msg_id = send_tg(msg, reply_markup=_order_keyboard(order_id))
                tracker.set_entry_message_id(key, msg_id)
                logging.info(f"✅ {instId} 進場（{order_id}）")
            else:
                send_tg(
                    f"📍 *{instId.split('-')[0]} 訊號就位*\n"
                    f"🆔 `{order_id}`\n"
                    f"進場 `{entry:.4f}`（當前 `{okx_price:.4f}`）\n"
                    f"等價格進入區間 + 5m 確認自動觸發",
                    reply_markup=_order_keyboard(order_id),
                )
                logging.info(f"📍 {instId} PENDING（{order_id}）")
            mark_cooldown(instId, cooldown_h)
            sent += 1
        except Exception as e:
            logging.error(f"[{instId}] 失敗：{e}")
            continue

    tracker.check_all()
    tracker.send_position_updates()
    logging.info(f"✅ 掃描完成，本輪 {sent} 筆")

    # intensive_monitor
    if tracker.signals:
        intensive_monitor(tracker)
    return sent


def intensive_monitor(tracker: SignalTracker) -> None:
    """⚡ 3 秒一輪、跑 55 秒"""
    cfg = load_config()
    im = cfg.get("intensive_monitor", {})
    if not im.get("enabled", True):
        return
    total = im.get("total_seconds", 55)
    interval = im.get("interval_seconds", 3)
    end_time = time.time() + total
    polls = 0
    while time.time() < end_time and tracker.signals:
        time.sleep(interval)
        try:
            tracker.quick_tick_check(force_fresh=True)
            polls += 1
        except Exception as e:
            logging.error(f"❌ intensive_monitor: {e}")
    logging.info(f"📡 即時監控完成：{polls} 輪")


def run_monitor(tracker: SignalTracker, polls: int = 1, interval: int = 30) -> None:
    if not tracker.signals:
        return
    for i in range(polls):
        try:
            tracker.check_all()
            if i < polls - 1:
                time.sleep(interval)
        except Exception as e:
            logging.error(f"❌ monitor poll: {e}")


# ═════════════════════════════════════════════════════════
# 主入口
# ═════════════════════════════════════════════════════════
def main() -> None:
    if not TG_TOKEN or not CHAT_ID:
        sys.stderr.write("❌ TG_TOKEN 或 CHAT_ID 未設定\n")
        sys.exit(1)
    try:
        logging.info("=" * 50)
        logging.info("🤖 Alpha Oracle Pro v14.1 更精準版")
        logging.info(f"⏰ {tw_ts()}")
        logging.info("=" * 50)
        tracker = SignalTracker(ACTIVE_SIGNALS_FILE)
        if len(sys.argv) > 1:
            cmd = sys.argv[1]
            if cmd in ("/stats", "/持倉", "stats"):
                send_tg(tracker.get_position_stats()); return
            if cmd in ("/learning", "/學習", "learning"):
                send_tg(format_learning_report()); return
            if cmd in ("/daily", "/日報", "daily"):
                date = sys.argv[2] if len(sys.argv) > 2 else None
                send_tg(format_daily_report(date)); return
            if cmd in ("/monthly", "/月報", "monthly"):
                ym = sys.argv[2] if len(sys.argv) > 2 else None
                send_tg(format_monthly_report(ym)); return
            if cmd in ("monitor", "/monitor", "/監控"):
                polls = int(sys.argv[2]) if len(sys.argv) > 2 else 1
                interval = int(sys.argv[3]) if len(sys.argv) > 3 else 30
                run_monitor(tracker, polls, interval); return
            if cmd in ("tick", "/tick"):
                tracker.quick_tick_check(force_fresh=True); return
        run_scan(tracker)
        logging.info("🎉 完成")
    except Exception as e:
        logging.error(f"🔥 系統錯誤：{e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
