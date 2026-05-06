#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro v15.2 — 頂級交易員模式（繁體中文）
══════════════════════════════════════════════════════════════════════
✨ 設計原則：保留 v14.3 完整功能、修掉所有 bug、砍掉反效果與裝飾項

✅ 9 大評分項：
   趨勢(30) RSI(25) OB(20) FVG(15) SNR(5) PA(5) 流動性(5) 動能(5)
   + MTF 多時框 ±15 + 量能 ±10 + EMA 排列 ±5 + 學習調整 ±10

✅ 8 大風控：
   1. score_threshold 評分門檻
   2. min_rr_ratio R:R 最低 1.5
   3. cooldown_hours 同幣冷卻
   4. circuit_breaker 連敗熔斷（單級：連 3 敗 24h）
   5. atr_max_pct 波動過濾
   6. blackout_windows 風險時段
   7. max_concurrent_positions 同時持倉上限
   8. daily_loss_limit_pct 當日損失紅線

✅ 學習機制（雙路）：
   - KNN 找最相似 10 筆歷史交易看勝率
   - 桶統計（分數/RSI/資金費率/時段/幣種/方向）

✅ 完整覆盤：6 大主因（趨勢反轉/RSI崩盤/流動性掃蕩/波動激增/反向動能/OB跌破）

✅ 資金管理：依 SL 距離自動算槓桿，$100 本金 / $20 風險

✨ v14.x 砍掉的（沒驗證 / 反效果 / 裝飾）：
   ❌ VWAP（與趨勢高度相關，多餘加減分）
   ❌ cooling_off（circuit_breaker 已涵蓋）
   ❌ overheating + 爛幣自動停（均值回歸反效果）
   ❌ god signal 神級標記（純裝飾）
   ❌ direction_bias（樣本不足是噪音，KNN 已包含）
   ❌ max_daily_signals（cooldown + threshold 已限）

✨ v14.x → v15.1 的改進：
   🔧 修復 TP 順序 bug（DOGE TP3 < TP2 collapse）
   ⚡ 即時價合併 K 線（OKX K 線 API 延遲時搶先抓）
   🔄 TG 訊息加重試（429/5xx 自動 backoff）
   🛡️ 持倉更新 15 分鐘 throttle（不洗版）
   💵 資金 / 槓桿 / 美元損益試算
   📊 1 分鐘 cron + 早期退出（無新單時 5 秒搞定）
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
# 1. 基礎設定
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

ACTIVE_SIGNALS_FILE = "active_signals.json"
TRADE_HISTORY_FILE = "trade_history.json"
SIGNAL_COOLDOWN_FILE = "signal_cooldown.json"
SYSTEM_STATE_FILE = "system_state.json"
LEARNING_FILE = "learning_state.json"
CONFIG_FILE = "config.json"

DEFAULT_CONFIG: dict = {
    "coins": ALL_COINS,
    "max_signals_per_scan": 1,
    "score_threshold": 85,
    "cooldown_hours": 3,
    "signal_expire_hours": 24,
    "atr_max_pct": 0.03,
    "min_rr_ratio": 1.5,
    "show_score_breakdown": False,
    # 🛡️ v15.2 新增：必要條件（HARD FILTER）
    "hard_filters": {
        "require_mtf_h4_align": True,    # 4H 趨勢必須順向（反向直接拒絕）
        "min_volume_ratio": 0.8,          # 最後 K 量必須 ≥ 0.8 倍均量
        "min_adx": 22,                    # 必須趨勢市（ADX > 22）
    },

    "capital_management": {
        "capital_per_trade_usd": 100,
        "max_loss_usd": 20,
        "max_leverage": 50,
        "min_leverage": 2,
    },

    # 🛡️ 兩條真正的紅線（max_daily_signals 已砍）
    "daily_limits": {
        "max_concurrent_positions": 2,
        "daily_loss_limit_pct": 5.0,
    },

    # 🔥 連敗熔斷（單級，cooling_off 已砍因為重複）
    "circuit_breaker": {
        "loss_threshold": 3,
        "pause_hours": 24,
    },

    # 📡 雙來源價格驗證
    "price_verification": {
        "enabled": True,
        "max_deviation_pct": 0.5,
        "block_on_unverified": False,
    },

    # 🧠 學習（KNN + 桶統計）
    "learning": {
        "enabled": True,
        "knn_enabled": True,
        "min_samples": 5,
        "max_score_adjust": 10,
    },

    # 🔍 覆盤
    "post_mortem": {
        "enabled": True,
        "loss_only": False,
    },

    # 🕒 風險時段
    "blackout_windows_tw": [
        {"start": "07:50", "end": "08:10", "reason": "資金費率結算（00 UTC）"},
        {"start": "15:50", "end": "16:10", "reason": "資金費率結算（08 UTC）"},
        {"start": "23:50", "end": "00:10", "reason": "資金費率結算（16 UTC）"},
        {"start": "21:25", "end": "21:45", "reason": "美股開盤波動"},
        {"start": "02:00", "end": "02:30", "reason": "FOMC 公布時段"},
    ],
    "auto_news_blackout": {
        "nfp": True,
        "cpi": True,
    },
    "news_blackouts": [],
}

_price_cache: dict = {}
_candle_cache: dict = {}
_mtf_cache: dict = {}
_tv_cache: dict = {}


# ═════════════════════════════════════════════════════════
# 2. 持久化 + 配置
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


# ═════════════════════════════════════════════════════════
# 3. Telegram 通知（含重試）
# ═════════════════════════════════════════════════════════
def send_tg(msg: str, parse_mode: str = "Markdown",
            reply_markup: dict | None = None,
            reply_to_message_id: int | None = None,
            max_retries: int = 3) -> int | None:
    """📤 送 TG，429/5xx 自動重試"""
    if not TG_TOKEN or not CHAT_ID:
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

    for attempt in range(max_retries):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json=payload, timeout=8,
            )
            if r.status_code == 200:
                try:
                    s = _load_json(SYSTEM_STATE_FILE, {})
                    s["last_tg_sent"] = time.time()
                    _save_json(SYSTEM_STATE_FILE, s)
                except Exception:
                    pass
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
# 4. 數據抓取
# ═════════════════════════════════════════════════════════
def fetch_price(instId: str) -> float:
    now = time.time()
    if instId in _price_cache:
        price, t = _price_cache[instId]
        if now - t < 5:
            return price
    try:
        r = requests.get(
            f"https://www.okx.com/api/v5/market/ticker?instId={instId}",
            timeout=5,
        ).json()
        if r.get("code") == "0" and r.get("data"):
            price = float(r["data"][0]["last"])
            if price > 0:
                _price_cache[instId] = (price, now)
                return price
    except Exception as e:
        logging.warning(f"⚠️ {instId} 價格失敗：{e}")
    return _price_cache.get(instId, (0.0, 0))[0]


def fetch_candles(instId: str, tf: str = "15m", limit: int = 100,
                  include_unconfirmed: bool = True) -> list:
    cache_key = f"{instId}_{tf}_{limit}_{include_unconfirmed}"
    now = time.time()
    if cache_key in _candle_cache:
        candles, t = _candle_cache[cache_key]
        if now - t < 30:
            return candles
    try:
        r = requests.get(
            f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}",
            timeout=6,
        ).json()
        if r.get("code") != "0":
            return _candle_cache.get(cache_key, ([], 0))[0]
        candles = []
        for row in r.get("data", []):
            confirmed = row[8] == "1"
            if not include_unconfirmed and not confirmed:
                continue
            candles.append({
                "ts": int(row[0]), "o": float(row[1]), "h": float(row[2]),
                "l": float(row[3]), "c": float(row[4]), "v": float(row[5]),
                "confirmed": confirmed,
            })
        candles.sort(key=lambda x: x["ts"])
        _candle_cache[cache_key] = (candles, now)
        return candles
    except Exception as e:
        logging.warning(f"⚠️ {instId} K 線失敗：{e}")
        return _candle_cache.get(cache_key, ([], 0))[0]


def fetch_funding_rate(instId: str) -> float | None:
    try:
        r = requests.get(
            f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}",
            timeout=5,
        ).json()
        if r.get("code") == "0" and r.get("data"):
            return float(r["data"][0]["fundingRate"])
    except Exception:
        pass
    return None


def fetch_mtf_trend(instId: str) -> dict:
    """🕒 1H + 4H 趨勢（30 秒快取）"""
    now = time.time()
    if instId in _mtf_cache:
        data, t = _mtf_cache[instId]
        if now - t < 30:
            return data
    out = {}
    for tf in ("1H", "4H"):
        df = fetch_candles(instId, tf=tf, limit=50, include_unconfirmed=False)
        if df:
            st = calc_supertrend(df)
            out[tf] = {"supertrend": st, "rsi": round(calc_rsi(df), 1)}
        else:
            out[tf] = {"supertrend": 0, "rsi": 50}
    _mtf_cache[instId] = (out, now)
    return out


def fetch_price_tv(instId: str) -> float | None:
    """📡 TradingView 第二來源（10 秒快取）"""
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
        h = TA_Handler(
            symbol=symbol, exchange="OKX", screener="crypto",
            interval=Interval.INTERVAL_1_MINUTE, timeout=8,
        )
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
    """⚖️ OKX vs TradingView 雙來源價格驗證"""
    tv_price = fetch_price_tv(instId)
    if tv_price is None:
        return (not block_on_unverified, None, 0.0)
    diff = abs(okx_price - tv_price) / okx_price * 100
    if diff > max_dev_pct:
        return False, tv_price, diff
    return True, tv_price, diff


