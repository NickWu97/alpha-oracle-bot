import requests
import os
import json
import hmac
import hashlib
import base64
import pandas as pd
import numpy as np
import logging
import traceback
import time
from datetime import datetime, timedelta, timezone

# ─────────────────────────────────────────────
# 1. 基礎配置
# ─────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Telegram
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID  = os.getenv("CHAT_ID")

# OKX 自動下單（設 AUTO_TRADE=true 才啟用）
OKX_API_KEY    = os.getenv("OKX_API_KEY",    "")
OKX_SECRET     = os.getenv("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
OKX_TD_MODE    = os.getenv("OKX_TD_MODE", "isolated")  # isolated=逐倉 / cross=全倉
OKX_LEVER      = int(os.getenv("OKX_LEVER", "5"))       # 槓桿倍數
AUTO_TRADE     = os.getenv("AUTO_TRADE", "false").lower() == "true"

# ── 模擬倉模式 ──────────────────────────────────────────────────────────────
# OKX_DEMO=true → 所有 API 請求加上 x-simulated-trading:1，走模擬帳戶
# 模擬倉需要在 OKX「模擬交易」頁面另外建立 API Key（非實盤 Key）
OKX_DEMO = os.getenv("OKX_DEMO", "false").lower() == "true"

# ── 同時開倉上限 ───────────────────────────────────────────────────────────
# 最多同時持有幾個倉位（含 WAITING 等待進場）
# 200U / 10倉 = 每倉最多 20U 風險資金
OKX_MAX_POSITIONS = int(os.getenv("OKX_MAX_POSITIONS", "10"))

# ── 倉位大小風控（二選一）───────────────────────────────────────────────────
#
# 【方式 A】風險比例（推薦，OKX_RISK_PCT > 0 時優先生效）
#   OKX_RISK_PCT=1  → 每單最大虧損 = 帳戶淨值 × 1%
#   例：帳戶 200U，OKX_RISK_PCT=1 → 每單最多虧 2U
#   bot 自動抓餘額與合約乘數，換算下幾張。虧損永遠不超過設定比例。
#
# 【方式 B】固定張數（OKX_RISK_PCT=0 時生效）
#   OKX_TRADE_SIZE=1 → 每單固定下 1 張合約（不管帳戶大小）
#
# 建議設定（200U / 10倉 嚴格風控）：
#   OKX_RISK_PCT=1   → 每單最多虧 2U（帳戶的 1%）
#   OKX_MAX_POSITIONS=10 → 最多同時持有 10 倉
#   OKX_TD_MODE=isolated → 逐倉，每倉獨立風控，一倉爆倉不影響其他
OKX_TRADE_SZ_FIXED = os.getenv("OKX_TRADE_SIZE", "1")
OKX_RISK_PCT       = float(os.getenv("OKX_RISK_PCT", "1"))  # 預設 1%

ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP",
    "BCH-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP"
]

LOG_FILE            = "active_trades.csv"
STATS_FILE          = "daily_stats.csv"
WAITING_EXPIRY_BARS = 20  # WAITING 超過幾根 K 棒自動清除（15m × 20 = 5 小時）

# ord_id = 進場單 ID（追蹤用）；algo_id = 止損 Algo 單 ID（移動止損用）
LOG_COLS   = ["instId", "side", "status", "entry", "sl", "tp1", "tp2", "tp3",
              "locked", "wait_since", "tp1_hit", "ord_id", "algo_id"]
STATS_COLS = ["instId", "result"]


# ─────────────────────────────────────────────
# 2. 工具函數
# ─────────────────────────────────────────────

def safe_float(val, fallback=0.0):
    try:    return float(val)
    except: return fallback

def safe_int(val, fallback=0):
    try:    return int(float(val))
    except: return fallback

def normalize_trade(t: dict) -> dict:
    """確保從 CSV 讀回來的欄位型態正確（原本 locked="0" 比較失效的 bug）"""
    return {
        "instId":     str(t.get("instId", "")),
        "side":       str(t.get("side", "")),
        "status":     str(t.get("status", "")),
        "entry":      safe_float(t.get("entry")),
        "sl":         safe_float(t.get("sl")),
        "tp1":        safe_float(t.get("tp1")),
        "tp2":        safe_float(t.get("tp2")),
        "tp3":        safe_float(t.get("tp3")),
        "locked":     safe_int(t.get("locked")),
        "wait_since": safe_int(t.get("wait_since", 0)),
        "tp1_hit":    safe_int(t.get("tp1_hit", 0)),  # 0=未通知, 1=已通知
        "ord_id":     str(t.get("ord_id",  "")),       # OKX 進場單 ID
        "algo_id":    str(t.get("algo_id", "")),       # OKX 止損 Algo 單 ID
    }


# ─────────────────────────────────────────────
# 3. 數據抓取
# ─────────────────────────────────────────────

def fetch_okx(instId: str) -> pd.DataFrame | None:
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=15m&limit=100"
        res = requests.get(url, timeout=10).json()
        df  = pd.DataFrame(
            res['data'],
            columns=['ts', 'o', 'h', 'l', 'c', 'v', 'volCcy', 'volCcyQuote', 'confirm']
        )
        df[['o', 'h', 'l', 'c', 'v']] = df[['o', 'h', 'l', 'c', 'v']].astype(float)
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except Exception as e:
        logging.warning(f"[{instId}] K 線抓取失敗: {e}")
        return None

def fetch_current_candle_hl(instId: str) -> tuple[float, float]:
    """
    抓取當前「未收盤」K 棒的最高/最低價（confirm="0"）。
    用於 WAITING 進場偵測，避免漏掉正在形成中的 K 棒觸及進場位。
    回傳 (low, high)；抓不到時回傳不影響判斷的安全值。
    """
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=15m&limit=3"
        res = requests.get(url, timeout=5).json()
        for row in res['data']:
            if row[8] == "0":                    # confirm == "0" 即當前未收盤
                return float(row[3]), float(row[2])  # (low, high)
    except Exception as e:
        logging.warning(f"[{instId}] 當前 K 棒抓取失敗: {e}")
    return float('inf'), float('-inf')           # 安全值：不觸發任何判斷

