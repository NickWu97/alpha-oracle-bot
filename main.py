#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro v15.3 — 頂級交易員模式（修正版，繁體中文）
══════════════════════════════════════════════════════════════════════
✨ v15.3 對比 v15.2 修正的問題：
   🔴 #1 record_trade 一筆單被記 2~4 次 → 每張單只記一次最終結算
   🔴 #2 hard_filters 預設值在 code 跟 config 不一致 → 統一從 cfg 取
   🔴 #3 min_rr_ratio 是死碼 → 改成可設定 tp_r_ratios 並真實檢查
   🔴 #4 ACTIVE 持倉沒最大持有時長 → 新增 max_hold_hours 自動平倉
   🟡 #5 同根 K 線 TP/SL 樂觀順序 → 改成 SL 優先（保守）
   🟡 #6 日 PnL 紅線單位混亂 → 改用 USD（對齊資金管理）
   🟡 #7 NFP/CPI 寫死台灣時間 → 改用 US/Eastern 即時換算（含 DST）
   🟡 #8 KNN side 權重過大 → 預先按 side 過濾再找鄰居
   🟡 #9 學習桶查詢用調整後分數 → 改用原始 raw score（避免自我強化）
   🟢 #10 沒檔案鎖 → 加 fcntl 全域鎖
   🟢 #11 OKX 請求沒重試 → 加 session + 指數 backoff
   🟢 #12 進場容差寫死 → 移到 config entry_zone_pct
   🟢 #15 OKX 沒共用 session → 用 Session + User-Agent
   🟢 #16 啟動時環境變數沒驗證 → 缺 token 直接 exit
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

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
    ET_TZ = ZoneInfo("America/New_York")
except Exception:
    ET_TZ = None  # 沒 zoneinfo 就退化成只看台灣時間

try:
    import fcntl  # Unix only
    _HAS_FCNTL = True
except Exception:
    _HAS_FCNTL = False

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
    "SUI-USDT-SWAP",
]

ACTIVE_SIGNALS_FILE = "active_signals.json"
TRADE_HISTORY_FILE = "trade_history.json"
SIGNAL_COOLDOWN_FILE = "signal_cooldown.json"
SYSTEM_STATE_FILE = "system_state.json"
LEARNING_FILE = "learning_state.json"
CONFIG_FILE = "config.json"
LOCK_FILE = ".alpha_oracle.lock"

DEFAULT_CONFIG: dict = {
    "coins": ALL_COINS,
    "max_signals_per_scan": 2,
    "score_threshold": 72,
    "cooldown_hours": 2,
    "signal_expire_hours": 24,
    "max_hold_hours": 48,                # #4 ACTIVE 最大持倉
    "atr_max_pct": 0.035,
    # #3 真正可設定的 R 倍數
    "tp_r_ratios": [1.5, 3.0, 5.0],
    "sl_atr_mult": 1.5,
    "min_rr_ratio": 1.5,
    "show_score_breakdown": False,

    # #12 進場容差
    "entry_zone_pct": {
        "long_favor": 0.006,   # 多單往下容忍 0.6%
        "long_against": 0.002, # 多單往上容忍 0.2%
        "short_favor": 0.006,  # 空單往上容忍 0.6%
        "short_against": 0.002,
    },

    # 🛡️ Hard filter
    "hard_filters": {
        "require_mtf_h4_align": True,
        "min_volume_ratio": 0.7,
        "min_adx": 18,
    },

    "capital_management": {
        "capital_per_trade_usd": 100,
        "max_loss_usd": 20,
        "total_capital_usd": 1000,       # #6 日紅線基數
        "max_leverage": 50,
        "min_leverage": 2,
    },

    # 🛡️ 兩條紅線
    "daily_limits": {
        "max_concurrent_positions": 2,
        "daily_loss_limit_usd": 50,      # #6 改成 USD
        "daily_loss_limit_pct": 5.0,     # 保留 fallback
    },

    "circuit_breaker": {
        "loss_threshold": 3,
        "pause_hours": 24,
    },

    "price_verification": {
        "enabled": True,
        "max_deviation_pct": 0.5,
        "block_on_unverified": False,
    },

    "learning": {
        "enabled": True,
        "knn_enabled": True,
        "min_samples": 5,
        "max_score_adjust": 10,
    },

    "post_mortem": {
        "enabled": True,
        "loss_only": False,
    },

    # 🕒 風險時段（純台灣時間）
    "blackout_windows_tw": [
        {"start": "07:50", "end": "08:10", "reason": "資金費率結算（00 UTC）"},
        {"start": "15:50", "end": "16:10", "reason": "資金費率結算（08 UTC）"},
        {"start": "23:50", "end": "00:10", "reason": "資金費率結算（16 UTC）"},
        {"start": "02:00", "end": "02:30", "reason": "FOMC 公布時段"},
    ],

    # #7 改用 ET 即時換算
    "auto_news_blackout": {
        "nfp": True,
        "cpi": True,
        "et_window_min_before": 35,  # 8:30 ET 前 35 分
        "et_window_min_after": 60,   # 8:30 ET 後 60 分
    },

    "news_blackouts": [],

    # #5 同根 K 線 TP/SL 衝突時：True=SL 優先（保守）
    "conservative_sl_first": True,
}

_price_cache: dict = {}
_candle_cache: dict = {}
_mtf_cache: dict = {}
_tv_cache: dict = {}
_funding_cache: dict = {}

# #11 共用 session + UA
_session = requests.Session()
_session.headers.update({"User-Agent": "alpha-oracle-pro/15.3"})


# ═════════════════════════════════════════════════════════
# 2. 持久化 + 配置 + 檔案鎖
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


# #10 全域檔案鎖（避免兩個 cron 實例同時跑踩 JSON）
class GlobalLock:
    def __init__(self, path: str):
        self.path = path
        self.fp = None

    def __enter__(self):
        if not _HAS_FCNTL:
            return self
        try:
            self.fp = open(self.path, "w")
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fp.write(f"{os.getpid()} {tw_ts()}\n")
            self.fp.flush()
            return self
        except BlockingIOError:
            logging.warning(f"⚠️ 已有實例執行中，本次跳過（鎖：{self.path}）")
            sys.exit(0)

    def __exit__(self, *args):
        if self.fp is not None:
            try:
                fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
                self.fp.close()
            except Exception:
                pass