# ═════════════════════════════════════════════════════════
# 5. 基礎技術指標
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
    mid = sum(c["c"] for c in df[-20:]) / 20
    cur = df[-1]["c"]
    band = atr * 0.5
    if cur > mid + band:
        return 1
    if cur < mid - band:
        return -1
    return 0


def calc_adx(df: list, period: int = 14) -> float:
    """📐 ADX 趨勢強度：>22 強趨勢、<18 震盪、中間過渡"""
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


# ═════════════════════════════════════════════════════════
# 6. SMC / ICT / SNR / PA / 流動性 / 動能
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
    return min(c["l"] for c in seg), max(c["h"] for c in seg)


def detect_price_action(df: list, side: str) -> bool:
    """📊 Pin Bar 或吞沒形態"""
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
    """💧 流動性掃蕩"""
    if len(df) < lookback + 1:
        return False
    seg = df[-(lookback + 1):-1]
    last = df[-1]
    pl = min(c["l"] for c in seg)
    ph = max(c["h"] for c in seg)
    mid = (pl + ph) / 2
    if side == "LONG":
        return last["l"] < pl and last["c"] > mid
    return last["h"] > ph and last["c"] < mid


def calc_momentum_ratio(df: list, side: str, n: int = 5) -> bool:
    """📈 盤口動能：最近 N 根 K 多空比例"""
    seg = df[-n:]
    bull = sum(1 for c in seg if c["c"] > c["o"])
    ratio = bull / max(1, len(seg))
    return ratio >= 0.6 if side == "LONG" else ratio <= 0.4


# ═════════════════════════════════════════════════════════
# 7. EMA + 量能
# ═════════════════════════════════════════════════════════
def calc_ema(df: list, period: int) -> float:
    if len(df) < period:
        return df[-1]["c"] if df else 0.0
    closes = [c["c"] for c in df]
    m = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * m + ema * (1 - m)
    return ema


def calc_ema_alignment(df: list, side: str) -> tuple[int, str]:
    """🪜 EMA 多週期排列：完美排列 +5 / 部分 +3 / 逆 EMA200 -5"""
    if len(df) < 200 + 5:
        return 0, ""
    e20, e50, e200 = calc_ema(df, 20), calc_ema(df, 50), calc_ema(df, 200)
    p = df[-1]["c"]
    if side == "LONG":
        if p > e20 > e50 > e200:
            return 5, "多頭完美排列"
        if p > e20 > e50:
            return 3, "短中期多頭"
        if p < e200:
            return -5, "在 EMA200 之下"
    else:
        if p < e20 < e50 < e200:
            return 5, "空頭完美排列"
        if p < e20 < e50:
            return 3, "短中期空頭"
        if p > e200:
            return -5, "在 EMA200 之上"
    return 0, ""


def calc_volume_quality(df: list, lookback: int = 20) -> tuple[float, int]:
    """📊 量能品質 → (倍數, 評分 -10 ~ +8)"""
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


def calc_mtf_alignment(mtf: dict, side: str) -> tuple[int, str]:
    """🎯 多時框共振 → (-15 ~ +15, 描述)"""
    expect = 1 if side == "LONG" else -1
    h1 = mtf.get("1H", {}).get("supertrend", 0)
    h4 = mtf.get("4H", {}).get("supertrend", 0)
    s = 0
    if h1 == expect:
        s += 8
    elif h1 == -expect:
        s -= 5
    if h4 == expect:
        s += 7
    elif h4 == -expect:
        s -= 5
    s = max(-15, min(15, s))
    desc = f"1H={'順' if h1 == expect else '反' if h1 == -expect else '中'} / 4H={'順' if h4 == expect else '反' if h4 == -expect else '中'}"
    return s, desc


# ═════════════════════════════════════════════════════════
# 8. 評分系統
# ═════════════════════════════════════════════════════════
def calc_score(df: list, side: str, current_price: float,
               mtf: dict | None = None, instId: str | None = None) -> tuple[int, str, dict]:
    """總分 = 趨勢30+RSI25+OB20+FVG15+SNR5+PA5+流動性5+動能5 + MTF±15 + 量能±8 + EMA±5"""
    detail = {}
    score = 0

    # 趨勢 30
    st = calc_supertrend(df)
    if (side == "LONG" and st == 1) or (side == "SHORT" and st == -1):
        score += 30; detail["trend"] = 30
    elif st == 0:
        score += 15; detail["trend"] = 15
    else:
        detail["trend"] = 0

    # RSI 25
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

    # OB 20
    ob = find_order_block(df, side)
    if ob and ob["low"] * 0.995 <= current_price <= ob["high"] * 1.005:
        score += 20; detail["ob"] = 20
    else:
        detail["ob"] = 0

    # FVG 15
    fvg = find_fvg(df, side)
    if fvg and fvg["low"] * 0.997 <= current_price <= fvg["high"] * 1.003:
        score += 15; detail["fvg"] = 15
    else:
        detail["fvg"] = 0

    # SNR 5
    sup, res = calc_snr(df)
    if side == "LONG" and current_price <= sup * 1.01:
        score += 5; detail["snr"] = 5
    elif side == "SHORT" and current_price >= res * 0.99:
        score += 5; detail["snr"] = 5
    else:
        detail["snr"] = 0

    # PA 5
    detail["pa"] = 5 if detect_price_action(df, side) else 0
    score += detail["pa"]

    # 流動性 5
    detail["liq"] = 5 if detect_liquidity_sweep(df, side) else 0
    score += detail["liq"]

    # 動能 5
    detail["mom"] = 5 if calc_momentum_ratio(df, side) else 0
    score += detail["mom"]

    # MTF ±15
    if mtf is None and instId:
        mtf = fetch_mtf_trend(instId)
    if mtf:
        mtf_s, mtf_d = calc_mtf_alignment(mtf, side)
        score += mtf_s
        detail["mtf"] = mtf_s
        detail["mtf_desc"] = mtf_d

    # 量能 ±8
    vol_r, vol_s = calc_volume_quality(df)
    score += vol_s
    detail["volume"] = vol_s
    detail["vol_ratio"] = vol_r

    # EMA ±5
    ema_s, ema_d = calc_ema_alignment(df, side)
    score += ema_s
    detail["ema"] = ema_s
    detail["ema_desc"] = ema_d

    grade = (
        "🔥 A+ 極強" if score >= 90
        else "⭐ A 強力" if score >= 80
        else "✅ B+ 合格" if score >= 70
        else "⚪ 觀望"
    )
    return score, grade, detail


# ═════════════════════════════════════════════════════════
# 9. 學習機制（KNN + 桶統計）
# ═════════════════════════════════════════════════════════
_FEATURE_SCALE = {
    "score": 30, "rsi": 50, "atr_pct": 3, "funding": 2,
    "vol_ratio": 3, "mtf_h1": 1, "mtf_h4": 1, "side": 1,
}


def vectorize_signal(score: int, side: str, detail: dict, funding_rate,
                     mtf: dict | None = None) -> dict:
    rsi = (detail or {}).get("rsi_value", 50)
    return {
        "score": float(score),
        "rsi": float(rsi),
        "atr_pct": float((detail or {}).get("atr_pct", 1.0)),
        "funding": float(funding_rate or 0) * 1000,
        "vol_ratio": float((detail or {}).get("vol_ratio", 1.0)),
        "mtf_h1": 1.0 if (mtf or {}).get("1H", {}).get("supertrend") == (1 if side == "LONG" else -1) else 0.0,
        "mtf_h4": 1.0 if (mtf or {}).get("4H", {}).get("supertrend") == (1 if side == "LONG" else -1) else 0.0,
        "side": 1.0 if side == "LONG" else 0.0,
    }


def find_similar_trades(features: dict, history: list, k: int = 10) -> list:
    candidates = []
    for t in history:
        f = t.get("features")
        if not f:
            continue
        d2 = sum(
            ((features.get(key, 0) - f.get(key, 0)) / max(scale, 1)) ** 2
            for key, scale in _FEATURE_SCALE.items()
        )
        candidates.append((d2, t))
    candidates.sort(key=lambda x: x[0])
    return [t for _, t in candidates[:k]]


def apply_knn_learning(score: int, side: str, detail: dict, funding_rate,
                       mtf: dict | None) -> tuple[int, list]:
    """🧬 KNN：找最相似 10 筆歷史交易看勝率"""
    cfg = load_config()
    if not cfg.get("learning", {}).get("knn_enabled", True):
        return score, []
    history = _load_json(TRADE_HISTORY_FILE, [])
    if len(history) < 10:
        return score, []
    feat = vectorize_signal(score, side, detail, funding_rate, mtf)
    similar = find_similar_trades(feat, history, k=10)
    if len(similar) < 3:
        return score, []
    wins = sum(1 for t in similar if t.get("close_type") in ("TP1", "TP2", "TP3", "LOCK"))
    n = len(similar)
    wr = wins / n
    notes = [f"🧬 KNN：{n} 筆最相似 → 勝 {wins} (勝率 {wr:.0%})"]
    if wr < 0.30:
        return score - 8, notes + ["低勝率 -8"]
    if wr < 0.40:
        return score - 4, notes + ["偏低 -4"]
    if wr > 0.70:
        return score + 5, notes + ["高勝率 +5"]
    if wr > 0.60:
        return score + 3, notes + ["中高 +3"]
    return score, notes


def _bucket_score(s: int) -> str:
    if s >= 90:
        return "score:90+"
    if s >= 80:
        return "score:80-89"
    if s >= 70:
        return "score:70-79"
    return "score:60-69"