def get_funding_ls(instId: str) -> tuple[str, str]:
    """抓取資金費率與多空持倉比"""
    base_id  = instId.replace("-SWAP", "").split("-")[0]
    funding  = "N/A"
    ls_ratio = "N/A"
    try:
        f_res   = requests.get(
            f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5
        ).json()
        funding = f"{float(f_res['data'][0]['fundingRate']) * 100:.4f}%"
    except Exception as e:
        logging.warning(f"[{instId}] 資金費率抓取失敗: {e}")
    try:
        ls_res   = requests.get(
            f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5
        ).json()
        ls_ratio = ls_res['data'][0]['ratio']
    except Exception as e:
        logging.warning(f"[{instId}] 多空比抓取失敗: {e}")
    return funding, ls_ratio

def send_tg(msg: str):
    if not TG_TOKEN or not CHAT_ID: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=15
        )
    except Exception as e:
        logging.warning(f"Telegram 發送失敗: {e}")


# ─────────────────────────────────────────────
# 4-A. OKX 自動下單 API
# ─────────────────────────────────────────────

def _okx_headers(method: str, path: str, body: str = "") -> dict:
    """產生 OKX REST API 簽名 Header（模擬倉時自動加 x-simulated-trading:1）"""
    ts  = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    msg = ts + method.upper() + path + body
    sig = base64.b64encode(
        hmac.new(OKX_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    h = {
        "OK-ACCESS-KEY":        OKX_API_KEY,
        "OK-ACCESS-SIGN":       sig,
        "OK-ACCESS-TIMESTAMP":  ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type":         "application/json",
    }
    if OKX_DEMO:
        h["x-simulated-trading"] = "1"   # 模擬倉專用 Header
    return h

def _okx_get(path: str, params: str = "") -> dict:
    """OKX GET 通用函數"""
    full_path = path + params
    try:
        r = requests.get(
            f"https://www.okx.com{full_path}",
            headers=_okx_headers("GET", full_path),
            timeout=10
        ).json()
        return r
    except Exception as e:
        logging.warning(f"OKX GET {path} 異常: {e}")
        return {"code": "-1", "msg": str(e), "data": []}

def _okx_post(path: str, payload) -> dict:
    """OKX POST 通用函數（payload 可為 dict 或 list）"""
    body = json.dumps(payload)
    try:
        r = requests.post(
            f"https://www.okx.com{path}",
            headers=_okx_headers("POST", path, body),
            data=body, timeout=10
        ).json()
        return r
    except Exception as e:
        logging.warning(f"OKX POST {path} 異常: {e}")
        return {"code": "-1", "msg": str(e), "data": []}

def okx_get_equity() -> float:
    """
    抓取帳戶 USDT 淨值（用於風險比例計算倉位）。
    回傳淨值浮點數，失敗時回傳 0.0。
    """
    r = _okx_get("/api/v5/account/balance", "?ccy=USDT")
    try:
        details = r["data"][0]["details"]
        for d in details:
            if d.get("ccy") == "USDT":
                eq = float(d.get("eq", 0))
                logging.info(f"帳戶 USDT 淨值：{eq:.2f}")
                return eq
    except Exception as e:
        logging.warning(f"帳戶餘額解析失敗: {e}")
    return 0.0

# 合約乘數快取（避免每單重複請求）
_ct_val_cache: dict[str, float] = {}

def okx_get_ct_val(instId: str) -> float:
    """
    抓取合約乘數 ctVal（每張合約代表多少標的資產）。
    例：BTC-USDT-SWAP ctVal=0.01，即 1 張 = 0.01 BTC。
    快取結果，整個執行週期只請求一次。
    """
    if instId in _ct_val_cache:
        return _ct_val_cache[instId]
    r = _okx_get("/api/v5/public/instruments",
                 f"?instType=SWAP&instId={instId}")
    try:
        ct_val = float(r["data"][0]["ctVal"])
        _ct_val_cache[instId] = ct_val
        logging.info(f"[{instId}] ctVal={ct_val}")
        return ct_val
    except Exception as e:
        logging.warning(f"[{instId}] ctVal 取得失敗: {e}，使用預設 1")
        return 1.0

def okx_calc_size(instId: str, entry: float, sl: float) -> str:
    """
    自動計算下單張數：
    ┌ 若 OKX_RISK_PCT > 0（風險比例模式）：
    │   張數 = floor(帳戶淨值 × 風險% / (|entry-sl| × ctVal))
    │   最少 1 張
    └ 否則（固定張數模式）：
        直接回傳 OKX_TRADE_SZ_FIXED
    回傳字串（OKX sz 欄位需要字串）
    """
    if OKX_RISK_PCT <= 0:
        return OKX_TRADE_SZ_FIXED          # 固定張數

    equity  = okx_get_equity()
    if equity <= 0:
        logging.warning("帳戶餘額為 0，退回固定張數")
        return OKX_TRADE_SZ_FIXED

    ct_val       = okx_get_ct_val(instId)
    price_risk   = abs(entry - sl)          # 每單位標的的虧損（USDT）
    risk_usdt    = equity * OKX_RISK_PCT / 100
    # 每張合約在此次交易中的潛在虧損 = price_risk × ctVal
    loss_per_lot = price_risk * ct_val
    if loss_per_lot <= 0:
        return OKX_TRADE_SZ_FIXED

    sz = max(1, int(risk_usdt / loss_per_lot))
    logging.info(
        f"[{instId}] 風險計算 → 淨值:{equity:.1f} 風險:{risk_usdt:.1f}U "
        f"每張虧:{loss_per_lot:.4f}U → 下 {sz} 張"
    )
    return str(sz)

def okx_set_leverage(instId: str, lever: int):
    """設定雙向持倉槓桿（開倉前呼叫）"""
    for ps in ["long", "short"]:
        r = _okx_post("/api/v5/account/set-leverage", {
            "instId":  instId,
            "lever":   str(lever),
            "mgnMode": OKX_TD_MODE,
            "posSide": ps,
        })
        if r.get("code") != "0":
            logging.warning(f"[{instId}] 設定槓桿 {ps} 失敗: {r.get('msg')}")

def okx_place_entry(instId: str, side: str, sz: str) -> str:
    """
    市價進場單。
    side: "LONG" or "SHORT"；sz: 張數字串
    回傳 ordId（失敗時回傳 ""）
    """
    demo_tag = " [模擬倉]" if OKX_DEMO else ""
    payload = {
        "instId":  instId,
        "tdMode":  OKX_TD_MODE,
        "side":    "buy"  if side == "LONG" else "sell",
        "posSide": "long" if side == "LONG" else "short",
        "ordType": "market",
        "sz":      sz,
    }
    r = _okx_post("/api/v5/trade/order", payload)
    if r.get("code") == "0":
        oid = r["data"][0].get("ordId", "")
        logging.info(f"[{instId}]{demo_tag} 進場單成功 ordId={oid}  sz={sz}")
        return oid
    logging.warning(f"[{instId}] 進場單失敗: {r.get('msg')}")
    send_tg(
        f"⚠️ *[{instId.split('-')[0]}] 自動進場失敗{demo_tag}*\n"
        f"`{r.get('msg', '未知錯誤')}`"
    )
    return ""

def okx_place_sl_algo(instId: str, side: str, sl_px: float, sz: str) -> str:
    """
    下止損 Algo 單（conditional order）。
    觸發後以市價平倉。
    回傳 algoId（失敗時回傳 ""）
    """
    payload = {
        "instId":          instId,
        "tdMode":          OKX_TD_MODE,
        "side":            "sell" if side == "LONG" else "buy",
        "posSide":         "long" if side == "LONG" else "short",
        "ordType":         "conditional",
        "sz":              sz,
        "slTriggerPx":     f"{sl_px:.6f}",
        "slOrdPx":         "-1",
        "slTriggerPxType": "last",
    }
    r = _okx_post("/api/v5/trade/order-algo", payload)
    if r.get("code") == "0":
        aid = r["data"][0].get("algoId", "")
        logging.info(f"[{instId}] 止損 Algo 成功 algoId={aid}  SL={sl_px:.4f}  sz={sz}")
        return aid
    logging.warning(f"[{instId}] 止損 Algo 失敗: {r.get('msg')}")
    send_tg(
        f"⚠️ *[{instId.split('-')[0]}] 止損 Algo 設定失敗*\n"
        f"`{r.get('msg', '未知錯誤')}`\n"
        f"請手動設止損：{sl_px:.4f}"
    )
    return ""

def okx_cancel_algo(instId: str, algo_id: str):
    """取消 Algo 單（移動止損前先取消舊的）"""
    if not algo_id:
        return
    r = _okx_post("/api/v5/trade/cancel-algos",
                  [{"instId": instId, "algoId": algo_id}])
    if r.get("code") != "0":
        logging.warning(f"[{instId}] 取消 Algo {algo_id} 失敗: {r.get('msg')}")

def okx_close_market(instId: str, side: str, sz: str):
    """
    市價平倉（TP3 達到時由 bot 主動平倉）。
    若倉位已被 Algo 平掉，OKX 會回傳錯誤，忽略即可。
    """
    payload = {
        "instId":  instId,
        "tdMode":  OKX_TD_MODE,
        "side":    "sell" if side == "LONG" else "buy",
        "posSide": "long" if side == "LONG" else "short",
        "ordType": "market",
        "sz":      sz,
    }
    r = _okx_post("/api/v5/trade/order", payload)
    if r.get("code") != "0":
        logging.warning(f"[{instId}] 市價平倉回應: {r.get('msg')} (可能已由 Algo 平倉)")


# ─────────────────────────────────────────────
# 4. 技術指標
# ─────────────────────────────────────────────

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    high_low   = df['h'] - df['l']
    high_close = np.abs(df['h'] - df['c'].shift())
    low_close  = np.abs(df['l'] - df['c'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean().iloc[-1]

def calculate_cvd(df: pd.DataFrame, lookback: int = 20) -> tuple[float, str]:
    """
    真實 CVD 估算：用每根 K 棒方向加權成交量，累積買賣壓差異。
    陽線視為買壓主導，陰線視為賣壓主導。
    """
    recent = df.tail(lookback).copy()
    body   = (recent['h'] - recent['l']).replace(0, 1e-10)
    recent['delta'] = np.where(
        recent['c'] >= recent['o'],
        recent['v'] * (recent['c'] - recent['l']) / body,   # 買壓
        -recent['v'] * (recent['h'] - recent['c']) / body   # 賣壓
    )
    cvd = recent['delta'].sum()
    label = "🟢 大戶吸籌 (CVD+)" if cvd > 0 else "🔴 大戶出貨 (CVD-)"
    return cvd, label

def calculate_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> int:
    """
    Supertrend 指標（ATR 基準趨勢方向過濾）
    period=10, multiplier=3.0（與 Pine Script 預設一致）
    回傳  1 → 多頭趨勢（只做多）
    回傳 -1 → 空頭趨勢（只做空）
    回傳  0 → 資料不足，不過濾
    """
    if len(df) < period + 2:
        return 0

    high  = df['h'].values.astype(float)
    low   = df['l'].values.astype(float)
    close = df['c'].values.astype(float)
    n     = len(df)

    # True Range
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(high[i] - low[i],
                    abs(high[i] - close[i - 1]),
                    abs(low[i]  - close[i - 1]))

    # Wilder ATR（與 Pine Script 的 ta.atr 相同）
    atr = np.zeros(n)
    atr[period] = tr[1:period + 1].mean()
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

    hl2 = (high + low) / 2.0

    # 基礎上下軌
    basic_up = hl2 - multiplier * atr   # 多頭支撐下軌（up line）
    basic_dn = hl2 + multiplier * atr   # 空頭壓力上軌（dn line）

    # Trailing 軌道 + 趨勢方向
    final_up = np.zeros(n)
    final_dn = np.zeros(n)
    trend    = np.ones(n, dtype=int)

    final_up[period] = basic_up[period]
    final_dn[period] = basic_dn[period]

    for i in range(period + 1, n):
        # 多頭下軌只能往上收緊
        final_up[i] = (
            basic_up[i]
            if basic_up[i] > final_up[i - 1] or close[i - 1] < final_up[i - 1]
            else final_up[i - 1]
        )
        # 空頭上軌只能往下收緊
        final_dn[i] = (
            basic_dn[i]
            if basic_dn[i] < final_dn[i - 1] or close[i - 1] > final_dn[i - 1]
            else final_dn[i - 1]
        )
        # 趨勢翻轉判斷
        if trend[i - 1] == -1 and close[i] > final_dn[i - 1]:
            trend[i] = 1
        elif trend[i - 1] == 1 and close[i] < final_up[i - 1]:
            trend[i] = -1
        else:
            trend[i] = trend[i - 1]

    return int(trend[-1])


# ─────────────────────────────────────────────
# 5. SMC 結構分析
# ─────────────────────────────────────────────

def find_swing_points(df: pd.DataFrame, n: int = 2, lookback: int = 80) -> tuple[list, list]:
    """
    找出擺動高低點 (流動性池)。
    n = 每側需要幾根 K 棒確認，n 越大找出的越是顯著擺動。
    """
    data = df.tail(lookback).reset_index(drop=True)
    swing_highs, swing_lows = [], []
    for i in range(n, len(data) - n):
        window_h = data['h'].iloc[i - n: i + n + 1]
        window_l = data['l'].iloc[i - n: i + n + 1]
        if data['h'].iloc[i] == window_h.max():
            swing_highs.append(data['h'].iloc[i])
        if data['l'].iloc[i] == window_l.min():
            swing_lows.append(data['l'].iloc[i])
    return sorted(set(swing_highs)), sorted(set(swing_lows))

def detect_market_structure(df: pd.DataFrame) -> str:
    """
    偵測市場結構：
    • W底（雙底）：兩個相近低點 → 多頭反轉訊號
    • M頭（雙頂）：兩個相近高點 → 空頭反轉訊號
    • 趨勢延續 / 盤整
    """
    swing_highs, swing_lows = find_swing_points(df, n=3, lookback=60)

    # W底：最近兩個擺動低點差異 < 1.5%
    if len(swing_lows) >= 2:
        l1, l2 = swing_lows[-2], swing_lows[-1]
        if l1 > 0 and abs(l1 - l2) / l1 < 0.015:
            return "W底反轉 📐"

    # M頭：最近兩個擺動高點差異 < 1.5%
    if len(swing_highs) >= 2:
        h1, h2 = swing_highs[-2], swing_highs[-1]
        if h1 > 0 and abs(h1 - h2) / h1 < 0.015:
            return "M頭反轉 📐"

    # 趨勢判斷（近 20 根 K 棒漲跌幅）
    recent = df.tail(20)
    slope  = (recent['c'].iloc[-1] - recent['c'].iloc[0]) / (recent['c'].iloc[0] + 1e-10)
    if   slope >  0.025: return "上升趨勢延續 📈"
    elif slope < -0.025: return "下降趨勢延續 📉"
    return "區間盤整 ↔️"

def find_order_block(df: pd.DataFrame, side: str, lookback: int = 15) -> dict | None:
    """
    找出最近的訂單塊 (Order Block)：
    • 多頭 OB = 上漲前的最後一根陰線
    • 空頭 OB = 下跌前的最後一根陽線
    """
    data = df.tail(lookback).reset_index(drop=True)
    for i in range(len(data) - 2, 0, -1):
        k, k_next = data.iloc[i], data.iloc[i + 1]
        if side == "LONG" and k['c'] < k['o'] and k_next['c'] > k_next['o']:
            return {"high": k['o'], "low": k['l']}
        if side == "SHORT" and k['c'] > k['o'] and k_next['c'] < k_next['o']:
            return {"high": k['h'], "low": k['c']}
    return None

def find_recent_fvg(df: pd.DataFrame, side: str) -> dict | None:
    """
    找出最近的 FVG (公平價值缺口)：
    • 多頭 FVG = k2['l'] > k0['h']（三根K棒向上留下缺口）
    • 空頭 FVG = k2['h'] < k0['l']
    """
    for i in range(len(df) - 3, max(len(df) - 20, 0), -1):
        k0, k2 = df.iloc[i - 1], df.iloc[i + 1]
        if side == "LONG"  and k2['l'] > k0['h']:
            return {"high": k2['l'], "low": k0['h']}
        if side == "SHORT" and k2['h'] < k0['l']:
            return {"high": k0['l'], "low": k2['h']}
    return None

def calculate_structural_sl(df: pd.DataFrame, side: str, entry: float, atr: float) -> float:
    """
    結構性止損：
    優先掛在 OB 或 FVG 邊緣外側（加 ATR×0.25 緩衝），
    避免純 ATR 止損被輕易掃單。
    """
    buffer = atr * 0.25
    ob     = find_order_block(df, side)
    fvg    = find_recent_fvg(df, side)

    if side == "LONG":
        candidates = []
        if ob  and ob['low']  < entry: candidates.append(ob['low']  - buffer)
        if fvg and fvg['low'] < entry: candidates.append(fvg['low'] - buffer)
        if candidates:
            sl = max(candidates)  # 取最近（最高）的支撐
            if (entry - sl) / (entry + 1e-10) < 0.004:  # 止損 < 0.4% 太近，加寬
                sl = entry - atr * 1.5
            return sl
        return entry - atr * 1.5

    else:
        candidates = []
        if ob  and ob['high']  > entry: candidates.append(ob['high']  + buffer)
        if fvg and fvg['high'] > entry: candidates.append(fvg['high'] + buffer)
        if candidates:
            sl = min(candidates)
            if (sl - entry) / (entry + 1e-10) < 0.004:
                sl = entry + atr * 1.5
            return sl
        return entry + atr * 1.5

def get_fixed_r_tps(entry: float, sl: float, side: str) -> tuple[float, float, float]:
    """
    固定 R 倍數止盈：TP1=1R, TP2=2R, TP3=3R
    清晰、可預期，方便風險管理。
    """
    risk = abs(entry - sl) + 1e-10
    if side == "LONG":
        return entry + risk, entry + risk * 2, entry + risk * 3
    else:
        return entry - risk, entry - risk * 2, entry - risk * 3

def suggest_leverage(atr: float, price: float) -> tuple[str, str]:
    """根據 ATR 波動率自動建議槓桿倍數"""
    vol_pct = (atr / (price + 1e-10)) * 100
    if   vol_pct > 3:   return "3x ~ 5x",   "⚠️ 高波動"
    elif vol_pct > 1.5: return "5x ~ 10x",  "中波動"
    else:               return "10x ~ 20x", "低波動"

# ─────────────────────────────────────────────
# 6. 三層過濾器
# ─────────────────────────────────────────────

def fetch_funding_rate_raw(instId: str) -> float:
    """抓取資金費率原始浮點值（用於過濾判斷）"""
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5
        ).json()
        return float(res['data'][0]['fundingRate'])
    except Exception as e:
        logging.warning(f"[{instId}] 資金費率原始值抓取失敗: {e}")
        return 0.0  # 抓不到時不過濾

def is_trending_market(df: pd.DataFrame) -> bool:
    """
    盤整過濾：當前 ATR(14) 必須高於近 50 根均 ATR × 0.7。
    ATR 太小代表市場在盤整，SMC 訊號在此環境下失真率高。
    """
    if len(df) < 50:
        return True  # 資料不足，不過濾
    high_low   = df['h'] - df['l']
    high_close = np.abs(df['h'] - df['c'].shift())
    low_close  = np.abs(df['l'] - df['c'].shift())
    tr         = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    current_atr = tr.rolling(14).mean().iloc[-1]
    avg_atr_50  = tr.tail(50).mean()
    return current_atr > avg_atr_50 * 0.7

def get_btc_direction(btc_df: pd.DataFrame, lookback: int = 5) -> str:
    """
    BTC 近期方向判斷：
    近 N 根 K 棒中 4 根以上為陰線 → DOWN
    近 N 根 K 棒中 4 根以上為陽線 → UP
    否則 → NEUTRAL
    """
    if btc_df is None or len(btc_df) < lookback:
        return "NEUTRAL"
    recent  = btc_df.tail(lookback)
    bearish = int((recent['c'] < recent['o']).sum())
    bullish = lookback - bearish
    if bearish >= 4: return "DOWN"
    if bullish >= 4: return "UP"
    return "NEUTRAL"

def classify_trade(side: str, structure: str, risk_pct: float) -> str:
    """
    自動判斷短單/長單：
    • 反轉結構（W底/M頭）→ 波段長單，等待更大空間
    • 趨勢延續 + 小 risk → 日內短單，快進快出
    """
    if "反轉" in structure:
        return "📊 長單 (波段)"
    elif risk_pct < 1.0:
        return "⚡ 短單 (日內)"
    return "📊 長單 (波段)"


# ─────────────────────────────────────────────
# 6. SMC 訊號掃描（整合所有分析）
# ─────────────────────────────────────────────

def find_smc_setup(df: pd.DataFrame) -> dict | None:
    """
    完整 SMC 掃描流程：
    1. BOS / CHoCH 結構突破偵測
    2. 結構性止損（OB / FVG 邊緣）
    3. 流動性導向止盈（擺動高低點）
    4. W底 / M頭市場結構識別
    5. CVD 買賣壓估算
    6. 槓桿建議 + 短/長單分類
    """
    if df is None or len(df) < 40:
        return None

    atr  = calculate_atr(df)
    best = None

    # 掃描最近 25 根 K 棒，取最新符合的 BOS 訊號
    for i in range(len(df) - 3, len(df) - 25, -1):
        k0, k1, k2 = df.iloc[i - 1], df.iloc[i], df.iloc[i + 1]

        # 多頭 BOS：K2 突破前 15 根高點且為陽線
        if k2['c'] > k2['o'] and k2['c'] > df['h'].iloc[i - 15:i].max():
            # 進場位改用 FVG 上緣（k2['l']）：最靠近當前價格，只需小幅回踩即可成交
            # 若無 FVG 則用 k1 收盤（BOS 前最後一根 K 棒收盤，同樣比中點更容易被觸及）
            entry = k2['l'] if k2['l'] > k0['h'] else k1['c']
            best  = {"side": "LONG", "entry": entry}

        # 空頭 BOS：K2 跌破前 15 根低點且為陰線
        elif k2['c'] < k2['o'] and k2['c'] < df['l'].iloc[i - 15:i].min():
            # 空頭同理：用 FVG 下緣（k2['h']）或 k1 收盤
            entry = k2['h'] if k2['h'] < k0['l'] else k1['c']
            best  = {"side": "SHORT", "entry": entry}

    if best is None:
        return None

    side  = best['side']
    entry = best['entry']
    price = df['c'].iloc[-1]

    # 結構性止損
    sl = calculate_structural_sl(df, side, entry, atr)

    # 固定 R 倍數止盈：TP1=1R, TP2=2R, TP3=3R
    tp1, tp2, tp3 = get_fixed_r_tps(entry, sl, side)

    # 各項分析
    risk          = abs(entry - sl) + 1e-10
    risk_pct      = risk / (entry + 1e-10) * 100
    structure     = detect_market_structure(df)
    lev, lev_note = suggest_leverage(atr, price)
    trade_type    = classify_trade(side, structure, risk_pct)
    _, cvd_label  = calculate_cvd(df)

    # Supertrend 方向
    st_val   = calculate_supertrend(df)
    st_label = "📈 多頭" if st_val == 1 else ("📉 空頭" if st_val == -1 else "⚪ 未知")

    return {
        "side":          side,
        "entry":         entry,
        "sl":            sl,
        "tp1":           tp1,
        "tp2":           tp2,
        "tp3":           tp3,
        "r1":            1.0,
        "r2":            2.0,
        "r3":            3.0,
        "structure":     structure,
        "leverage":      lev,
        "leverage_note": lev_note,
        "trade_type":    trade_type,
        "cvd_label":     cvd_label,
        "st_val":        st_val,
        "st_label":      st_label,
    }


# ─────────────────────────────────────────────
# 7. 主程式
# ─────────────────────────────────────────────

def main():
    try:
        now_tw        = datetime.utcnow() + timedelta(hours=8)
        manual_report = os.getenv("MANUAL_REPORT", "false").lower() == "true"

        # 檔案初始化
        for f, cols in zip([LOG_FILE, STATS_FILE], [LOG_COLS, STATS_COLS]):
            if not os.path.exists(f) or os.stat(f).st_size == 0:
                pd.DataFrame(columns=cols).to_csv(f, index=False)

        # ── A. 戰績回報（午夜 00:00 或手動觸發）────────────────────────
        is_midnight = (now_tw.hour == 0 and 0 <= now_tw.minute < 15)
        if is_midnight or manual_report:
            if not os.path.exists("midnight.ok") or manual_report:
                df_s = pd.read_csv(STATS_FILE)
                if not df_s.empty:
                    tp_c  = len(df_s[df_s['result'] == 'TP'])   # 含保本
                    sl_c  = len(df_s[df_s['result'] == 'SL'])
                    be_c  = len(df_s[df_s['result'] == 'BE'])   # 額外保本細項（若有）
                    total = tp_c + sl_c
                    wr    = (tp_c / total * 100) if total > 0 else 0
                    date_str = (now_tw - timedelta(days=1)).strftime('%Y-%m-%d')
                    send_tg(
                        f"📊 *Alpha Oracle 每日戰績*\n"
                        f"──────────────────\n"
                        f"📅 日期：{date_str}\n"
                        f"\n"
                        f"✅ 盈利（含保本）：{tp_c} 單\n"
                        f"❌ 止損：{sl_c} 單\n"
                        f"📊 總計：{total} 單\n"
                        f"\n"
                        f"🔥 勝率：*{wr:.1f}%*\n"
                        f"──────────────────\n"
                        f"📌 保本亦計為獲勝"
                    )
                    if is_midnight:
                        pd.DataFrame(columns=STATS_COLS).to_csv(STATS_FILE, index=False)
                        with open("midnight.ok", "w") as fh: fh.write("ok")
        elif now_tw.hour != 0 and os.path.exists("midnight.ok"):
            os.remove("midnight.ok")

        # ── B. 核心監控邏輯 ─────────────────────────────────────────────
        try:
            trades_df = pd.read_csv(LOG_FILE)
            if "wait_since" not in trades_df.columns:
                trades_df["wait_since"] = 0
            if "tp1_hit" not in trades_df.columns:
                trades_df["tp1_hit"] = 0
        except Exception:
            trades_df = pd.DataFrame(columns=LOG_COLS)

        active_ids     = trades_df['instId'].tolist()
        updated_trades = []
        current_bar    = int(datetime.utcnow().timestamp() // 900)  # 15m bar index

        # 過濾器 ③ 前置：先抓 BTC 方向（整個迴圈只需抓一次）
        btc_df    = fetch_okx("BTC-USDT-SWAP")
        btc_trend = get_btc_direction(btc_df)
        logging.info(f"BTC 當前方向：{btc_trend}")

        # 計算目前 ACTIVE + WAITING 的倉位數（用於上限判斷）
        current_positions = len(trades_df)
        logging.info(f"目前倉位數：{current_positions} / {OKX_MAX_POSITIONS}")

        for instId in ALL_COINS:
            df = fetch_okx(instId)
            if df is None or df.empty:
                time.sleep(0.2)
                continue

            curr_p   = df['c'].iloc[-1]
            coin_sym = instId.split('-')[0]

            # ── 1. 發現新機會 ───────────────────────────────────────────
            if instId not in active_ids:

                # ── 倉位上限檢查 ──────────────────────────────────────
                # 含 WAITING 計入上限，避免掛太多等待單佔滿 10 個位子
                if current_positions >= OKX_MAX_POSITIONS:
                    logging.info(f"[{instId}] 已達倉位上限 {OKX_MAX_POSITIONS}，跳過新訊號")
                    time.sleep(0.2)
                    continue

                # 過濾器 ①：盤整市場 — ATR 不足時跳過，避免假突破
                if not is_trending_market(df):
                    logging.info(f"[{instId}] 盤整市場，跳過")
                    time.sleep(0.2)
                    continue

                setup = find_smc_setup(df)
                if setup:

                    # 過濾器 ②：CVD 方向與訊號一致性
                    # 大戶出貨（CVD-）不做多；大戶吸籌（CVD+）不做空
                    cvd_val, _ = calculate_cvd(df)
                    if setup['side'] == "LONG" and cvd_val < 0:
                        logging.info(f"[{instId}] CVD 負值（大戶出貨），多頭訊號跳過")
                        time.sleep(0.2)
                        continue
                    if setup['side'] == "SHORT" and cvd_val > 0:
                        logging.info(f"[{instId}] CVD 正值（大戶吸籌），空頭訊號跳過")
                        time.sleep(0.2)
                        continue

                    # 過濾器 ③：資金費率極端值
                    # 資費 > +0.05% 代表多頭過熱，不追多；< -0.05% 代表空頭過熱，不追空
                    fr = fetch_funding_rate_raw(instId)
                    if setup['side'] == "LONG" and fr > 0.0005:
                        logging.info(f"[{instId}] 資費過高 ({fr*100:.4f}%)，多頭過熱，跳過")
                        time.sleep(0.2)
                        continue
                    if setup['side'] == "SHORT" and fr < -0.0005:
                        logging.info(f"[{instId}] 資費過低 ({fr*100:.4f}%)，空頭過熱，跳過")
                        time.sleep(0.2)
                        continue

                    # 過濾器 ③：BTC 方向（山寨幣專用，BTC 本身不限制）
                    # BTC 下跌中不做山寨多頭；BTC 上漲中不做山寨空頭
                    if instId != "BTC-USDT-SWAP":
                        if setup['side'] == "LONG" and btc_trend == "DOWN":
                            logging.info(f"[{instId}] BTC 下跌中，山寨多頭跳過")
                            time.sleep(0.2)
                            continue
                        if setup['side'] == "SHORT" and btc_trend == "UP":
                            logging.info(f"[{instId}] BTC 上漲中，山寨空頭跳過")
                            time.sleep(0.2)
                            continue

                    # 過濾器 ④：Supertrend 方向一致性
                    # Supertrend 空頭（-1）時不做多；多頭（1）時不做空
                    # st_val==0 表示資料不足，放行不過濾
                    if setup['st_val'] == -1 and setup['side'] == "LONG":
                        logging.info(f"[{instId}] Supertrend 空頭，多頭訊號跳過")
                        time.sleep(0.2)
                        continue
                    if setup['st_val'] == 1 and setup['side'] == "SHORT":
                        logging.info(f"[{instId}] Supertrend 多頭，空頭訊號跳過")
                        time.sleep(0.2)
                        continue

                    funding, ls_ratio = get_funding_ls(instId)
                    side_zh = "🟢 多單 (LONG)" if setup['side'] == "LONG" else "🔴 空單 (SHORT)"

                    msg  = f"🔥 *Alpha Oracle 訊號發射* 🔥\n"
                    msg += f"──────────────────\n"
                    msg += f"💎 幣種：#{coin_sym}\n"
                    msg += f"🎯 方向：{side_zh}\n"
                    msg += f"⏰ 週期：15m\n"
                    msg += f"📊 數據：多空比 {ls_ratio} | 資費 {funding} | {setup['cvd_label']}\n"
                    msg += f"\n"
                    msg += f"📍 進場位：{setup['entry']:.4f}\n"
                    msg += f"🚫 止損位：{setup['sl']:.4f}  (-1R)\n"
                    msg += f"💰 TP1 (1.0R)：{setup['tp1']:.4f}\n"
                    msg += f"💰 TP2 (2.0R)：{setup['tp2']:.4f}\n"
                    msg += f"💰 TP3 (3.0R)：{setup['tp3']:.4f}\n"
                    msg += f"\n"
                    msg += f"🏗️ 結構：{setup['structure']}\n"
                    msg += f"📡 Supertrend：{setup['st_label']}\n"
                    msg += f"🕹️ 槓桿：{setup['leverage']} ({setup['leverage_note']})\n"
                    msg += f"📌 類型：{setup['trade_type']}\n"
                    msg += f"\n"
                    msg += f"💡 *等待回踩成交...*"
                    send_tg(msg)

                    updated_trades.append({
                        "instId":     instId,
                        "side":       setup['side'],
                        "status":     "WAITING",
                        "entry":      setup['entry'],
                        "sl":         setup['sl'],
                        "tp1":        setup['tp1'],
                        "tp2":        setup['tp2'],
                        "tp3":        setup['tp3'],
                        "locked":     0,
                        "wait_since": current_bar,
                        "tp1_hit":    0,
                        "ord_id":     "",
                        "algo_id":    "",
                    })
                    current_positions += 1  # 即時更新，避免同一輪開過多單
                time.sleep(0.2)
                continue

            # ── 2. 追蹤現有單據 ─────────────────────────────────────────
            t = normalize_trade(trades_df[trades_df['instId'] == instId].iloc[0].to_dict())

            # WAITING 狀態
            if t['status'] == "WAITING":
                bars_waited = current_bar - t['wait_since']
                if bars_waited > WAITING_EXPIRY_BARS:
                    logging.info(f"[{instId}] WAITING 逾 {bars_waited} bars，自動清除")
                    time.sleep(0.2)
                    continue

                # 進場觸發：四層防漏偵測
                # ① 最近 8 根已收盤 K 棒（擴大回溯，防止 bot 在錯誤時間點執行而漏單）
                # ② 當前未收盤 K 棒的最高/最低（防止進場位在本根 K 棒內觸及但尚未確認）
                # ③ 當前收盤價（curr_p）直接補充判斷：
                #    API 失敗導致 cur_low=inf 時仍可透過 curr_p 觸發進場
                n_check      = min(8, len(df))
                cur_low, cur_high = fetch_current_candle_hl(instId)
                check_low    = min(df['l'].iloc[-n_check:].min(), cur_low, curr_p)
                check_high   = max(df['h'].iloc[-n_check:].max(), cur_high, curr_p)
                is_hit = (
                    (t['side'] == "LONG"  and check_low  <= t['entry']) or
                    (t['side'] == "SHORT" and check_high >= t['entry'])
                )

                # 進場保護：若當前收盤價已突破止損，
                # 代表價格直接穿過 entry+SL，不應進場，直接清除此單
                already_sl = (
                    (t['side'] == "LONG"  and curr_p < t['sl']) or
                    (t['side'] == "SHORT" and curr_p > t['sl'])
                )
                if is_hit and already_sl:
                    logging.info(f"[{instId}] 進場位已觸及但當前價已穿破止損，放棄此單")
                    time.sleep(0.2)
                    continue  # 不加入 updated_trades，直接清除

                if is_hit:
                    t['status'] = "ACTIVE"
                    fill_price  = t['entry']
                    side_zh     = "🟢 多單 (LONG)" if t['side'] == "LONG" else "🔴 空單 (SHORT)"

                    # ── 自動下單 ─────────────────────────────────────────
                    auto_tag = ""
                    if AUTO_TRADE and OKX_API_KEY:
                        demo_str = "🔬 模擬倉" if OKX_DEMO else "💵 實盤"
                        # ① 計算下單張數（風險比例 or 固定張數）
                        sz = okx_calc_size(instId, t['entry'], t['sl'])
                        # ② 設定槓桿
                        okx_set_leverage(instId, OKX_LEVER)
                        # ③ 市價進場
                        ord_id  = okx_place_entry(instId, t['side'], sz)
                        t['ord_id'] = ord_id
                        # ④ 設止損 Algo（OKX 端保護，bot 故障也能止損）
                        algo_id = okx_place_sl_algo(instId, t['side'], t['sl'], sz)
                        t['algo_id'] = algo_id
                        sz_mode = (f"風險{OKX_RISK_PCT}%" if OKX_RISK_PCT > 0
                                   else f"固定{sz}張")
                        auto_tag = (
                            f"\n\n🤖 *自動下單已執行 ({demo_str})*\n"
                            f"📦 下單：{sz} 張 ({sz_mode})\n"
                            f"🔧 槓桿：{OKX_LEVER}x | {OKX_TD_MODE}"
                        )
                    # ─────────────────────────────────────────────────────

                    send_tg(
                        f"🚀 *Alpha Oracle | 進場成交* 🚀\n"
                        f"──────────────────\n"
                        f"💎 幣種：#{coin_sym}\n"
                        f"🎯 方向：{side_zh}\n"
                        f"\n"
                        f"📍 成交價：{fill_price:.4f}\n"
                        f"🚫 止損位：{t['sl']:.4f}  (-1R)\n"
                        f"💰 TP1 (1.0R)：{t['tp1']:.4f}\n"
                        f"💰 TP2 (2.0R)：{t['tp2']:.4f}\n"
                        f"💰 TP3 (3.0R)：{t['tp3']:.4f}\n"
                        f"{auto_tag}"
                    )
                updated_trades.append(t)

            # ACTIVE 狀態
            elif t['status'] == "ACTIVE":

                risk_r = abs(t['entry'] - t['sl']) + 1e-10

                # 嚴格執行：用最近 3 根 K 棒高低點 + 當前未收盤 K 棒判斷 TP/SL
                # 避免只盯收盤價而漏掉在同一根 K 棒內觸及的 TP 或 SL
                act_n          = min(3, len(df))
                act_cur_lo, act_cur_hi = fetch_current_candle_hl(instId)
                act_low  = min(df['l'].iloc[-act_n:].min(), act_cur_lo, curr_p)
                act_high = max(df['h'].iloc[-act_n:].max(), act_cur_hi, curr_p)

                # 達到 TP1 → 通知 + 移動止損到保本（進場位）
                if t['tp1_hit'] == 0 and (
                    (t['side'] == "LONG"  and act_high >= t['tp1']) or
                    (t['side'] == "SHORT" and act_low  <= t['tp1'])
                ):
                    t['tp1_hit'] = 1
                    be_tag = ""
                    if AUTO_TRADE and OKX_API_KEY:
                        # 取消舊止損 Algo，重新設在進場位（保本）
                        sz_be   = okx_calc_size(instId, t['entry'], t['sl'])
                        okx_cancel_algo(instId, t.get('algo_id', ''))
                        new_sl  = t['entry']
                        new_aid = okx_place_sl_algo(instId, t['side'], new_sl, sz_be)
                        t['algo_id'] = new_aid
                        t['sl']      = new_sl
                        be_tag = f"\n🤖 *止損已自動移至進場位（保本）*"
                    send_tg(
                        f"🎯 *Alpha Oracle | 達到 TP1*\n"
                        f"──────────────────\n"
                        f"💎 幣種：#{coin_sym}\n"
                        f"✅ 已觸及第一止盈位\n"
                        f"\n"
                        f"📍 當前價：{curr_p:.4f}\n"
                        f"💰 TP1 (1.0R)：{t['tp1']:.4f}  ✅\n"
                        f"💰 TP2 (2.0R)：{t['tp2']:.4f}\n"
                        f"💰 TP3 (3.0R)：{t['tp3']:.4f}\n"
                        f"🚫 止損移至保本：{t['sl']:.4f}"
                        f"{be_tag}"
                    )

                # 達到 TP2 → 移動止損到 TP1（鎖利）
                if t['locked'] == 0 and (
                    (t['side'] == "LONG"  and act_high >= t['tp2']) or
                    (t['side'] == "SHORT" and act_low  <= t['tp2'])
                ):
                    t['locked'] = 1
                    t['sl']     = t['tp1']
                    lock_tag = ""
                    if AUTO_TRADE and OKX_API_KEY:
                        sz_lk   = okx_calc_size(instId, t['entry'], t['sl'])
                        okx_cancel_algo(instId, t.get('algo_id', ''))
                        new_aid = okx_place_sl_algo(instId, t['side'], t['tp1'], sz_lk)
                        t['algo_id'] = new_aid
                        lock_tag = f"\n🤖 *止損已自動移至 TP1*"
                    send_tg(
                        f"🔒 *Alpha Oracle | 達到 TP2 · 鎖利保護*\n"
                        f"──────────────────\n"
                        f"💎 幣種：#{coin_sym}\n"
                        f"✅ 已達 TP2，止損上移鎖利\n"
                        f"\n"
                        f"📍 當前價：{curr_p:.4f}\n"
                        f"🚫 新止損：{t['tp1']:.4f}（TP1 位置 · 鎖利）\n"
                        f"💰 TP3 (3.0R)：{t['tp3']:.4f}"
                        f"{lock_tag}"
                    )

                # SL 觸發：用 K 棒低點（多單）/ 高點（空單）判斷，嚴格執行
                is_sl  = (
                    (t['side'] == "LONG"  and act_low  <= t['sl']) or
                    (t['side'] == "SHORT" and act_high >= t['sl'])
                )
                # TP3 觸發：同上，用高低點判斷
                is_tp3 = (
                    (t['side'] == "LONG"  and act_high >= t['tp3']) or
                    (t['side'] == "SHORT" and act_low  <= t['tp3'])
                )

                if is_sl or is_tp3:
                    is_breakeven = is_sl and t['locked'] == 1
                    res          = "SL" if (is_sl and not is_breakeven) else "TP"
                    if is_tp3:
                        result_label = "💰 止盈達標 (TP3)"
                        exit_p       = t['tp3']
                    elif is_breakeven:
                        result_label = "🔒 保本出場 (Break Even)"
                        exit_p       = t['tp1']
                    else:
                        result_label = "❌ 止損離場"
                        exit_p       = t['sl']

                    # ── 自動平倉 ──────────────────────────────────────
                    auto_close_tag = ""
                    if AUTO_TRADE and OKX_API_KEY:
                        sz_cl = okx_calc_size(instId, t['entry'], t['sl'])
                        if is_tp3:
                            okx_cancel_algo(instId, t.get('algo_id', ''))
                            okx_close_market(instId, t['side'], sz_cl)
                            auto_close_tag = "\n🤖 *已自動市價平倉 (TP3)*"
                        else:
                            okx_cancel_algo(instId, t.get('algo_id', ''))
                            auto_close_tag = "\n🤖 *止損 Algo 已執行平倉*"
                    # ─────────────────────────────────────────────────

                    send_tg(
                        f"🏁 *Alpha Oracle | 交易結算*\n"
                        f"──────────────────\n"
                        f"💎 幣種：#{coin_sym}\n"
                        f"🏆 結果：{result_label}\n"
                        f"\n"
                        f"📍 離場價：{exit_p:.4f}\n"
                        f"🚫 止損位：{t['sl']:.4f}\n"
                        f"💰 TP1 (1.0R)：{t['tp1']:.4f}\n"
                        f"💰 TP2 (2.0R)：{t['tp2']:.4f}\n"
                        f"💰 TP3 (3.0R)：{t['tp3']:.4f}"
                        f"{auto_close_tag}"
                    )
                    pd.DataFrame([{"instId": instId, "result": res}]).to_csv(
                        STATS_FILE, mode='a', header=False, index=False
                    )
                    time.sleep(0.2)
                    continue

                updated_trades.append(t)

            time.sleep(0.2)  # rate limit 保護

        pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)

    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    main()