# ═════════════════════════════════════════════════════════
# 3. Telegram 通知（含重試）
# ═════════════════════════════════════════════════════════
def send_tg(msg: str, parse_mode: str = "Markdown",
            reply_markup: dict | None = None,
            reply_to_message_id: int | None = None,
            max_retries: int = 3) -> int | None:
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
            r = _session.post(
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
# 4. OKX 數據抓取（#11 含重試 + session）
# ═════════════════════════════════════════════════════════
def _okx_get(url: str, timeout: float = 6) -> dict | None:
    for attempt in range(3):
        try:
            r = _session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = min(2 ** attempt, 8)
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            return None
        except requests.RequestException as e:
            logging.debug(f"OKX 請求失敗（第 {attempt + 1} 次）：{e}")
            time.sleep(2 ** attempt)
    return None


def fetch_price(instId: str) -> float:
    now = time.time()
    if instId in _price_cache:
        price, t = _price_cache[instId]
        if now - t < 5:
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


def fetch_candles(instId: str, tf: str = "15m", limit: int = 100,
                  include_unconfirmed: bool = True) -> list:
    cache_key = f"{instId}_{tf}_{limit}_{include_unconfirmed}"
    now = time.time()
    if cache_key in _candle_cache:
        candles, t = _candle_cache[cache_key]
        if now - t < 30:
            return candles
    data = _okx_get(
        f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}"
    )
    if not data or data.get("code") != "0":
        return _candle_cache.get(cache_key, ([], 0))[0]
    candles = []
    for row in data.get("data", []):
        try:
            confirmed = row[8] == "1"
            if not include_unconfirmed and not confirmed:
                continue
            candles.append({
                "ts": int(row[0]), "o": float(row[1]), "h": float(row[2]),
                "l": float(row[3]), "c": float(row[4]), "v": float(row[5]),
                "confirmed": confirmed,
            })
        except Exception:
            continue
    candles.sort(key=lambda x: x["ts"])
    _candle_cache[cache_key] = (candles, now)
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


def fetch_mtf_trend(instId: str) -> dict:
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
    """簡化版趨勢判斷（不是真 Supertrend，純粹 close vs 20MA ± 0.5ATR）"""
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
    return min(c["l"] for c in seg), max(c["h"] for c in seg)


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
    pl = min(c["l"] for c in seg)
    ph = max(c["h"] for c in seg)
    mid = (pl + ph) / 2
    if side == "LONG":
        return last["l"] < pl and last["c"] > mid
    return last["h"] > ph and last["c"] < mid


def calc_momentum_ratio(df: list, side: str, n: int = 5) -> bool:
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
    desc = (f"1H={'順' if h1 == expect else '反' if h1 == -expect else '中'} / "
            f"4H={'順' if h4 == expect else '反' if h4 == -expect else '中'}")
    return s, desc