def _bucket_rsi(rsi: float, side: str) -> str:
    b = int(rsi // 10) * 10
    return f"rsi_{side.lower()}:{b}-{b + 9}"


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


def _signal_buckets(score: int, side: str, detail: dict, funding_rate, coin: str) -> list:
    rsi = (detail or {}).get("rsi_value", 50)
    return [
        _bucket_score(score),
        _bucket_rsi(rsi, side),
        _bucket_funding(funding_rate),
        f"coin:{coin}",
        f"coin_side:{coin}_{side}",
    ]


def update_learning(trade: dict, sig_snapshot: dict | None = None) -> None:
    state = _load_json(LEARNING_FILE, {})
    state.setdefault("buckets", {})
    state.setdefault("by_coin", {})
    state.setdefault("loss_reasons", [])

    score = trade.get("score", 0)
    coin = trade.get("coin", "?")
    side = trade.get("side", "?")
    ct = trade.get("close_type", "?")
    fr = trade.get("funding_rate")
    detail = trade.get("detail") or (sig_snapshot or {}).get("detail", {})

    is_win = ct in ("TP1", "TP2", "TP3", "LOCK")
    is_be = ct == "BE"
    is_loss = ct == "SL"

    for b in _signal_buckets(score, side, detail, fr, coin):
        bd = state["buckets"].setdefault(b, {"win": 0, "loss": 0, "be": 0, "total": 0})
        bd["total"] += 1
        if is_win:
            bd["win"] += 1
        elif is_loss:
            bd["loss"] += 1
        elif is_be:
            bd["be"] += 1

    cd = state["by_coin"].setdefault(coin, {"win": 0, "loss": 0, "be": 0, "total": 0})
    cd["total"] += 1
    if is_win:
        cd["win"] += 1
    elif is_loss:
        cd["loss"] += 1
    elif is_be:
        cd["be"] += 1

    state["updated_at"] = time.time()
    _save_json(LEARNING_FILE, state)


def apply_learning_adjustment(score: int, side: str, detail: dict,
                              funding_rate, coin: str) -> tuple[int, list]:
    cfg = load_config()
    lcfg = cfg.get("learning", {})
    if not lcfg.get("enabled", True):
        return score, []
    state = _load_json(LEARNING_FILE, {})
    buckets = state.get("buckets", {})
    min_n = lcfg.get("min_samples", 5)
    max_adj = lcfg.get("max_score_adjust", 10)

    notes, total = [], 0
    for b in _signal_buckets(score, side, detail, funding_rate, coin):
        bd = buckets.get(b)
        if not bd or bd.get("total", 0) < min_n:
            continue
        wr = bd["win"] / bd["total"]
        if wr < 0.30:
            d = -3
        elif wr < 0.40:
            d = -2
        elif wr > 0.70:
            d = 2
        elif wr > 0.60:
            d = 1
        else:
            continue
        total += d
        notes.append(f"{b} (n={bd['total']}, 勝率 {wr:.0%}) → {d:+d}")
    total = max(-max_adj, min(max_adj, total))
    return score + total, notes


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
# 10. 訊號生成
# ═════════════════════════════════════════════════════════
def generate_signal(instId: str, df: list, current_price: float,
                    funding_rate: float | None = None,
                    score_threshold: int = 75,
                    atr_max_pct: float = 0.04,
                    signal_expire_hours: int = 24) -> dict | None:
    if df is None or len(df) < 50:
        return None
    atr = calc_atr(df)
    atr_pct = atr / current_price * 100
    if atr / current_price > atr_max_pct:
        return None

    coin = instId.split("-")[0]
    mtf = fetch_mtf_trend(instId)

    # 🛡️ Hard filter（必要條件，沒過直接拒絕）
    cfg_hf = load_config().get("hard_filters", {})

    # ① ADX 必須 > 22（趨勢市才開單）
    min_adx = cfg_hf.get("min_adx", 22)
    if min_adx > 0:
        adx = calc_adx(df)
        if adx < min_adx:
            return None  # 震盪市直接拒絕

    # ② 量能必須 ≥ 0.8 倍均量
    min_vol = cfg_hf.get("min_volume_ratio", 0.8)
    if min_vol > 0:
        vol_ratio, _ = calc_volume_quality(df)
        if vol_ratio < min_vol:
            return None  # 沒量直接拒絕

    candidates = []
    for side in ("LONG", "SHORT"):
        # ③ MTF 4H 必須順向（反向直接跳過）
        if cfg_hf.get("require_mtf_h4_align", True):
            expect = 1 if side == "LONG" else -1
            h4 = mtf.get("4H", {}).get("supertrend", 0)
            if h4 == -expect:
                continue  # 4H 反向，跳過此方向

        score, grade, detail = calc_score(df, side, current_price, mtf=mtf)
        detail["atr_pct"] = round(atr_pct, 3)

        # 學習雙路：桶統計 + KNN
        adj_score, knn_notes = apply_knn_learning(score, side, detail, funding_rate, mtf)
        adj_score, bucket_notes = apply_learning_adjustment(
            adj_score, side, detail, funding_rate, coin
        )
        if knn_notes or bucket_notes:
            detail["learning_notes"] = knn_notes + bucket_notes
            detail["learning_adjust"] = adj_score - score
        score = adj_score

        if score < score_threshold:
            continue

        entry = current_price
        sl_dist = atr * 1.5
        sl = entry - sl_dist if side == "LONG" else entry + sl_dist
        risk = abs(entry - sl)

        # 固定 1.5R / 3R / 5R（修掉 dynamic TP collapse bug）
        if side == "LONG":
            tp1 = entry + risk * 1.5
            tp2 = entry + risk * 3.0
            tp3 = entry + risk * 5.0
        else:
            tp1 = entry - risk * 1.5
            tp2 = entry - risk * 3.0
            tp3 = entry - risk * 5.0

        # R:R 最低門檻
        cfg = load_config()
        min_rr = cfg.get("min_rr_ratio", 1.5)
        actual_tp1_r = abs(tp1 - entry) / max(risk, 1e-9)
        if actual_tp1_r < min_rr - 0.02:
            continue

        ob_zone = find_order_block(df, side)
        candidates.append({
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
            "mtf_snapshot": mtf,
            "ob_zone": ob_zone,
            "created": time.time(),
            "expires": time.time() + signal_expire_hours * 3600,
        })

    return max(candidates, key=lambda x: x["score"]) if candidates else None


# ═════════════════════════════════════════════════════════
# 11. 資金管理
# ═════════════════════════════════════════════════════════
def calc_position_sizing(entry: float, sl: float, tp1: float, tp2: float,
                         tp3: float, side: str, cfg: dict | None = None) -> dict | None:
    if cfg is None:
        cfg = load_config()
    cm = cfg.get("capital_management", {})
    capital = cm.get("capital_per_trade_usd", 100)
    max_loss = cm.get("max_loss_usd", 20)
    max_lev = cm.get("max_leverage", 50)
    min_lev = cm.get("min_leverage", 2)

    sl_dist = abs(entry - sl) / entry
    if sl_dist <= 0:
        return None
    leverage = max(min_lev, min(max_lev, round((max_loss / capital) / sl_dist)))
    pos_value = capital * leverage
    contracts = pos_value / entry

    def _pnl(t):
        if side == "LONG":
            return pos_value * (t - entry) / entry
        return pos_value * (entry - t) / entry

    return {
        "capital": capital, "max_loss": max_loss, "leverage": int(leverage),
        "position_value": round(pos_value, 2), "contracts": round(contracts, 4),
        "sl_loss": round(abs(_pnl(sl)), 2),
        "tp1_profit": round(_pnl(tp1), 2),
        "tp2_profit": round(_pnl(tp2), 2),
        "tp3_profit": round(_pnl(tp3), 2),
    }


# ═════════════════════════════════════════════════════════
# 12. 通知格式
# ═════════════════════════════════════════════════════════
def _fmt_entry(sig: dict, current_price: float) -> str:
    """📌 進場通知（對齊用戶截圖格式）"""
    coin = sig["instId"].split("-")[0]
    side = sig["side"]
    entry, sl = sig["entry"], sig["sl"]
    tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
    score = sig["score"]
    direction = "做多" if side == "LONG" else "做空"
    emoji = "🟢" if side == "LONG" else "🔴"
    grade = "🔥 A+ 極強" if score >= 90 else "⭐ A 強力" if score >= 80 else "✅ B+ 合格"

    tp1_pct = abs(tp1 - entry) / entry * 100
    tp2_pct = abs(tp2 - entry) / entry * 100
    tp3_pct = abs(tp3 - entry) / entry * 100
    sl_pct = abs(sl - entry) / entry * 100

    sizing = calc_position_sizing(entry, sl, tp1, tp2, tp3, side)
    sizing_block = ""
    if sizing:
        sizing_block = (
            f"💵 槓桿 `{sizing['leverage']}x` · 倉 `${sizing['position_value']:,.0f}`\n"
            f"   SL `-${sizing['sl_loss']:.0f}` / TP1 `+${sizing['tp1_profit']:.0f}` / "
            f"TP2 `+${sizing['tp2_profit']:.0f}` / TP3 `+${sizing['tp3_profit']:.0f}`\n"
        )

    return (
        f"{emoji} *{coin} 進場提醒* {grade}\n"
        f"━━━━━━━━━━━━━━\n"
        f"🆔 `{sig['order_id']}`\n"
        f"⏰ {tw_ts()}\n"
        f"方向：{direction}　評分：*{score} 分*\n"
        f"💰 進場：`{entry:.4f}`\n"
        f"🛑 SL ：`{sl:.4f}`  (距 -{sl_pct:.2f}%)\n"
        f"🥇 TP1：`{tp1:.4f}`  (距 +{tp1_pct:.2f}% / 1.5R)\n"
        f"🥈 TP2：`{tp2:.4f}`  (距 +{tp2_pct:.2f}% / 3.0R)\n"
        f"🏆 TP3：`{tp3:.4f}`  (距 +{tp3_pct:.2f}% / 5.0R)\n"
        f"{sizing_block}"
    )


def _fmt_tp(coin: str, side: str, order_id: str, tp_level: str, price: float,
            pnl_pct: float, r_mult: float, wick_triggered: bool = False) -> str:
    """🎯 TP 通知（v15.2 精簡 2 行）"""
    wick = " 🪡" if wick_triggered else ""
    next_action = (
        "平 ⅓ 保本"
        if tp_level == "TP1"
        else "再平 ⅓ 鎖利"
        if tp_level == "TP2"
        else "全平收工 🏆"
    )
    return (
        f"🎯 *{coin} {tp_level}* `{pnl_pct:+.2f}%` (`{r_mult:.1f}R`){wick}\n"
        f"🆔 `{order_id[-8:]}` · {next_action} · {tw_now().strftime('%H:%M')}"
    )


def _fmt_sl(coin: str, side: str, order_id: str, price: float,
            pnl_pct: float, mode: str = "LOSS",
            r_value: float = -1.0, wick_triggered: bool = False) -> str:
    """🛑 SL/BE/LOCK 通知（v15.2 精簡 2 行）"""
    wick = " 🪡" if wick_triggered else ""
    if mode == "BE":
        return (
            f"🔒 *{coin} 保本* `0R`{wick}\n"
            f"🆔 `{order_id[-8:]}` · TP1 已收 SL 移到進場 · {tw_now().strftime('%H:%M')}"
        )
    if mode == "LOCK":
        return (
            f"🔐 *{coin} 鎖利* `+{r_value:.1f}R`{wick}\n"
            f"🆔 `{order_id[-8:]}` · TP2 已收 SL 已鎖 TP1 · {tw_now().strftime('%H:%M')}"
        )
    return (
        f"❌ *{coin} 止損* `{pnl_pct:+.2f}%` (`-1R`){wick}\n"
        f"🆔 `{order_id[-8:]}` · {tw_now().strftime('%H:%M')}"
    )


def _fmt_position(sig: dict, current_price: float) -> str:
    """📊 持倉更新（v15.2 精簡 3 行）"""
    coin = sig["instId"].split("-")[0]
    side = sig["side"]
    entry = sig["entry"]
    pnl = ((current_price - entry) / entry * 100) if side == "LONG" else ((entry - current_price) / entry * 100)
    pnl_e = "🟢" if pnl >= 0 else "🔴"
    if sig.get("hit_tp3"):
        progress = "🏆 全收"
    elif sig.get("hit_tp2"):
        progress = "🥇🥈 鎖利中"
    elif sig.get("hit_tp1"):
        progress = "🥇 保本中"
    else:
        progress = "⏳ 等 TP1"
    return (
        f"📊 *{coin}* {side} {pnl_e}`{pnl:+.2f}%`\n"
        f"當前 `{current_price:.4f}` / 進 `{entry:.4f}` / SL `{sig['sl']:.4f}`\n"
        f"{progress} · 🆔 `{sig.get('order_id', 'N/A')[-8:]}`"
    )


# ═════════════════════════════════════════════════════════
# 13. 覆盤分析（6 大主因）
# ═════════════════════════════════════════════════════════
def analyze_loss(sig: dict, df_at_loss: list) -> list:
    if not df_at_loss or len(df_at_loss) < 20:
        return [{"code": "SHORT", "title": "📋 資料不足", "detail": "進場後 K 線太少", "severity": 0}]

    side = sig["side"]
    expect = 1 if side == "LONG" else -1
    n = len(df_at_loss)
    df_then = df_at_loss[: max(20, n // 3)]
    df_now = df_at_loss
    reasons = []

    # 1. 趨勢反轉
    if calc_supertrend(df_then) == expect and calc_supertrend(df_now) == -expect:
        reasons.append({
            "code": "TREND_REVERSAL", "title": "🔄 趨勢反轉",
            "detail": f"進場時 Supertrend 順勢，止損前已翻向反向",
            "severity": 30,
        })

    # 2. RSI 動能瓦解 / 反彈
    rsi_then, rsi_now = calc_rsi(df_then), calc_rsi(df_now)
    if side == "LONG" and rsi_then > 45 and rsi_now < 35 and (rsi_then - rsi_now) > 12:
        reasons.append({
            "code": "RSI_COLLAPSE", "title": "📉 多頭動能瓦解",
            "detail": f"RSI 從 {rsi_then:.0f} 急跌至 {rsi_now:.0f}",
            "severity": 25,
        })
    elif side == "SHORT" and rsi_then < 55 and rsi_now > 65 and (rsi_now - rsi_then) > 12:
        reasons.append({
            "code": "RSI_REBOUND", "title": "📈 空頭動能反轉",
            "detail": f"RSI 從 {rsi_then:.0f} 反彈至 {rsi_now:.0f}",
            "severity": 25,
        })

    # 3. 流動性掃蕩（反向）
    sweep_dir = "SHORT" if side == "LONG" else "LONG"
    if detect_liquidity_sweep(df_now[-12:], sweep_dir):
        reasons.append({
            "code": "LIQ_SWEEP", "title": "🌊 流動性掃蕩",
            "detail": "止損前出現反向假突破插針後快速收回",
            "severity": 22,
        })

    # 4. OB 結構失效
    ob = find_order_block(df_then, side)
    if ob:
        breached = (
            (side == "LONG" and df_now[-1]["c"] < ob["low"])
            or (side == "SHORT" and df_now[-1]["c"] > ob["high"])
        )
        if breached:
            reasons.append({
                "code": "OB_BROKEN", "title": "🧱 訂單塊跌破",
                "detail": "進場依據的 SMC 訂單塊已被收盤跌破",
                "severity": 20,
            })

    # 5. 波動激增
    atr_then, atr_now = calc_atr(df_then), calc_atr(df_now)
    if atr_then > 0 and atr_now / atr_then > 1.5:
        reasons.append({
            "code": "VOL_SPIKE", "title": "🌪 波動率激增",
            "detail": f"ATR 從 {atr_then:.4f} 擴張至 {atr_now:.4f}（{(atr_now / atr_then - 1) * 100:.0f}%）",
            "severity": 18,
        })

    # 6. 連續反向 K
    last10 = df_now[-10:]
    against = sum(
        1 for c in last10
        if (side == "LONG" and c["c"] < c["o"]) or (side == "SHORT" and c["c"] > c["o"])
    )
    if against >= 7:
        reasons.append({
            "code": "AGAINST_K", "title": "💪 持續反向動能",
            "detail": f"出場前 10 根 K 線中 {against} 根反向收線",
            "severity": 15,
        })

    if not reasons:
        reasons.append({
            "code": "NORMAL_NOISE", "title": "📊 正常波動雜訊",
            "detail": "未偵測到明確的趨勢反轉或結構破壞",
            "severity": 5,
        })

    reasons.sort(key=lambda x: -x["severity"])
    return reasons[:3]


def _calc_postmortem_context(sig: dict, df_at_loss: list) -> dict:
    """🔍 計算覆盤的關鍵市況變化（給專業版覆盤用）"""
    if not df_at_loss or len(df_at_loss) < 20:
        return {}
    n = len(df_at_loss)
    df_then = df_at_loss[: max(20, n // 3)]
    df_now = df_at_loss

    atr_then = calc_atr(df_then)
    atr_now = calc_atr(df_now)
    rsi_then = calc_rsi(df_then)
    rsi_now = calc_rsi(df_now)
    st_then = calc_supertrend(df_then)
    st_now = calc_supertrend(df_now)
    adx_now = calc_adx(df_now)

    entry_price = sig["entry"]
    atr_pct_then = atr_then / entry_price * 100 if entry_price > 0 else 0
    atr_pct_now = atr_now / entry_price * 100 if entry_price > 0 else 0

    activated = sig.get("activated_at") or sig.get("created", time.time())
    duration_min = (time.time() - activated) / 60

    if len(df_now) >= 21:
        vol_avg = sum(c["v"] for c in df_now[-21:-1]) / 20
        vol_last = df_now[-1]["v"]
        vol_change = vol_last / vol_avg if vol_avg > 0 else 1.0
    else:
        vol_change = 1.0

    return {
        "duration_min": duration_min,
        "rsi_then": rsi_then,
        "rsi_now": rsi_now,
        "rsi_change": rsi_now - rsi_then,
        "atr_pct_then": atr_pct_then,
        "atr_pct_now": atr_pct_now,
        "atr_change_pct": (atr_now / atr_then - 1) * 100 if atr_then > 0 else 0,
        "st_then": st_then,
        "st_now": st_now,
        "adx_now": adx_now,
        "vol_change": vol_change,
    }


def _generate_lessons(reasons: list) -> list:
    advice = {
        "TREND_REVERSAL": "進場後若 Supertrend 翻向反向，建議主動減倉不等止損",
        "RSI_COLLAPSE": "RSI 從中性區（>45）急跌到超賣（<35）通常代表動能轉換",
        "RSI_REBOUND": "RSI 從中性區（<55）反彈到超買（>65）代表空頭動能瓦解",
        "LIQ_SWEEP": "插針型止損後反向 K 隨即出現，多半是主力誘多/誘空",
        "OB_BROKEN": "SMC 訂單塊一旦收盤跌破代表結構失效",
        "VOL_SPIKE": "ATR 突然擴張代表進入高波動區，可縮小倉位",
        "AGAINST_K": "反向 K 連續 7 根以上代表趨勢已轉，應主動止損",
        "NORMAL_NOISE": "本次屬正常波動雜訊，可能 SL 設得太緊",
        "SHORT": "進場後資料不足，無法詳細歸因",
    }
    out, seen = [], set()
    for r in reasons[:2]:
        c = r.get("code")
        if c and c not in seen and c in advice:
            seen.add(c)
            out.append(advice[c])
    return out


def get_similar_stats(score: int, side: str, detail: dict, funding_rate, coin: str) -> tuple:
    state = _load_json(LEARNING_FILE, {})
    bd = state.get("buckets", {}).get(f"coin_side:{coin}_{side}", {})
    n = bd.get("total", 0)
    return (n, bd.get("win", 0), bd.get("loss", 0), bd.get("be", 0))


def _fmt_postmortem(sig: dict, mode: str, reasons: list, lessons: list,
                    similar: tuple | None = None,
                    context: dict | None = None) -> str:
    """🔍 覆盤訊息（v15.2 專業交易員等級：含具體數據 + 市況變化 + 統計）"""
    coin = sig["instId"].split("-")[0]
    order_id = sig.get("order_id", "?")
    side = sig["side"]
    direction = "多" if side == "LONG" else "空"
    score = sig.get("score", 0)

    # 結算結果（含 USD 損益）
    sizing = calc_position_sizing(
        sig["entry"], sig["sl"], sig["tp1"], sig["tp2"], sig["tp3"], side
    )
    if mode == "BE":
        result_emoji = "🔒"
        result_text = "保本"
        result_r = "0R"
        result_usd = "$0"
    elif mode == "LOCK":
        result_emoji = "🔐"
        result_text = "鎖利"
        result_r = "+1.5R"
        result_usd = f"+${sizing['tp1_profit']:.0f}" if sizing else ""
    else:
        result_emoji = "❌"
        result_text = "止損"
        result_r = "-1R"
        result_usd = f"-${sizing['sl_loss']:.0f}" if sizing else ""

    # 持倉時長
    duration = ""
    if context and context.get("duration_min", 0) > 0:
        d = context["duration_min"]
        duration = f"{d / 60:.1f}h" if d > 60 else f"{d:.0f}m"

    # 進場時 MTF
    mtf = sig.get("mtf_snapshot", {})
    h1_st = mtf.get("1H", {}).get("supertrend", 0)
    h4_st = mtf.get("4H", {}).get("supertrend", 0)
    h1_arrow = "↑" if h1_st == 1 else "↓" if h1_st == -1 else "→"
    h4_arrow = "↑" if h4_st == 1 else "↓" if h4_st == -1 else "→"

    lines = [
        f"🔍 *{coin} {direction}單覆盤* `#{order_id[-8:]}`",
        f"━━━━━━━━━━━━━━",
        f"{result_emoji} 結算 *{result_text}* `{result_r}` / `{result_usd}`",
        f"📊 開單評分 `{score}` 分 · 持倉 `{duration}` · MTF 1H{h1_arrow} 4H{h4_arrow}",
        f"",
        f"🎯 *失敗主因（依嚴重度）：*",
    ]
    for i, r in enumerate(reasons[:3], 1):
        lines.append(f"{i}. {r['title']}")
        if r.get("detail"):
            lines.append(f"   _{r['detail']}_")

    # 📈 市況變化（具體數據）
    if context:
        ctx_lines = []
        if context.get("rsi_then") is not None and abs(context.get("rsi_change", 0)) >= 8:
            arrow = "↓" if context["rsi_change"] < 0 else "↑"
            ctx_lines.append(
                f"  RSI `{context['rsi_then']:.0f}` {arrow} `{context['rsi_now']:.0f}` "
                f"({context['rsi_change']:+.0f})"
            )
        if context.get("atr_change_pct", 0) != 0 and abs(context["atr_change_pct"]) > 30:
            ctx_lines.append(
                f"  ATR `{context['atr_pct_then']:.2f}%` → `{context['atr_pct_now']:.2f}%` "
                f"({context['atr_change_pct']:+.0f}%)"
            )
        st_t, st_n = context.get("st_then", 0), context.get("st_now", 0)
        if st_t != st_n and st_t != 0 and st_n != 0:
            from_arrow = "↑" if st_t == 1 else "↓"
            to_arrow = "↑" if st_n == 1 else "↓"
            ctx_lines.append(f"  Supertrend {from_arrow} → {to_arrow}（趨勢翻轉）")
        if context.get("vol_change", 1) >= 1.5:
            ctx_lines.append(f"  量能爆 `{context['vol_change']:.1f}x` 均量")
        if context.get("adx_now") is not None:
            ctx_lines.append(f"  當前 ADX `{context['adx_now']:.0f}`（{'趨勢' if context['adx_now'] > 25 else '震盪'}）")

        if ctx_lines:
            lines.append("")
            lines.append("📈 *出場前市況：*")
            lines.extend(ctx_lines)

    # 💡 教訓（最多 1 條）
    if lessons:
        lines.append("")
        lines.append(f"💡 *教訓：* {lessons[0]}")

    # 📚 同類設定統計
    if similar and similar[0] >= 3:
        n, w, l, be = similar
        wr = w / n * 100
        verdict = "✅ 高勝率組" if wr >= 60 else "⚠️ 中等" if wr >= 40 else "❌ 低勝率組"
        lines.append("")
        lines.append(
            f"📚 *同類歷史：* {n} 筆（勝 {w} / 敗 {l}），勝率 `{wr:.0f}%` {verdict}"
        )
        if wr < 40:
            lines.append(f"   _建議：類似條件出現時系統自動降權_")

    return "\n".join(lines)


# ═════════════════════════════════════════════════════════
# 14. 風控
# ═════════════════════════════════════════════════════════
def check_circuit_breaker(cfg: dict) -> tuple[bool, str]:
    cb = cfg.get("circuit_breaker", {})
    threshold = cb.get("loss_threshold", 3)
    pause_h = cb.get("pause_hours", 24)
    history = _load_json(TRADE_HISTORY_FILE, [])
    closed = [t for t in history if t.get("close_type") in ("SL", "BE", "LOCK", "TP1", "TP2", "TP3")]
    if len(closed) < threshold:
        return False, ""
    last_n = closed[-threshold:]
    if not all(t.get("close_type") == "SL" for t in last_n):
        return False, ""
    try:
        last_sl_dt = datetime.strptime(last_n[-1]["time"], "%Y-%m-%d %H:%M").replace(tzinfo=TW_TZ)
    except Exception:
        return False, ""
    elapsed = (tw_now() - last_sl_dt).total_seconds() / 3600
    if elapsed < pause_h:
        return True, f"🔥 連 {threshold} 敗熔斷，剩餘 `{pause_h - elapsed:.1f}h`"
    return False, ""


def is_in_news_window(cfg: dict) -> tuple[bool, str]:
    now = tw_now()
    for nb in cfg.get("news_blackouts", []):
        try:
            s = datetime.fromisoformat(nb["start"])
            e = datetime.fromisoformat(nb["end"])
            if s.tzinfo is None:
                s = s.replace(tzinfo=TW_TZ)
                e = e.replace(tzinfo=TW_TZ)
            if s <= now <= e:
                return True, nb.get("reason", "新聞事件")
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
            return True, "CPI 數據時段（自動偵測）"
    return False, ""


def is_blackout_time(cfg: dict) -> tuple[bool, str]:
    now = tw_now()
    cur = now.hour * 60 + now.minute
    for w in cfg.get("blackout_windows_tw", []):
        try:
            sh, sm = map(int, w["start"].split(":"))
            eh, em = map(int, w["end"].split(":"))
            sm_t, em_t = sh * 60 + sm, eh * 60 + em
            in_window = (
                sm_t <= cur < em_t if sm_t <= em_t
                else (cur >= sm_t or cur < em_t)
            )
            if in_window:
                return True, w.get("reason", "風險時段")
        except Exception:
            continue
    return False, ""


def get_today_stats() -> dict:
    today = tw_now().strftime("%Y-%m-%d")
    history = _load_json(TRADE_HISTORY_FILE, [])
    today_trades = [t for t in history if t.get("date") == today]
    closed = [t for t in today_trades if t.get("close_type") in ("SL", "BE", "LOCK", "TP1", "TP2", "TP3")]
    return {
        "trades_count": len(today_trades),
        "pnl_pct": sum(t.get("pnl", 0) for t in closed),
        "wins": sum(1 for t in closed if t.get("close_type") in ("TP1", "TP2", "TP3", "LOCK")),
        "losses": sum(1 for t in closed if t.get("close_type") == "SL"),
    }


def check_daily_limits(cfg: dict, tracker) -> tuple[bool, str]:
    """🛡️ 兩條紅線：max_concurrent + daily_loss"""
    dl = cfg.get("daily_limits", {})
    open_count = sum(
        1 for s in tracker.signals.values()
        if s.get("status") in ("PENDING", "ACTIVE", "BE", "TRAIL")
    )
    if open_count >= dl.get("max_concurrent_positions", 2):
        return True, f"📦 持倉達上限：{open_count}/{dl['max_concurrent_positions']}"
    stats = get_today_stats()
    loss_limit = dl.get("daily_loss_limit_pct", 5.0)
    if stats["pnl_pct"] < -loss_limit:
        return True, f"⚠️ 當日 PnL `{stats['pnl_pct']:.2f}%` 已過 -{loss_limit}% 紅線"
    return False, ""


def is_cooling(instId: str, hours: float = 3) -> bool:
    cd = _load_json(SIGNAL_COOLDOWN_FILE, {})
    last = cd.get(instId)
    return last is not None and (time.time() - float(last)) < hours * 3600


def mark_cooldown(instId: str, hours: float = 3) -> None:
    cd = _load_json(SIGNAL_COOLDOWN_FILE, {})
    cd[instId] = time.time()
    cutoff = time.time() - hours * 3600 * 3
    cd = {k: v for k, v in cd.items() if float(v) > cutoff}
    _save_json(SIGNAL_COOLDOWN_FILE, cd)


# ═════════════════════════════════════════════════════════
# 15. 健康監控
# ═════════════════════════════════════════════════════════
def check_health() -> tuple[bool, str]:
    state = get_system_state()
    last_tg = state.get("last_tg_sent", 0)
    last_warn = state.get("last_health_warning", 0)
    if time.time() - last_warn < 6 * 3600:
        return False, ""
    if last_tg > 0:
        hours = (time.time() - last_tg) / 3600
        if hours > 24:
            state["last_health_warning"] = time.time()
            set_system_state(state)
            return True, (
                f"⚠️ *系統健康警報*\n"
                f"超過 *{hours:.0f} 小時*沒送過 TG 訊息\n"
                f"檢查：TG_TOKEN / OKX API / Actions 配額"
            )
    return False, ""


# ═════════════════════════════════════════════════════════
# 16. 交易記錄
# ═════════════════════════════════════════════════════════
def record_trade(coin: str, side: str, order_id: str, entry: float,
                 close_price: float, close_type: str, score: int,
                 sig_snapshot: dict | None = None) -> None:
    is_win = close_type in ("TP1", "TP2", "TP3", "LOCK")
    pnl = ((close_price - entry) / entry * 100) if side == "LONG" else ((entry - close_price) / entry * 100)
    snap = sig_snapshot or {}
    detail = snap.get("detail", {}) or {}
    fr = snap.get("funding_rate")
    mtf = snap.get("mtf_snapshot")
    features = vectorize_signal(score, side, detail, fr, mtf)
    trade = {
        "time": tw_now().strftime("%Y-%m-%d %H:%M"),
        "date": tw_now().strftime("%Y-%m-%d"),
        "order_id": order_id, "coin": coin, "side": side,
        "entry": entry, "close": close_price, "close_type": close_type,
        "pnl": round(pnl, 2), "is_win": is_win, "score": score,
        "funding_rate": fr, "detail": detail,
        "features": features, "mtf": mtf,
    }
    history = _load_json(TRADE_HISTORY_FILE, [])
    history.append(trade)
    _save_json(TRADE_HISTORY_FILE, history)
    logging.info(f"📝 記錄：{coin} {order_id} {close_type} ({pnl:+.2f}%)")
    try:
        update_learning(trade, sig_snapshot)
    except Exception as e:
        logging.warning(f"⚠️ 學習更新失敗：{e}")


# ═════════════════════════════════════════════════════════
# 17. 訊號追蹤器
# ═════════════════════════════════════════════════════════
class SignalTracker:
    def __init__(self, filepath: str = ACTIVE_SIGNALS_FILE):
        self.filepath = filepath
        self.signals: dict = _load_json(filepath, {})
        self.transitions = 0

    def _save(self) -> None:
        _save_json(self.filepath, self.signals)

    def add(self, signal: dict, active: bool = False) -> tuple[str, str]:
        order_id = f"{int(time.time())}-{uuid.uuid4().hex[:8].upper()}"
        key = f"{signal['instId']}_{signal['side']}_{order_id}"
        now_ts = time.time()
        self.signals[key] = {
            **signal,
            "order_id": order_id,
            "status": "ACTIVE" if active else "PENDING",
            "hit_tp1": False, "hit_tp2": False, "hit_tp3": False,
            "activated_at": now_ts if active else None,
            "last_checked_ts": now_ts if active else None,
            "entry_message_id": None,
        }
        self._save()
        logging.info(f"📌 新增：{order_id} ({signal['instId']} {signal['side']})")
        return key, order_id

    def has_open_position(self, instId: str) -> bool:
        return any(
            s for s in self.signals.values()
            if s.get("instId") == instId
            and s.get("status") in ("PENDING", "ACTIVE", "BE", "TRAIL")
        )

    def set_entry_message_id(self, key: str, msg_id: int | None) -> None:
        if key in self.signals and msg_id:
            self.signals[key]["entry_message_id"] = msg_id
            self._save()

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
            if price <= 0:
                return False
            sig["current_price"] = price
            status = sig["status"]
            if status == "PENDING":
                return self._check_pending(sig, price)
            if status not in ("ACTIVE", "BE", "TRAIL"):
                return False

            all_candles = fetch_candles(sig["instId"], tf="15m", limit=100)
            last_ts_s = (
                sig.get("last_checked_ts") or sig.get("activated_at")
                or sig.get("created") or 0
            )
            new_candles = [c for c in all_candles if c["ts"] > int(last_ts_s * 1000)]

            # 即時價合併進最後一根 K
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
            send_tg(f"⏰ *{coin} 訊號過期*\n🆔 `{order_id}`\n進場 `{entry:.4f}` 未觸發")
            self.transitions += 1
            return True

        in_zone = (
            side == "LONG" and entry * 0.994 <= price <= entry * 1.002
        ) or (
            side == "SHORT" and entry * 0.998 <= price <= entry * 1.006
        )
        if in_zone:
            now_ts = time.time()
            sig["status"] = "ACTIVE"
            sig["activated_at"] = now_ts
            sig["last_checked_ts"] = now_ts
            msg_id = send_tg(_fmt_entry(sig, price), reply_markup=_order_keyboard(order_id))
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

        # 🥇 TP1
        if not sig.get("hit_tp1") and favor_hit(tp1):
            sig["hit_tp1"] = True
            sig["sl"] = entry
            sig["status"] = "BE"
            sl = entry
            pnl = ((tp1 - entry) / entry * 100) if side == "LONG" else ((entry - tp1) / entry * 100)
            send_tg(_fmt_tp(coin, side, order_id, "TP1", tp1, pnl, 1.5, wick_favor(tp1)),
                    reply_markup=kb, reply_to_message_id=reply_to)
            record_trade(coin, side, order_id, entry, tp1, "TP1", sig["score"], sig)
            self._save()
            self.transitions += 1

        # 🥈 TP2
        if not sig.get("hit_tp2") and favor_hit(tp2):
            sig["hit_tp2"] = True
            sig["sl"] = tp1
            sig["status"] = "TRAIL"
            sl = tp1
            pnl = ((tp2 - entry) / entry * 100) if side == "LONG" else ((entry - tp2) / entry * 100)
            send_tg(_fmt_tp(coin, side, order_id, "TP2", tp2, pnl, 3.0, wick_favor(tp2)),
                    reply_markup=kb, reply_to_message_id=reply_to)
            record_trade(coin, side, order_id, entry, tp2, "TP2", sig["score"], sig)
            self._save()
            self.transitions += 1

        # 🏆 TP3 → 結束
        if not sig.get("hit_tp3") and favor_hit(tp3):
            sig["hit_tp3"] = True
            pnl = ((tp3 - entry) / entry * 100) if side == "LONG" else ((entry - tp3) / entry * 100)
            send_tg(_fmt_tp(coin, side, order_id, "TP3", tp3, pnl, 5.0, wick_favor(tp3)),
                    reply_markup=kb, reply_to_message_id=reply_to)
            record_trade(coin, side, order_id, entry, tp3, "TP3", sig["score"], sig)
            self.transitions += 1
            return True

        # 🛑 SL（依狀態分類）
        if against_hit(sl):
            if sig.get("hit_tp2"):
                mode, r_value, ct = "LOCK", 1.5, "LOCK"
            elif sig.get("hit_tp1"):
                mode, r_value, ct = "BE", 0.0, "BE"
            else:
                mode, r_value, ct = "LOSS", -1.0, "SL"
            pnl = ((sl - entry) / entry * 100) if side == "LONG" else ((entry - sl) / entry * 100)
            send_tg(_fmt_sl(coin, side, order_id, sl, pnl, mode, r_value, wick_against(sl)),
                    reply_markup=kb, reply_to_message_id=reply_to)
            record_trade(coin, side, order_id, entry, sl, ct, sig["score"], sig)
            self._send_postmortem(sig, mode)
            self.transitions += 1
            return True
        return False

    def _send_postmortem(self, sig: dict, mode: str) -> None:
        coin = sig.get("instId", "?").split("-")[0]
        order_id = sig.get("order_id", "?")
        try:
            cfg = load_config()
            if not cfg.get("post_mortem", {}).get("enabled", True):
                return
            if mode == "LOCK" and cfg.get("post_mortem", {}).get("loss_only", False):
                return
            activated = sig.get("activated_at") or sig.get("created") or 0
            all_c = fetch_candles(sig["instId"], tf="15m", limit=100)
            df = [c for c in all_c if (c["ts"] / 1000) >= (activated - 900)]
            if len(df) < 10:
                send_tg(
                    f"🔍 *{coin} 覆盤*\n"
                    f"🆔 `{order_id}`\n"
                    f"進場後資料太少（{len(df)} 根 K 線），可能剛開單就被插針掃損",
                    reply_to_message_id=sig.get("entry_message_id"),
                )
                return
            reasons = analyze_loss(sig, df)
            lessons = _generate_lessons(reasons)
            similar = get_similar_stats(
                sig.get("score", 0), sig["side"],
                sig.get("detail", {}), sig.get("funding_rate"), coin,
            )
            context = _calc_postmortem_context(sig, df)
            send_tg(
                _fmt_postmortem(sig, mode, reasons, lessons, similar, context),
                reply_to_message_id=sig.get("entry_message_id"),
            )
            if mode == "LOSS":
                record_loss_reason(coin, sig["side"], reasons)
        except Exception as e:
            logging.error(f"❌ 覆盤失敗：{e}")
            try:
                send_tg(
                    f"🔍 *{coin} 覆盤錯誤*\n🆔 `{order_id}`\n例外：`{str(e)[:100]}`",
                    reply_to_message_id=sig.get("entry_message_id"),
                )
            except Exception:
                pass

    def send_position_updates(self) -> None:
        """📊 持倉更新（15 分鐘 throttle）"""
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
            send_tg(
                _fmt_position(sig, price),
                reply_markup=_order_keyboard(sig.get("order_id", "")),
                reply_to_message_id=sig.get("entry_message_id"),
            )
            cnt += 1
        if cnt:
            state["last_position_update_ts"] = now
            set_system_state(state)
            logging.info(f"📊 持倉更新 × {cnt}")

    def get_position_stats(self) -> str:
        positions = list(self.signals.values())
        if not positions:
            return "📭 *目前無持倉*"
        lines = [f"📊 *追蹤中（{len(positions)} 筆）*", "═" * 18, ""]
        for i, p in enumerate(positions):
            price = fetch_price(p["instId"]) or p["entry"]
            coin = p["instId"].split("-")[0]
            side = p["side"]
            pnl = ((price - p["entry"]) / p["entry"] * 100) if side == "LONG" else ((p["entry"] - price) / p["entry"] * 100)
            pnl_e = "🟢" if pnl >= 0 else "🔴"
            progress = (
                "🏆 TP3" if p.get("hit_tp3")
                else "🥈 TP2" if p.get("hit_tp2")
                else "🥇 TP1" if p.get("hit_tp1")
                else "⏳"
            )
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
# 18. 報表
# ═════════════════════════════════════════════════════════
def _summarize(trades: list) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0}
    win = sum(1 for t in trades if t.get("close_type") in ("TP1", "TP2", "TP3", "LOCK"))
    loss = sum(1 for t in trades if t.get("close_type") == "SL")
    be = sum(1 for t in trades if t.get("close_type") == "BE")
    pnl = sum(t.get("pnl", 0) for t in trades)
    pnls = [t.get("pnl", 0) for t in trades]
    return {
        "n": n, "win": win, "loss": loss, "be": be,
        "wr": win / n * 100, "pnl": pnl,
        "max_win": max(pnls) if pnls else 0,
        "max_loss": min(pnls) if pnls else 0,
    }


def format_daily_report(date: str | None = None) -> str:
    if date is None:
        date = tw_now().strftime("%Y-%m-%d")
    history = _load_json(TRADE_HISTORY_FILE, [])
    today = [t for t in history if t.get("date") == date]
    s = _summarize(today)
    if s["n"] == 0:
        return f"📭 *日報 {date}*\n當日尚無交易"
    lines = [
        f"📊 *日報 {date}*",
        f"━━━━━━━━━━━━━━",
        f"交易：{s['n']} 筆（勝 {s['win']} / 平 {s['be']} / 敗 {s['loss']}）",
        f"勝率：`{s['wr']:.0f}%`　PnL：`{s['pnl']:+.2f}%`",
        f"最大獲利：`{s['max_win']:+.2f}%`　最大虧損：`{s['max_loss']:+.2f}%`",
        f"",
    ]
    by_coin = {}
    for t in today:
        by_coin.setdefault(t.get("coin", "?"), []).append(t)
    if by_coin:
        lines.append("💎 *各幣種：*")
        for c, ts in sorted(by_coin.items(), key=lambda x: -sum(t.get("pnl", 0) for t in x[1])):
            sub = _summarize(ts)
            lines.append(f"  {c}: {sub['n']} 筆 / 勝率 `{sub['wr']:.0f}%` / `{sub['pnl']:+.2f}%`")
    return "\n".join(lines)


def format_monthly_report(year_month: str | None = None) -> str:
    if year_month is None:
        year_month = tw_now().strftime("%Y-%m")
    history = _load_json(TRADE_HISTORY_FILE, [])
    month = [t for t in history if t.get("date", "").startswith(year_month)]
    s = _summarize(month)
    if s["n"] == 0:
        return f"📭 *月報 {year_month}*\n本月尚無交易"
    cur, max_w, max_l, type_ = 0, 0, 0, None
    for t in month:
        ct = t.get("close_type")
        if ct in ("TP1", "TP2", "TP3", "LOCK"):
            cur = cur + 1 if type_ == "win" else 1
            type_ = "win"; max_w = max(max_w, cur)
        elif ct == "SL":
            cur = cur + 1 if type_ == "loss" else 1
            type_ = "loss"; max_l = max(max_l, cur)
    lines = [
        f"📈 *月報 {year_month}*",
        f"━━━━━━━━━━━━━━",
        f"總交易：{s['n']} 筆（勝 {s['win']} / 平 {s['be']} / 敗 {s['loss']}）",
        f"勝率：`{s['wr']:.0f}%`　PnL：`{s['pnl']:+.2f}%`",
        f"最大獲利：`{s['max_win']:+.2f}%`　最大虧損：`{s['max_loss']:+.2f}%`",
        f"🔥 最大連勝：{max_w}　❄️ 最大連敗：{max_l}",
        f"",
    ]
    by_coin = {}
    for t in month:
        by_coin.setdefault(t.get("coin", "?"), []).append(t)
    if by_coin:
        lines.append("💎 *各幣種：*")
        for c, ts in sorted(by_coin.items(), key=lambda x: -sum(t.get("pnl", 0) for t in x[1])):
            sub = _summarize(ts)
            lines.append(f"  {c}: {sub['n']} 筆 / 勝率 `{sub['wr']:.0f}%` / `{sub['pnl']:+.2f}%`")
    return "\n".join(lines)


def format_learning_report() -> str:
    state = _load_json(LEARNING_FILE, {})
    buckets = state.get("buckets", {})
    by_coin = state.get("by_coin", {})
    if not buckets and not by_coin:
        return "🧠 *學習狀態*\n📭 尚未累積資料（需 5+ 筆已結束交易）"
    lines = ["🧠 *機器人學習狀態*", "━━━━━━━━━━━━━━", ""]
    if by_coin:
        lines.append("📊 *各幣種戰績：*")
        for c, d in sorted(by_coin.items(), key=lambda x: -x[1].get("total", 0))[:12]:
            n, w, l, be = d.get("total", 0), d.get("win", 0), d.get("loss", 0), d.get("be", 0)
            wr = w / n * 100 if n else 0
            lines.append(f"  {c}: {n} 筆（勝 {w} / 平 {be} / 敗 {l}，勝率 `{wr:.0f}%`）")
        lines.append("")
    high = [(b, d) for b, d in buckets.items() if d.get("total", 0) >= 5 and d["win"] / d["total"] > 0.6]
    low = [(b, d) for b, d in buckets.items() if d.get("total", 0) >= 5 and d["win"] / d["total"] < 0.4]
    if high:
        lines.append("✅ *高勝率組合：*")
        for b, d in sorted(high, key=lambda x: -x[1]["win"] / x[1]["total"])[:5]:
            lines.append(f"  `{b}` → {d['total']} 筆，勝率 `{d['win'] / d['total'] * 100:.0f}%`")
        lines.append("")
    if low:
        lines.append("⚠️ *低勝率組合：*")
        for b, d in sorted(low, key=lambda x: x[1]["win"] / x[1]["total"])[:5]:
            lines.append(f"  `{b}` → {d['total']} 筆，勝率 `{d['win'] / d['total'] * 100:.0f}%`")
        lines.append("")
    lines.append("💡 _累積資料越多，KNN 評分調整越精準_")
    return "\n".join(lines)


def format_audit_report() -> str:
    """🔬 指標有效性審查"""
    history = _load_json(TRADE_HISTORY_FILE, [])
    closed = [t for t in history if t.get("close_type") in ("SL", "BE", "LOCK", "TP1", "TP2", "TP3")]
    n_closed = len(closed)
    if n_closed < 10:
        return f"📭 *指標有效性審查*\n資料不足（{n_closed} 筆 < 10 不足以審查）"

    def _stats(trades):
        n = len(trades)
        if n == 0:
            return None
        wins = sum(1 for t in trades if t["close_type"] in ("TP1", "TP2", "TP3", "LOCK"))
        return {"n": n, "wr": wins / n * 100}

    def _v(diff):
        return "✅" if diff > 10 else "⚠️" if diff > 0 else "❌"

    overall = _stats(closed)
    lines = [
        f"🔬 *指標有效性審查*",
        f"━━━━━━━━━━━━━━",
        f"樣本：{n_closed} 筆",
        f"整體勝率：`{overall['wr']:.0f}%`",
        f"",
    ]

    secs = []
    # 高分 vs 低分
    high_s = [t for t in closed if t.get("score", 0) >= 85]
    low_s = [t for t in closed if 70 <= t.get("score", 0) < 85]
    if len(high_s) >= 3 and len(low_s) >= 5:
        h, l = _stats(high_s), _stats(low_s)
        diff = h["wr"] - l["wr"]
        secs.append(
            f"{_v(diff)} *高分（85+）vs 一般（70-84）*\n"
            f"  高分：{h['n']} 筆 / `{h['wr']:.0f}%`\n"
            f"  一般：{l['n']} 筆 / `{l['wr']:.0f}%`\n"
            f"  差異：`{diff:+.0f}%`"
        )

    # MTF
    mtf_a = [t for t in closed if (t.get("features") or {}).get("mtf_h1", 0) == 1.0]
    mtf_o = [t for t in closed if (t.get("features") or {}).get("mtf_h1", 1.0) == 0.0]
    if len(mtf_a) >= 3 and len(mtf_o) >= 3:
        a, o = _stats(mtf_a), _stats(mtf_o)
        diff = a["wr"] - o["wr"]
        secs.append(
            f"{_v(diff)} *MTF 1H 順勢 vs 中性*\n"
            f"  順勢：{a['n']} 筆 / `{a['wr']:.0f}%`\n"
            f"  中性：{o['n']} 筆 / `{o['wr']:.0f}%`\n"
            f"  差異：`{diff:+.0f}%`"
        )

    # 量能
    high_v = [t for t in closed if (t.get("features") or {}).get("vol_ratio", 1.0) >= 1.5]
    low_v = [t for t in closed if (t.get("features") or {}).get("vol_ratio", 1.0) < 1.0]
    if len(high_v) >= 3 and len(low_v) >= 3:
        h, l = _stats(high_v), _stats(low_v)
        diff = h["wr"] - l["wr"]
        secs.append(
            f"{_v(diff)} *高量能 vs 低量能*\n"
            f"  高量：{h['n']} 筆 / `{h['wr']:.0f}%`\n"
            f"  低量：{l['n']} 筆 / `{l['wr']:.0f}%`\n"
            f"  差異：`{diff:+.0f}%`"
        )

    # 多空方向
    longs = [t for t in closed if t.get("side") == "LONG"]
    shorts = [t for t in closed if t.get("side") == "SHORT"]
    if longs and shorts:
        l, s = _stats(longs), _stats(shorts)
        diff = l["wr"] - s["wr"]
        be = "✅" if abs(diff) < 10 else "⚠️" if abs(diff) < 20 else "❌"
        secs.append(
            f"{be} *方向平衡*\n"
            f"  LONG：{l['n']} 筆 / `{l['wr']:.0f}%`\n"
            f"  SHORT：{s['n']} 筆 / `{s['wr']:.0f}%`\n"
            f"  差異：`{diff:+.0f}%`"
        )

    if secs:
        lines.append("\n\n".join(secs))
        lines.append("")
    lines.append("💡 ✅=有效（差>10%）　⚠️=邊際　❌=反向（建議降權）")
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════
# 19. 主掃描 + 監控
# ═════════════════════════════════════════════════════════
def run_scan(tracker: SignalTracker) -> int:
    cfg = load_config()
    coins = cfg.get("coins", ALL_COINS)
    max_signals = cfg.get("max_signals_per_scan", 2)
    score_thr = cfg.get("score_threshold", 75)
    cooldown_h = cfg.get("cooldown_hours", 3)
    expire_h = cfg.get("signal_expire_hours", 24)
    atr_max = cfg.get("atr_max_pct", 0.04)
    pv_cfg = cfg.get("price_verification", {})
    pv_enabled = pv_cfg.get("enabled", True)
    pv_max_dev = pv_cfg.get("max_deviation_pct", 0.5)
    pv_block = pv_cfg.get("block_on_unverified", False)

    logging.info("🚀 開始掃描...")

    # 健康檢查
    unhealthy, h_msg = check_health()
    if unhealthy:
        send_tg(h_msg)

    # 風控（依序）
    cb_active, cb_msg = check_circuit_breaker(cfg)
    if cb_active:
        logging.warning(f"🔥 {cb_msg}")
        tracker.check_all()
        tracker.send_position_updates()
        return 0

    blocked, b_reason = is_blackout_time(cfg)
    if blocked:
        logging.info(f"🕒 {b_reason}")
        tracker.check_all()
        tracker.send_position_updates()
        return 0

    in_news, news = is_in_news_window(cfg)
    if in_news:
        logging.info(f"📰 {news}")
        tracker.check_all()
        tracker.send_position_updates()
        return 0

    limit_hit, limit_msg = check_daily_limits(cfg, tracker)
    if limit_hit:
        logging.info(f"🛡️ {limit_msg}")
        tracker.check_all()
        tracker.send_position_updates()
        return 0

    # 早期退出
    eligible = [
        c for c in coins
        if not tracker.has_open_position(c) and not is_cooling(c, cooldown_h)
    ]
    if not eligible:
        logging.info(f"📭 全部 {len(coins)} 幣都在冷卻 / 持倉，僅跑監控")
        tracker.check_all()
        tracker.send_position_updates()
        return 0

    sent = 0
    logging.info(f"🎯 可掃：{[c.split('-')[0] for c in eligible]}")
    for instId in eligible:
        if sent >= max_signals:
            break
        try:
            okx_price = fetch_price(instId)
            if okx_price <= 0:
                continue

            # 雙來源價格驗證
            if pv_enabled:
                ok, tv_price, diff = verify_price(instId, okx_price, pv_max_dev, pv_block)
                if not ok:
                    if tv_price is not None:
                        send_tg(
                            f"⚠️ *{instId.split('-')[0]} 價格異常*\n"
                            f"OKX `{okx_price:.4f}` vs TV `{tv_price:.4f}`\n"
                            f"偏離 `{diff:.3f}%` > {pv_max_dev}%"
                        )
                    continue

            df = fetch_candles(instId, tf="15m", limit=100, include_unconfirmed=False)
            if not df:
                continue

            funding = fetch_funding_rate(instId)
            sig = generate_signal(
                instId, df, okx_price, funding,
                score_threshold=score_thr,
                atr_max_pct=atr_max,
                signal_expire_hours=expire_h,
            )
            if not sig:
                continue

            in_zone = (
                sig["side"] == "LONG"
                and sig["entry"] * 0.994 <= okx_price <= sig["entry"] * 1.002
            ) or (
                sig["side"] == "SHORT"
                and sig["entry"] * 0.998 <= okx_price <= sig["entry"] * 1.006
            )

            key, order_id = tracker.add(sig, active=in_zone)

            if in_zone:
                signal_with_id = dict(sig)
                signal_with_id["order_id"] = order_id
                msg_id = send_tg(
                    _fmt_entry(signal_with_id, okx_price),
                    reply_markup=_order_keyboard(order_id),
                )
                tracker.set_entry_message_id(key, msg_id)
                logging.info(f"✅ {instId} 進場通知已送（{order_id}）")
            else:
                send_tg(
                    f"📍 *{instId.split('-')[0]} 訊號就位*\n"
                    f"🆔 `{order_id}`\n"
                    f"進場 `{sig['entry']:.4f}`（當前 `{okx_price:.4f}`）\n"
                    f"等價格進入區間自動觸發",
                    reply_markup=_order_keyboard(order_id),
                )
                logging.info(f"📍 {instId} PENDING")

            mark_cooldown(instId, cooldown_h)
            sent += 1
        except Exception as e:
            logging.error(f"[{instId}] 失敗：{e}")
            continue

    tracker.check_all()
    tracker.send_position_updates()
    logging.info(f"✅ 掃描完成，本輪 {sent} 筆")
    return sent


def run_monitor(tracker: SignalTracker) -> None:
    if not tracker.signals:
        return
    logging.info(f"🔔 monitor：追蹤 {len(tracker.signals)} 筆")
    tracker.check_all()
    tracker.send_position_updates()


# ═════════════════════════════════════════════════════════
# 20. 主入口
# ═════════════════════════════════════════════════════════
def main() -> None:
    try:
        logging.info("=" * 50)
        logging.info("🤖 Alpha Oracle Pro v15.1 專業精緻版")
        logging.info(f"⏰ {tw_ts()}")
        logging.info("=" * 50)

        tracker = SignalTracker(ACTIVE_SIGNALS_FILE)

        if len(sys.argv) > 1:
            cmd = sys.argv[1]
            if cmd in ("/stats", "/持倉", "stats"):
                send_tg(tracker.get_position_stats()); return
            if cmd in ("/daily", "/日報", "daily"):
                date = sys.argv[2] if len(sys.argv) > 2 else None
                send_tg(format_daily_report(date)); return
            if cmd in ("/monthly", "/月報", "monthly"):
                ym = sys.argv[2] if len(sys.argv) > 2 else None
                send_tg(format_monthly_report(ym)); return
            if cmd in ("/learning", "/學習", "learning"):
                send_tg(format_learning_report()); return
            if cmd in ("/audit", "/審查", "audit"):
                send_tg(format_audit_report()); return
            if cmd in ("monitor", "/monitor", "/監控"):
                run_monitor(tracker); return

        run_scan(tracker)
        logging.info("🎉 完成")
    except Exception as e:
        logging.error(f"🔥 系統錯誤：{e}")
        try:
            s = get_system_state()
            s["scan_failure_count"] = s.get("scan_failure_count", 0) + 1
            set_system_state(s)
        except Exception:
            pass
        import traceback; traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