# ═════════════════════════════════════════════════════════
# 8. 評分系統
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
    detail["vol_ratio"] = vol_r

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
# #8 side 從特徵中拿掉，改成預先過濾
_FEATURE_SCALE = {
    "score": 30, "rsi": 50, "atr_pct": 3, "funding": 2,
    "vol_ratio": 3, "mtf_h1": 1, "mtf_h4": 1,
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


def find_similar_trades(features: dict, side: str, history: list, k: int = 10) -> list:
    """#8 先按 side 過濾，再做 KNN"""
    candidates = []
    for t in history:
        f = t.get("features")
        if not f:
            continue
        # 必須同方向才可比較
        if t.get("side") != side:
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
    cfg = load_config()
    if not cfg.get("learning", {}).get("knn_enabled", True):
        return score, []
    history = _load_json(TRADE_HISTORY_FILE, [])
    if len(history) < 10:
        return score, []
    feat = vectorize_signal(score, side, detail, funding_rate, mtf)
    similar = find_similar_trades(feat, side, history, k=10)
    if len(similar) < 3:
        return score, []
    wins = sum(1 for t in similar if t.get("close_type") in ("TP3", "LOCK"))
    n = len(similar)
    wr = wins / n
    notes = [f"🧬 KNN：{n} 筆最相似（同向） → 勝 {wins} (勝率 {wr:.0%})"]
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

    # #9 學習桶用 raw_score（進場時的原始分數，沒被學習調整過）
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


def apply_learning_adjustment(raw_score: int, side: str, detail: dict,
                              funding_rate, coin: str) -> tuple[int, list]:
    """#9 用 raw_score（未調整分數）查桶，避免自我強化"""
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
# 10. 訊號生成
# ═════════════════════════════════════════════════════════
def generate_signal(instId: str, df: list, current_price: float,
                    funding_rate: float | None,
                    cfg: dict) -> dict | None:
    """#2 #3 全部從 cfg 取參數，無寫死預設值"""
    if df is None or len(df) < 50:
        return None
    atr = calc_atr(df)
    atr_pct = atr / current_price * 100
    if atr / current_price > cfg.get("atr_max_pct", 0.035):
        return None

    coin = instId.split("-")[0]
    mtf = fetch_mtf_trend(instId)

    # 🛡️ Hard filter（#2 統一從 cfg 取，預設值來自 DEFAULT_CONFIG）
    hf = cfg.get("hard_filters", {})

    min_adx = hf.get("min_adx", DEFAULT_CONFIG["hard_filters"]["min_adx"])
    if min_adx > 0:
        adx = calc_adx(df)
        if adx < min_adx:
            return None

    min_vol = hf.get("min_volume_ratio", DEFAULT_CONFIG["hard_filters"]["min_volume_ratio"])
    if min_vol > 0:
        vol_ratio, _ = calc_volume_quality(df)
        if vol_ratio < min_vol:
            return None

    # #3 TP 倍數從 config 取
    tp_r = cfg.get("tp_r_ratios", [1.5, 3.0, 5.0])
    if len(tp_r) != 3:
        tp_r = [1.5, 3.0, 5.0]
    sl_mult = cfg.get("sl_atr_mult", 1.5)
    min_rr = cfg.get("min_rr_ratio", 1.5)
    score_thr = cfg.get("score_threshold", 72)
    expire_h = cfg.get("signal_expire_hours", 24)

    candidates = []
    for side in ("LONG", "SHORT"):
        # H4 順向過濾
        if hf.get("require_mtf_h4_align", True):
            expect = 1 if side == "LONG" else -1
            h4 = mtf.get("4H", {}).get("supertrend", 0)
            if h4 == -expect:
                continue

        raw_score, _, detail = calc_score(df, side, current_price, mtf=mtf)
        detail["atr_pct"] = round(atr_pct, 3)

        # #9 學習雙路：KNN + 桶都基於 raw_score
        adj_score, knn_notes = apply_knn_learning(raw_score, side, detail, funding_rate, mtf)
        bucket_adj, bucket_notes = apply_learning_adjustment(
            raw_score, side, detail, funding_rate, coin
        )
        # 兩條調整加總
        score = adj_score + (bucket_adj - raw_score)

        if knn_notes or bucket_notes:
            detail["learning_notes"] = knn_notes + bucket_notes
            detail["learning_adjust"] = score - raw_score

        if score < score_thr:
            continue

        # 進場 + SL + TP（用 ATR 距離）
        entry = current_price
        sl_dist = atr * sl_mult
        if side == "LONG":
            sl = entry - sl_dist
            tp1 = entry + sl_dist * tp_r[0]
            tp2 = entry + sl_dist * tp_r[1]
            tp3 = entry + sl_dist * tp_r[2]
        else:
            sl = entry + sl_dist
            tp1 = entry - sl_dist * tp_r[0]
            tp2 = entry - sl_dist * tp_r[1]
            tp3 = entry - sl_dist * tp_r[2]

        # #3 真正的 R:R 檢查
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        actual_tp1_r = abs(tp1 - entry) / risk
        if actual_tp1_r < min_rr - 0.02:
            continue

        # TP 順序保險：tp3 必須最遠
        if side == "LONG":
            if not (entry < tp1 < tp2 < tp3):
                continue
        else:
            if not (entry > tp1 > tp2 > tp3):
                continue

        ob_zone = find_order_block(df, side)

        grade = (
            "🔥 A+ 極強" if score >= 90
            else "⭐ A 強力" if score >= 80
            else "✅ B+ 合格"
        )

        candidates.append({
            "instId": instId,
            "side": side,
            "tf": "15m",
            "entry": round(entry, 6),
            "sl": round(sl, 6),
            "tp1": round(tp1, 6),
            "tp2": round(tp2, 6),
            "tp3": round(tp3, 6),
            "score": int(score),
            "raw_score": int(raw_score),  # #9 保存原始分數
            "grade": grade,
            "detail": detail,
            "funding_rate": funding_rate,
            "mtf_snapshot": mtf,
            "ob_zone": ob_zone,
            "tp_r_ratios": tp_r,
            "created": time.time(),
            "expires": time.time() + expire_h * 3600,
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


def calc_realized_usd(sig: dict, close_type: str) -> float:
    """⚖️ 依出場類型計算 USD 損益（⅓ 分批假設）

    每張單 1/3 + 1/3 + 1/3 分批：
    - TP3: 1/3 × TP1 + 1/3 × TP2 + 1/3 × TP3
    - LOCK (TP2 命中後 SL 在 TP1): 1/3 × TP1 + 1/3 × TP2 + 1/3 × TP1
    - BE (TP1 命中後 SL 在 entry): 1/3 × TP1 + 2/3 × entry
    - SL (沒到任何 TP): 整倉 × SL
    """
    sizing = calc_position_sizing(
        sig["entry"], sig["sl_original"], sig["tp1"], sig["tp2"], sig["tp3"],
        sig["side"],
    )
    if not sizing:
        return 0.0
    tp1_p = sizing["tp1_profit"]
    tp2_p = sizing["tp2_profit"]
    tp3_p = sizing["tp3_profit"]
    sl_l = -sizing["sl_loss"]
    if close_type == "TP3":
        return round((tp1_p + tp2_p + tp3_p) / 3, 2)
    if close_type == "LOCK":
        return round((tp1_p + tp2_p + tp1_p) / 3, 2)
    if close_type == "BE":
        return round(tp1_p / 3, 2)  # 後 2/3 在 entry 平 = 0
    return round(sl_l, 2)


def calc_realized_r(close_type: str, tp_r: list) -> float:
    """同樣的 ⅓ 假設，回傳 R 倍數"""
    if not tp_r or len(tp_r) != 3:
        tp_r = [1.5, 3.0, 5.0]
    if close_type == "TP3":
        return round((tp_r[0] + tp_r[1] + tp_r[2]) / 3, 2)
    if close_type == "LOCK":
        return round((tp_r[0] + tp_r[1] + tp_r[0]) / 3, 2)
    if close_type == "BE":
        return round(tp_r[0] / 3, 2)
    return -1.0


# ═════════════════════════════════════════════════════════
# 12. 通知格式
# ═════════════════════════════════════════════════════════
def _fmt_entry(sig: dict, current_price: float) -> str:
    coin = sig["instId"].split("-")[0]
    side = sig["side"]
    entry, sl = sig["entry"], sig["sl"]
    tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
    score = sig["score"]
    direction = "做多" if side == "LONG" else "做空"
    emoji = "🟢" if side == "LONG" else "🔴"
    grade = "🔥 A+ 極強" if score >= 90 else "⭐ A 強力" if score >= 80 else "✅ B+ 合格"
    tp_r = sig.get("tp_r_ratios", [1.5, 3.0, 5.0])
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
        f"🥇 TP1：`{tp1:.4f}`  (距 +{tp1_pct:.2f}% / {tp_r[0]}R)\n"
        f"🥈 TP2：`{tp2:.4f}`  (距 +{tp2_pct:.2f}% / {tp_r[1]}R)\n"
        f"🏆 TP3：`{tp3:.4f}`  (距 +{tp3_pct:.2f}% / {tp_r[2]}R)\n"
        f"{sizing_block}"
    )


def _fmt_tp_milestone(coin: str, order_id: str, tp_level: str, price: float,
                     pnl_pct: float, r_mult: float, wick: bool, next_action: str) -> str:
    w = " 🪡" if wick else ""
    return (
        f"🎯 *{coin} {tp_level}* `{pnl_pct:+.2f}%` (`{r_mult:.1f}R`){w}\n"
        f"🆔 `{order_id[-8:]}` · {next_action} · {tw_now().strftime('%H:%M')}"
    )


def _fmt_final_close(coin: str, order_id: str, close_type: str,
                     price: float, pnl_pct: float, pnl_usd: float,
                     realized_r: float, wick: bool) -> str:
    """🏁 最終結算通知（每筆單只送一次）"""
    w = " 🪡" if wick else ""
    if close_type == "TP3":
        return (
            f"🏆 *{coin} TP3 全部達標* `{pnl_pct:+.2f}%` (`{realized_r:.1f}R` / `+${pnl_usd:.0f}`){w}\n"
            f"🆔 `{order_id[-8:]}` · {tw_now().strftime('%H:%M')}"
        )
    if close_type == "LOCK":
        return (
            f"🔐 *{coin} 鎖利出場* `{pnl_pct:+.2f}%` (`{realized_r:.1f}R` / `+${pnl_usd:.0f}`){w}\n"
            f"🆔 `{order_id[-8:]}` · {tw_now().strftime('%H:%M')}"
        )
    if close_type == "BE":
        sign = "+" if pnl_usd >= 0 else ""
        return (
            f"🔒 *{coin} 保本出場* `{pnl_pct:+.2f}%` (`{realized_r:.1f}R` / `{sign}${pnl_usd:.0f}`){w}\n"
            f"🆔 `{order_id[-8:]}` · {tw_now().strftime('%H:%M')}"
        )
    # SL
    return (
        f"❌ *{coin} 止損* `{pnl_pct:+.2f}%` (`{realized_r:.1f}R` / `-${abs(pnl_usd):.0f}`){w}\n"
        f"🆔 `{order_id[-8:]}` · {tw_now().strftime('%H:%M')}"
    )


def _fmt_position(sig: dict, current_price: float) -> str:
    coin = sig["instId"].split("-")[0]
    side = sig["side"]
    direction = "做多" if side == "LONG" else "做空"
    entry = sig["entry"]
    sl = sig["sl"]
    tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
    pnl = ((current_price - entry) / entry * 100) if side == "LONG" else ((entry - current_price) / entry * 100)
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"
    sl_dist = abs(current_price - sl) / current_price * 100
    tp1_dist = abs(tp1 - current_price) / current_price * 100
    tp2_dist = abs(tp2 - current_price) / current_price * 100
    tp3_dist = abs(tp3 - current_price) / current_price * 100
    if sig.get("hit_tp3"):
        progress = "🏆 全部達標 ✅"
    elif sig.get("hit_tp2"):
        progress = f"⏳ 等待 TP3 (還差 +{tp3_dist:.2f}%)"
    elif sig.get("hit_tp1"):
        progress = f"⏳ 等待 TP2 (還差 +{tp2_dist:.2f}%)"
    else:
        progress = f"⏳ 等待 TP1 (還差 +{tp1_dist:.2f}%)"
    return (
        f"📊 *{coin} 持倉更新*\n"
        f"━━━━━━━━━━━━━━\n"
        f"🆔 `{sig.get('order_id', 'N/A')}`\n"
        f"方向：{direction}\n"
        f"💰 當前：`{current_price:.4f}`  {pnl_emoji} {pnl:+.2f}%\n"
        f"🛑 SL ：`{sl:.4f}`  (距 +{sl_dist:.2f}%)\n"
        f"🥇 TP1：`{tp1:.4f}`  (距 +{tp1_dist:.2f}%){' ✅' if sig.get('hit_tp1') else ''}\n"
        f"🥈 TP2：`{tp2:.4f}`  (距 +{tp2_dist:.2f}%){' ✅' if sig.get('hit_tp2') else ''}\n"
        f"🏆 TP3：`{tp3:.4f}`  (距 +{tp3_dist:.2f}%){' ✅' if sig.get('hit_tp3') else ''}\n"
        f"進度：{progress}"
    )


# ═════════════════════════════════════════════════════════
# 13. 覆盤分析
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
        reasons.append({
            "code": "TREND_REVERSAL", "title": "🔄 趨勢反轉",
            "detail": "進場時 Supertrend 順勢，止損前已翻向反向",
            "severity": 30,
        })

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

    sweep_dir = "SHORT" if side == "LONG" else "LONG"
    if detect_liquidity_sweep(df_now[-12:], sweep_dir):
        reasons.append({
            "code": "LIQ_SWEEP", "title": "🌊 流動性掃蕩",
            "detail": "止損前出現反向假突破插針後快速收回",
            "severity": 22,
        })

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

    atr_then, atr_now = calc_atr(df_then), calc_atr(df_now)
    if atr_then > 0 and atr_now / atr_then > 1.5:
        reasons.append({
            "code": "VOL_SPIKE", "title": "🌪 波動率激增",
            "detail": f"ATR 從 {atr_then:.4f} 擴張至 {atr_now:.4f}"
                      f"（{(atr_now / atr_then - 1) * 100:.0f}%）",
            "severity": 18,
        })

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
        "rsi_then": rsi_then, "rsi_now": rsi_now,
        "rsi_change": rsi_now - rsi_then,
        "atr_pct_then": atr_pct_then, "atr_pct_now": atr_pct_now,
        "atr_change_pct": (atr_now / atr_then - 1) * 100 if atr_then > 0 else 0,
        "st_then": st_then, "st_now": st_now,
        "adx_now": adx_now, "vol_change": vol_change,
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


def get_similar_stats(score: int, side: str, detail: dict,
                      funding_rate, coin: str) -> tuple:
    state = _load_json(LEARNING_FILE, {})
    bd = state.get("buckets", {}).get(f"coin_side:{coin}_{side}", {})
    n = bd.get("total", 0)
    return (n, bd.get("win", 0), bd.get("loss", 0), bd.get("be", 0))


def _fmt_postmortem(sig: dict, close_type: str, reasons: list, lessons: list,
                    similar: tuple | None = None,
                    context: dict | None = None) -> str:
    coin = sig["instId"].split("-")[0]
    order_id = sig.get("order_id", "?")
    side = sig["side"]
    direction = "多" if side == "LONG" else "空"
    score = sig.get("score", 0)

    sizing = calc_position_sizing(
        sig["entry"], sig["sl_original"], sig["tp1"], sig["tp2"], sig["tp3"], side
    )
    tp_r = sig.get("tp_r_ratios", [1.5, 3.0, 5.0])
    realized_r = calc_realized_r(close_type, tp_r)
    realized_usd = calc_realized_usd(sig, close_type)

    if close_type == "BE":
        result_emoji, result_text = "🔒", "保本"
    elif close_type == "LOCK":
        result_emoji, result_text = "🔐", "鎖利"
    elif close_type == "TP3":
        result_emoji, result_text = "🏆", "全勝"
    else:
        result_emoji, result_text = "❌", "止損"

    sign = "+" if realized_usd >= 0 else ""
    result_r = f"{realized_r:+.1f}R"
    result_usd = f"{sign}${realized_usd:.0f}"

    duration = ""
    if context and context.get("duration_min", 0) > 0:
        d = context["duration_min"]
        duration = f"{d / 60:.1f}h" if d > 60 else f"{d:.0f}m"

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
        f"🎯 *出場主因（依嚴重度）：*",
    ]
    for i, r in enumerate(reasons[:3], 1):
        lines.append(f"{i}. {r['title']}")
        if r.get("detail"):
            lines.append(f"   _{r['detail']}_")

    if context:
        ctx_lines = []
        if context.get("rsi_then") is not None and abs(context.get("rsi_change", 0)) >= 8:
            arrow = "↓" if context["rsi_change"] < 0 else "↑"
            ctx_lines.append(
                f"  RSI `{context['rsi_then']:.0f}` {arrow} "
                f"`{context['rsi_now']:.0f}` ({context['rsi_change']:+.0f})"
            )
        if context.get("atr_change_pct", 0) != 0 and abs(context["atr_change_pct"]) > 30:
            ctx_lines.append(
                f"  ATR `{context['atr_pct_then']:.2f}%` → "
                f"`{context['atr_pct_now']:.2f}%` ({context['atr_change_pct']:+.0f}%)"
            )
        st_t, st_n = context.get("st_then", 0), context.get("st_now", 0)
        if st_t != st_n and st_t != 0 and st_n != 0:
            from_arrow = "↑" if st_t == 1 else "↓"
            to_arrow = "↑" if st_n == 1 else "↓"
            ctx_lines.append(f"  Supertrend {from_arrow} → {to_arrow}（趨勢翻轉）")
        if context.get("vol_change", 1) >= 1.5:
            ctx_lines.append(f"  量能爆 `{context['vol_change']:.1f}x` 均量")
        if context.get("adx_now") is not None:
            ctx_lines.append(
                f"  當前 ADX `{context['adx_now']:.0f}`"
                f"（{'趨勢' if context['adx_now'] > 25 else '震盪'}）"
            )
        if ctx_lines:
            lines.append("")
            lines.append("📈 *出場前市況：*")
            lines.extend(ctx_lines)

    if lessons:
        lines.append("")
        lines.append(f"💡 *教訓：* {lessons[0]}")

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
# 14. 風控（#6 USD、#7 DST）
# ═════════════════════════════════════════════════════════
def check_circuit_breaker(cfg: dict) -> tuple[bool, str]:
    cb = cfg.get("circuit_breaker", {})
    threshold = cb.get("loss_threshold", 3)
    pause_h = cb.get("pause_hours", 24)
    history = _load_json(TRADE_HISTORY_FILE, [])
    # 修完 #1 後，每張單只記一筆，連敗判斷自然準
    closed = [t for t in history
              if t.get("close_type") in ("SL", "BE", "LOCK", "TP3")]
    if len(closed) < threshold:
        return False, ""
    last_n = closed[-threshold:]
    if not all(t.get("close_type") == "SL" for t in last_n):
        return False, ""
    try:
        last_sl_dt = datetime.strptime(
            last_n[-1]["time"], "%Y-%m-%d %H:%M"
        ).replace(tzinfo=TW_TZ)
    except Exception:
        return False, ""
    elapsed = (tw_now() - last_sl_dt).total_seconds() / 3600
    if elapsed < pause_h:
        return True, f"🔥 連 {threshold} 敗熔斷，剩餘 `{pause_h - elapsed:.1f}h`"
    return False, ""


def is_in_news_window(cfg: dict) -> tuple[bool, str]:
    """#7 NFP/CPI 改用 US/Eastern 即時換算，自動處理 DST"""
    now = tw_now()

    # 手動 news_blackouts（用戶覆寫）
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
    if not (auto.get("nfp", True) or auto.get("cpi", True)):
        return False, ""
    if ET_TZ is None:
        return False, ""

    et_now = now.astimezone(ET_TZ)
    before_min = auto.get("et_window_min_before", 35)
    after_min = auto.get("et_window_min_after", 60)

    # NFP：第一個週五 8:30 ET
    if auto.get("nfp", True) and et_now.weekday() == 4 and et_now.day <= 7:
        target = et_now.replace(hour=8, minute=30, second=0, microsecond=0)
        window_start = target - timedelta(minutes=before_min)
        window_end = target + timedelta(minutes=after_min)
        if window_start <= et_now <= window_end:
            return True, f"NFP 非農（ET 8:30，當前 {et_now.strftime('%H:%M ET')}）"

    # CPI：10~16 號 8:30 ET（粗略）
    if auto.get("cpi", True) and 10 <= et_now.day <= 16:
        target = et_now.replace(hour=8, minute=30, second=0, microsecond=0)
        window_start = target - timedelta(minutes=before_min)
        window_end = target + timedelta(minutes=after_min)
        if window_start <= et_now <= window_end:
            return True, f"CPI 數據（ET 8:30，當前 {et_now.strftime('%H:%M ET')}）"

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
    closed = [t for t in today_trades
              if t.get("close_type") in ("SL", "BE", "LOCK", "TP3")]
    return {
        "trades_count": len(closed),
        "pnl_pct": sum(t.get("pnl", 0) for t in closed),
        "pnl_usd": sum(t.get("pnl_usd", 0) for t in closed),  # #6
        "wins": sum(1 for t in closed if t.get("close_type") in ("TP3", "LOCK")),
        "losses": sum(1 for t in closed if t.get("close_type") == "SL"),
    }


def check_daily_limits(cfg: dict, tracker) -> tuple[bool, str]:
    """🛡️ #6 改用 USD 為日紅線單位"""
    dl = cfg.get("daily_limits", {})

    # 持倉上限
    open_count = sum(
        1 for s in tracker.signals.values()
        if s.get("status") in ("PENDING", "ACTIVE", "BE", "TRAIL")
    )
    if open_count >= dl.get("max_concurrent_positions", 2):
        return True, f"📦 持倉達上限：{open_count}/{dl['max_concurrent_positions']}"

    stats = get_today_stats()

    # 優先用 USD 紅線
    loss_limit_usd = dl.get("daily_loss_limit_usd")
    if loss_limit_usd is not None:
        if stats["pnl_usd"] < -abs(loss_limit_usd):
            return True, (
                f"⚠️ 當日 PnL `${stats['pnl_usd']:.0f}` 已過 "
                f"-${abs(loss_limit_usd)} 紅線"
            )

    # fallback：百分比（針對沒 sizing 資料的舊歷史）
    loss_limit_pct = dl.get("daily_loss_limit_pct", 5.0)
    if stats["pnl_pct"] < -loss_limit_pct:
        return True, (
            f"⚠️ 當日累積 PnL `{stats['pnl_pct']:.2f}%` 已過 "
            f"-{loss_limit_pct}% 紅線"
        )

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
# 16. 交易記錄（#1 每張單只記一次）
# ═════════════════════════════════════════════════════════
def record_trade_final(sig: dict, close_type: str, close_price: float) -> None:
    """#1 每張單只在最終出場時呼叫一次"""
    side = sig["side"]
    entry = sig["entry"]
    score = sig.get("score", 0)
    raw_score = sig.get("raw_score", score)

    # 用 ⅓ 分批假設計算實際 PnL%
    if close_type == "TP3":
        avg_close = (sig["tp1"] + sig["tp2"] + sig["tp3"]) / 3
    elif close_type == "LOCK":
        # ⅓ tp1 + ⅓ tp2 + ⅓ 出場價（=tp1，因為 SL 在 tp1）
        avg_close = (sig["tp1"] + sig["tp2"] + close_price) / 3
    elif close_type == "BE":
        # ⅓ tp1 + ⅔ 出場價（=entry）
        avg_close = (sig["tp1"] + close_price * 2) / 3
    else:  # SL
        avg_close = close_price

    pnl_pct = (
        ((avg_close - entry) / entry * 100)
        if side == "LONG"
        else ((entry - avg_close) / entry * 100)
    )

    tp_r = sig.get("tp_r_ratios", [1.5, 3.0, 5.0])
    realized_r = calc_realized_r(close_type, tp_r)
    realized_usd = calc_realized_usd(sig, close_type)

    coin = sig["instId"].split("-")[0]
    order_id = sig.get("order_id", "?")
    detail = sig.get("detail", {})
    fr = sig.get("funding_rate")
    mtf = sig.get("mtf_snapshot")
    features = vectorize_signal(score, side, detail, fr, mtf)

    is_win = close_type in ("TP3", "LOCK")

    trade = {
        "time": tw_now().strftime("%Y-%m-%d %H:%M"),
        "date": tw_now().strftime("%Y-%m-%d"),
        "order_id": order_id,
        "coin": coin,
        "side": side,
        "entry": entry,
        "close": round(avg_close, 6),
        "close_type": close_type,
        "pnl": round(pnl_pct, 2),
        "pnl_usd": realized_usd,        # #6 美元損益
        "realized_r": realized_r,
        "tp_hits": {
            "tp1": bool(sig.get("hit_tp1")),
            "tp2": bool(sig.get("hit_tp2")),
            "tp3": bool(sig.get("hit_tp3")),
        },
        "is_win": is_win,
        "score": score,
        "raw_score": raw_score,         # #9 學習用
        "funding_rate": fr,
        "detail": detail,
        "features": features,
        "mtf": mtf,
    }
    history = _load_json(TRADE_HISTORY_FILE, [])
    history.append(trade)
    _save_json(TRADE_HISTORY_FILE, history)
    logging.info(f"📝 記錄：{coin} {order_id} {close_type} "
                 f"({pnl_pct:+.2f}% / {realized_r:+.1f}R / ${realized_usd:+.0f})")
    try:
        update_learning(trade, sig)
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
        self.cfg = load_config()

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
            # 保留原始 SL 給 sizing 計算（即使 SL 被推到 BE/TP1 也要算原始風險）
            "sl_original": signal["sl"],
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

            # #4 最大持倉時長
            max_hold_h = self.cfg.get("max_hold_hours", 48)
            activated = sig.get("activated_at", time.time())
            if (time.time() - activated) / 3600 > max_hold_h:
                self._force_close_by_timeout(sig, price)
                return True

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

        # #12 進場容差從 config 取
        ez = self.cfg.get("entry_zone_pct", DEFAULT_CONFIG["entry_zone_pct"])
        if side == "LONG":
            low = entry * (1 - ez.get("long_favor", 0.006))
            high = entry * (1 + ez.get("long_against", 0.002))
            in_zone = low <= price <= high
        else:
            low = entry * (1 - ez.get("short_against", 0.002))
            high = entry * (1 + ez.get("short_favor", 0.006))
            in_zone = low <= price <= high

        if in_zone:
            now_ts = time.time()
            sig["status"] = "ACTIVE"
            sig["activated_at"] = now_ts
            sig["last_checked_ts"] = now_ts
            msg_id = send_tg(
                _fmt_entry(sig, price),
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

        # #5 同根 K 線 TP/SL 衝突時 → SL 優先（保守）
        # 先判斷 SL 是否會被掃，若會則直接出場
        conservative = self.cfg.get("conservative_sl_first", True)
        if conservative and against_hit(sl):
            return self._finalize_close(sig, sl, candle, wick_against(sl))

        # TP1 milestone（不 record）
        if not sig.get("hit_tp1") and favor_hit(tp1):
            sig["hit_tp1"] = True
            sig["sl"] = entry
            sig["status"] = "BE"
            sl = entry
            pnl = ((tp1 - entry) / entry * 100) if side == "LONG" else ((entry - tp1) / entry * 100)
            tp_r = sig.get("tp_r_ratios", [1.5, 3.0, 5.0])
            send_tg(
                _fmt_tp_milestone(coin, order_id, "TP1", tp1, pnl, tp_r[0],
                                  wick_favor(tp1), "已平 ⅓ + SL 移到進場"),
                reply_markup=kb, reply_to_message_id=reply_to,
            )
            self._save()
            self.transitions += 1

        # TP2 milestone（不 record）
        if not sig.get("hit_tp2") and favor_hit(tp2):
            sig["hit_tp2"] = True
            sig["sl"] = tp1
            sig["status"] = "TRAIL"
            sl = tp1
            pnl = ((tp2 - entry) / entry * 100) if side == "LONG" else ((entry - tp2) / entry * 100)
            tp_r = sig.get("tp_r_ratios", [1.5, 3.0, 5.0])
            send_tg(
                _fmt_tp_milestone(coin, order_id, "TP2", tp2, pnl, tp_r[1],
                                  wick_favor(tp2), "再平 ⅓ + SL 鎖到 TP1"),
                reply_markup=kb, reply_to_message_id=reply_to,
            )
            self._save()
            self.transitions += 1

        # TP3 → 最終結算
        if not sig.get("hit_tp3") and favor_hit(tp3):
            sig["hit_tp3"] = True
            return self._finalize_close(sig, tp3, candle, wick_favor(tp3))

        # SL（保守模式下已在開頭處理，這裡是非保守模式）
        if not conservative and against_hit(sl):
            return self._finalize_close(sig, sl, candle, wick_against(sl))

        return False

    def _finalize_close(self, sig: dict, exit_price: float, candle: dict,
                        wick: bool) -> bool:
        """🏁 最終出場：決定 close_type、發訊息、record_trade（只一次）"""
        side = sig["side"]
        entry = sig["entry"]
        coin = sig["instId"].split("-")[0]
        order_id = sig["order_id"]
        reply_to = sig.get("entry_message_id")
        kb = _order_keyboard(order_id)

        # 決定 close_type
        if sig.get("hit_tp3"):
            close_type = "TP3"
        elif sig.get("hit_tp2"):
            close_type = "LOCK"
        elif sig.get("hit_tp1"):
            close_type = "BE"
        else:
            close_type = "SL"

        # 計算實際 pnl%（用 ⅓ 平均）
        if close_type == "TP3":
            avg_close = (sig["tp1"] + sig["tp2"] + sig["tp3"]) / 3
        elif close_type == "LOCK":
            avg_close = (sig["tp1"] + sig["tp2"] + exit_price) / 3
        elif close_type == "BE":
            avg_close = (sig["tp1"] + exit_price * 2) / 3
        else:
            avg_close = exit_price
        pnl_pct = (
            ((avg_close - entry) / entry * 100)
            if side == "LONG"
            else ((entry - avg_close) / entry * 100)
        )

        tp_r = sig.get("tp_r_ratios", [1.5, 3.0, 5.0])
        realized_r = calc_realized_r(close_type, tp_r)
        realized_usd = calc_realized_usd(sig, close_type)

        send_tg(
            _fmt_final_close(coin, order_id, close_type, exit_price,
                             pnl_pct, realized_usd, realized_r, wick),
            reply_markup=kb, reply_to_message_id=reply_to,
        )

        record_trade_final(sig, close_type, exit_price)

        # 覆盤（除了 TP3 全勝以外都送）
        if close_type != "TP3":
            self._send_postmortem(sig, close_type)
        elif self.cfg.get("post_mortem", {}).get("enabled", True) and \
             not self.cfg.get("post_mortem", {}).get("loss_only", False):
            self._send_postmortem(sig, close_type)

        self.transitions += 1
        return True

    def _force_close_by_timeout(self, sig: dict, price: float) -> None:
        """#4 ACTIVE 持倉超過 max_hold_hours，依當前價分類"""
        side = sig["side"]
        entry = sig["entry"]
        coin = sig["instId"].split("-")[0]
        order_id = sig["order_id"]
        in_profit = (
            price > entry if side == "LONG"
            else price < entry
        )

        if sig.get("hit_tp2"):
            close_type = "LOCK"
            exit_price = sig["tp1"]
        elif sig.get("hit_tp1"):
            close_type = "BE" if not in_profit else "BE"
            exit_price = entry
        else:
            # 沒到任何 TP，按目前價歸類
            close_type = "BE" if in_profit else "SL"
            exit_price = price if not in_profit else entry

        send_tg(
            f"⏰ *{coin} 持倉超時自動平倉*\n"
            f"🆔 `{order_id}`\n"
            f"持倉超過 {self.cfg.get('max_hold_hours', 48)}h，"
            f"以當前價 `{price:.4f}` 結算"
        )

        tp_r = sig.get("tp_r_ratios", [1.5, 3.0, 5.0])
        realized_r = calc_realized_r(close_type, tp_r)
        realized_usd = calc_realized_usd(sig, close_type)
        pnl_pct = (
            ((exit_price - entry) / entry * 100)
            if side == "LONG"
            else ((entry - exit_price) / entry * 100)
        )

        send_tg(
            _fmt_final_close(coin, order_id, close_type, exit_price,
                             pnl_pct, realized_usd, realized_r, False),
            reply_markup=_order_keyboard(order_id),
            reply_to_message_id=sig.get("entry_message_id"),
        )

        record_trade_final(sig, close_type, exit_price)
        if close_type != "TP3":
            self._send_postmortem(sig, close_type)
        self.transitions += 1

    def _send_postmortem(self, sig: dict, close_type: str) -> None:
        coin = sig.get("instId", "?").split("-")[0]
        order_id = sig.get("order_id", "?")
        try:
            if not self.cfg.get("post_mortem", {}).get("enabled", True):
                return
            activated = sig.get("activated_at") or sig.get("created") or 0
            all_c = fetch_candles(sig["instId"], tf="15m", limit=100)
            df = [c for c in all_c if (c["ts"] / 1000) >= (activated - 900)]
            if len(df) < 10:
                send_tg(
                    f"🔍 *{coin} 覆盤*\n"
                    f"🆔 `{order_id}`\n"
                    f"進場後資料太少（{len(df)} 根 K 線）",
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
                _fmt_postmortem(sig, close_type, reasons, lessons, similar, context),
                reply_to_message_id=sig.get("entry_message_id"),
            )
            if close_type == "SL":
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
            pnl = (
                ((price - p["entry"]) / p["entry"] * 100)
                if side == "LONG"
                else ((p["entry"] - price) / p["entry"] * 100)
            )
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
    win = sum(1 for t in trades if t.get("close_type") in ("TP3", "LOCK"))
    loss = sum(1 for t in trades if t.get("close_type") == "SL")
    be = sum(1 for t in trades if t.get("close_type") == "BE")
    pnl = sum(t.get("pnl", 0) for t in trades)
    pnl_usd = sum(t.get("pnl_usd", 0) for t in trades)
    pnls = [t.get("pnl", 0) for t in trades]
    return {
        "n": n, "win": win, "loss": loss, "be": be,
        "wr": win / n * 100, "pnl": pnl, "pnl_usd": pnl_usd,
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
        f"勝率：`{s['wr']:.0f}%`　PnL：`{s['pnl']:+.2f}%` / `${s['pnl_usd']:+.0f}`",
        f"最大獲利：`{s['max_win']:+.2f}%`　最大虧損：`{s['max_loss']:+.2f}%`",
        f"",
    ]
    by_coin = {}
    for t in today:
        by_coin.setdefault(t.get("coin", "?"), []).append(t)
    if by_coin:
        lines.append("💎 *各幣種：*")
        for c, ts in sorted(by_coin.items(),
                            key=lambda x: -sum(t.get("pnl_usd", 0) for t in x[1])):
            sub = _summarize(ts)
            lines.append(
                f"  {c}: {sub['n']} 筆 / 勝率 `{sub['wr']:.0f}%` / "
                f"`${sub['pnl_usd']:+.0f}`"
            )
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
        if ct in ("TP3", "LOCK"):
            cur = cur + 1 if type_ == "win" else 1
            type_ = "win"; max_w = max(max_w, cur)
        elif ct == "SL":
            cur = cur + 1 if type_ == "loss" else 1
            type_ = "loss"; max_l = max(max_l, cur)
    lines = [
        f"📈 *月報 {year_month}*",
        f"━━━━━━━━━━━━━━",
        f"總交易：{s['n']} 筆（勝 {s['win']} / 平 {s['be']} / 敗 {s['loss']}）",
        f"勝率：`{s['wr']:.0f}%`　PnL：`{s['pnl']:+.2f}%` / `${s['pnl_usd']:+.0f}`",
        f"最大獲利：`{s['max_win']:+.2f}%`　最大虧損：`{s['max_loss']:+.2f}%`",
        f"🔥 最大連勝：{max_w}　❄️ 最大連敗：{max_l}",
        f"",
    ]
    by_coin = {}
    for t in month:
        by_coin.setdefault(t.get("coin", "?"), []).append(t)
    if by_coin:
        lines.append("💎 *各幣種：*")
        for c, ts in sorted(by_coin.items(),
                            key=lambda x: -sum(t.get("pnl_usd", 0) for t in x[1])):
            sub = _summarize(ts)
            lines.append(
                f"  {c}: {sub['n']} 筆 / 勝率 `{sub['wr']:.0f}%` / "
                f"`${sub['pnl_usd']:+.0f}`"
            )
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
        for c, d in sorted(by_coin.items(),
                           key=lambda x: -x[1].get("total", 0))[:12]:
            n = d.get("total", 0)
            w = d.get("win", 0)
            l = d.get("loss", 0)
            be = d.get("be", 0)
            wr = w / n * 100 if n else 0
            lines.append(
                f"  {c}: {n} 筆（勝 {w} / 平 {be} / 敗 {l}，勝率 `{wr:.0f}%`）"
            )
        lines.append("")
    high = [(b, d) for b, d in buckets.items()
            if d.get("total", 0) >= 5 and d["win"] / d["total"] > 0.6]
    low = [(b, d) for b, d in buckets.items()
           if d.get("total", 0) >= 5 and d["win"] / d["total"] < 0.4]
    if high:
        lines.append("✅ *高勝率組合：*")
        for b, d in sorted(high, key=lambda x: -x[1]["win"] / x[1]["total"])[:5]:
            lines.append(
                f"  `{b}` → {d['total']} 筆，勝率 "
                f"`{d['win'] / d['total'] * 100:.0f}%`"
            )
        lines.append("")
    if low:
        lines.append("⚠️ *低勝率組合：*")
        for b, d in sorted(low, key=lambda x: x[1]["win"] / x[1]["total"])[:5]:
            lines.append(
                f"  `{b}` → {d['total']} 筆，勝率 "
                f"`{d['win'] / d['total'] * 100:.0f}%`"
            )
        lines.append("")
    lines.append("💡 _累積資料越多，KNN 評分調整越精準_")
    return "\n".join(lines)


def format_audit_report() -> str:
    history = _load_json(TRADE_HISTORY_FILE, [])
    closed = [t for t in history
              if t.get("close_type") in ("SL", "BE", "LOCK", "TP3")]
    n_closed = len(closed)
    if n_closed < 10:
        return (
            f"📭 *指標有效性審查*\n"
            f"資料不足（{n_closed} 筆 < 10 不足以審查）"
        )

    def _stats(trades):
        n = len(trades)
        if n == 0:
            return None
        wins = sum(1 for t in trades if t["close_type"] in ("TP3", "LOCK"))
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
    cooldown_h = cfg.get("cooldown_hours", 2)
    pv_cfg = cfg.get("price_verification", {})
    pv_enabled = pv_cfg.get("enabled", True)
    pv_max_dev = pv_cfg.get("max_deviation_pct", 0.5)
    pv_block = pv_cfg.get("block_on_unverified", False)

    logging.info("🚀 開始掃描...")

    unhealthy, h_msg = check_health()
    if unhealthy:
        send_tg(h_msg)

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
            if pv_enabled:
                ok, tv_price, diff = verify_price(
                    instId, okx_price, pv_max_dev, pv_block
                )
                if not ok:
                    if tv_price is not None:
                        send_tg(
                            f"⚠️ *{instId.split('-')[0]} 價格異常*\n"
                            f"OKX `{okx_price:.4f}` vs TV `{tv_price:.4f}`\n"
                            f"偏離 `{diff:.3f}%` > {pv_max_dev}%"
                        )
                    continue
            df = fetch_candles(instId, tf="15m", limit=100,
                               include_unconfirmed=False)
            if not df:
                continue
            funding = fetch_funding_rate(instId)
            sig = generate_signal(instId, df, okx_price, funding, cfg)
            if not sig:
                continue

            ez = cfg.get("entry_zone_pct", DEFAULT_CONFIG["entry_zone_pct"])
            if sig["side"] == "LONG":
                low = sig["entry"] * (1 - ez.get("long_favor", 0.006))
                high = sig["entry"] * (1 + ez.get("long_against", 0.002))
            else:
                low = sig["entry"] * (1 - ez.get("short_against", 0.002))
                high = sig["entry"] * (1 + ez.get("short_favor", 0.006))
            in_zone = low <= okx_price <= high

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

    if tracker.signals:
        intensive_monitor(tracker, total_seconds=50, interval_seconds=10)
    return sent


def intensive_monitor(tracker: SignalTracker, total_seconds: int = 50,
                      interval_seconds: int = 10) -> None:
    if not tracker.signals:
        return
    end_time = time.time() + total_seconds
    polls = 0
    while time.time() < end_time and tracker.signals:
        time.sleep(interval_seconds)
        try:
            _price_cache.clear()
            _candle_cache.clear()
            tracker.check_all()
            polls += 1
        except Exception as e:
            logging.error(f"❌ intensive_monitor poll: {e}")
    logging.info(f"📡 即時監控完成：{polls} 輪 × {interval_seconds}s 間隔")


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
    # #16 啟動時驗證環境變數
    if not TG_TOKEN or not CHAT_ID:
        sys.stderr.write("❌ TG_TOKEN 或 CHAT_ID 環境變數未設定\n")
        sys.exit(1)

    try:
        logging.info("=" * 50)
        logging.info("🤖 Alpha Oracle Pro v15.3 修正版")
        logging.info(f"⏰ {tw_ts()}")
        logging.info("=" * 50)

        with GlobalLock(LOCK_FILE):
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
    except SystemExit:
        raise
    except Exception as e:
        logging.error(f"🔥 系統錯誤：{e}")
        try:
            s = get_system_state()
            s["scan_failure_count"] = s.get("scan_failure_count", 0) + 1
            set_system_state(s)
        except Exception:
            pass
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
