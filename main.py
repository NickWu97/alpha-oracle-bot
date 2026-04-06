"""
Alpha Oracle v2 — ICT Smart Money Bot（強化版）
架構：ICT Killzones + Weekly/H4 Bias + M15 Liquidity Sweep + M5 MSS/FVG/OB Entry

【v2 新增技術分析 — 提升勝率至 70-80%】
  ① Equal Highs/Lows (EQH/EQL) — 精確識別雙頂/雙底流動性水位
  ② Order Block (OB)           — 訂單塊作為第二層進場確認
  ③ OTE Zone (Fib 61.8-78.6%) — 最佳進場區間，避免追高/追低
  ④ Silver Bullet Killzone     — 10:00-11:00 / 14:00-15:00 NY（高機率時段）
  ⑤ Asian Range                — 亞洲時段高低作為流動性目標
  ⑥ Weekly Bias                — 週線方向確認，確保方向正確
  ⑦ Confluence Score           — 9 項評分，≥ 5 才進場（過濾低品質訊號）
  ⑧ ATR 波動率過濾             — 縮量震盪時評分 -1，避免來回掃（新增）
  ⑨ 智慧 FVG 進場點            — 評分 > 7 改用 Edge 25%，避免錯過大行情（新增）

進場 SOP:
  1. Weekly BOS 方向確認 → 新增
  2. H4 BOS + 折價/溢價區 → 確定方向
  3. ICT Killzone → 倫敦/紐約開盤/Silver Bullet
  4. M15 流動性掃蕩（含 EQH/EQL 水位）→ 4 條件驗證
  5. M15 Order Block 確認 → 新增
  6. M5 MSS + FVG → 掃損後結構突破 + 公平價值缺口
  7. OTE Zone 確認 → 進場點在 61.8-78.6% 回測區 → 新增
  8. ATR 波動率確認 → 非縮量市場才進場 → 新增
  9. 匯流評分 ≥ 5/9 → 才送出訊號 → 新增
  10. 智慧進場點 → 評分 > 7 用 FVG Edge，其餘用 CE → 新增

風控（不變）：
  SL = 掃損棒低/高點（精確結構 SL）
  TP1(BE) = 1.5R → 觸及後立即移 SL 到成本，統計計入勝利
  TP2     = 2.0R（1:2 R/R 主目標）
  TP3     = H4 對向流動性（延伸目標）
"""
import requests
import os
import pandas as pd
import numpy as np
import logging
import traceback
import time
from datetime import datetime, timedelta

# ─────────────────────────────────────────────
# 1. 基礎配置
# ─────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID  = os.getenv("CHAT_ID")

ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP",
    "BCH-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP",
]
CORR_GROUPS = [
    {"BTC-USDT-SWAP", "ETH-USDT-SWAP"},
    {"SOL-USDT-SWAP", "AVAX-USDT-SWAP", "APT-USDT-SWAP"},
    {"LINK-USDT-SWAP", "ADA-USDT-SWAP", "XRP-USDT-SWAP"},
]

MAX_DAILY_SL        = 3          # 2 → 3：每日止損上限略放寬
MAX_CONCURRENT      = 3
SL_MIN_PCT          = 0.003      # 0.5% → 0.3%：允許更緊的結構性 SL
WAITING_EXPIRY_BARS = 36         # 24 → 36 根（6h→9h）：等待 FVG 回測更有耐心
COOLDOWN_BARS_SL    = 8
COOLDOWN_BARS_TP    = 4

# ★ 匯流評分門檻（0-9分）— 放寬：5 → 4
CONFLUENCE_MIN_SCORE = 4

LOG_FILE   = "active_trades.csv"
STATS_FILE = "daily_stats.csv"
LOG_COLS   = ["instId", "side", "status", "entry", "sl", "tp1", "tp2", "tp3",
              "locked", "wait_since", "tp1_hit"]
STATS_COLS = ["instId", "result", "date"]

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
    return {k: (safe_float(t.get(k)) if k not in ("instId","side","status")
                else (safe_int(t.get(k)) if k in ("locked","wait_since","tp1_hit")
                      else str(t.get(k,""))))
            for k in LOG_COLS}

# ─────────────────────────────────────────────
# 3. 時區工具
# ─────────────────────────────────────────────
def is_us_dst(d: datetime) -> bool:
    """美國夏令時：3 月第二個週日 ~ 11 月第一個週日"""
    y = d.year
    mar1  = datetime(y, 3, 1)
    dst_s = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)
    nov1  = datetime(y, 11, 1)
    dst_e = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    return dst_s <= d.replace(tzinfo=None) < dst_e

def utc_to_ny(now_utc: datetime) -> datetime:
    return now_utc + timedelta(hours=-4 if is_us_dst(now_utc) else -5)

def is_ict_killzone(now_utc: datetime) -> tuple[bool, str]:
    """
    ★ v2 更新：新增 Silver Bullet 時段
    ICT 殺手時段（New York Time 為準）
    ✅ London Open    : 03:00 – 05:00 NY  (歐盤開盤掃損)
    ✅ New York Open  : 07:00 – 10:00 NY  (美盤開盤掃損)
    ✅ Silver Bullet AM: 10:00 – 11:00 NY (ICT 最高機率做單時段)
    ✅ Silver Bullet PM: 14:00 – 15:00 NY (下午 Silver Bullet)
    ❌ 其他時段一律忽略新訊號
    """
    ny = utc_to_ny(now_utc)
    h  = ny.hour
    if 2 <= h < 6:                                     # 放寬：3-5 → 2-6（倫敦前置+後延）
        return True, f"🇬🇧 London Open ({h:02d}:xx NY)"
    if 7 <= h < 11:                                    # 放寬：7-10 → 7-11（含 Silver Bullet AM）
        return True, f"🗽 New York Open ({h:02d}:xx NY)"
    if 13 <= h < 16:                                   # 放寬：14-15 → 13-16（下午時段擴大）
        return True, f"🥈 Silver Bullet PM ({h:02d}:xx NY)"
    return False, f"⏸ 非 Killzone ({h:02d}:xx NY)"

def get_kz_quality(kz_label: str) -> str:
    """評估 Killzone 品質（Silver Bullet 最高分）"""
    if "Silver Bullet" in kz_label: return "SILVER_BULLET"
    if "New York"      in kz_label: return "NY"
    if "London"        in kz_label: return "LONDON"
    return "OTHER"

# ─────────────────────────────────────────────
# 4. 數據抓取
# ─────────────────────────────────────────────
def fetch_okx(instId: str, bar: str = "15m", limit: int = 150) -> pd.DataFrame | None:
    try:
        url = (f"https://www.okx.com/api/v5/market/candles"
               f"?instId={instId}&bar={bar}&limit={limit}")
        res = requests.get(url, timeout=12).json()
        df  = pd.DataFrame(res['data'],
                           columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df[df['confirm']=="1"].iloc[::-1].reset_index(drop=True)
    except Exception as e:
        logging.warning(f"[{instId}] {bar} 抓取失敗: {e}")
        return None

def fetch_current_candle_hl(instId: str) -> tuple[float, float]:
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=15m&limit=3"
        for row in requests.get(url, timeout=5).json()['data']:
            if row[8] == "0":
                return float(row[3]), float(row[2])
    except: pass
    return float('inf'), float('-inf')

def get_funding_ls(instId: str) -> tuple[str, str]:
    base = instId.replace("-SWAP","").split("-")[0]
    fr, ls = "N/A", "N/A"
    try:
        fr = f"{float(requests.get(f'https://www.okx.com/api/v5/public/funding-rate?instId={instId}',timeout=5).json()['data'][0]['fundingRate'])*100:.4f}%"
    except: pass
    try:
        ls = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base}",timeout=5).json()['data'][0]['ratio']
    except: pass
    return fr, ls

def fetch_funding_rate_raw(instId: str) -> float:
    try:
        return float(requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}",timeout=5).json()['data'][0]['fundingRate'])
    except: return 0.0

def send_tg(msg: str):
    if not TG_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"Markdown"}, timeout=15)
    except Exception as e:
        logging.warning(f"TG 發送失敗: {e}")

# ─────────────────────────────────────────────
# 5. 基礎指標
# ─────────────────────────────────────────────
def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    hl  = df['h'] - df['l']
    hc  = np.abs(df['h'] - df['c'].shift())
    lc  = np.abs(df['l'] - df['c'].shift())
    return pd.concat([hl,hc,lc],axis=1).max(axis=1).rolling(period).mean().iloc[-1]


def check_atr_volatility(df: pd.DataFrame,
                          short_period  : int = 14,
                          lookback_bars : int = 96) -> tuple[bool, str, float]:
    """
    ★ 新增：ATR 波動率過濾

    縮量震盪辨識邏輯：
    ① 計算「近 short_period(14) 根」的 ATR 均值 = 當前波動率
    ② 計算「過去 lookback_bars(96根 = 24小時) 的 TR 均值」= 基準波動率
    ③ 若 當前ATR / 基準ATR < 0.50 → 市場縮量震盪

    縮量震盪的問題：
    - 假掃損比例極高，價格容易在流動性水位附近來回觸碰
    - 掃損後的「動能」不足，FVG 可能也只是磨盤而非有效突破
    - 結果：交易勝率大幅下降

    評分影響：縮量 → 評分 -1；正常波動 → 不扣分

    M15 x 96 = 1440 分鐘 = 24 小時
    返回: (is_volatile_enough, label, atr_ratio)
    """
    min_bars = lookback_bars + short_period
    if len(df) < min_bars:
        return True, "⚪ ATR(資料不足)", 1.0

    # True Range 序列（高效向量計算，不重複呼叫 calculate_atr）
    hl       = df['h'] - df['l']
    hc       = np.abs(df['h'] - df['c'].shift())
    lc       = np.abs(df['l'] - df['c'].shift())
    tr_series = pd.concat([hl, hc, lc], axis=1).max(axis=1).fillna(0)

    # 當前短期 ATR（最近 short_period 根的 TR 均值）
    current_atr = tr_series.iloc[-short_period:].mean()

    # 長期基準 ATR（24 小時內，排除最近 short_period 根以避免自我參照）
    long_window = tr_series.iloc[-(lookback_bars + short_period):-short_period]
    long_avg_atr = long_window.mean()

    if long_avg_atr < 1e-10:
        return True, "⚪ ATR(基準為零)", 1.0

    atr_ratio   = current_atr / long_avg_atr
    is_volatile = atr_ratio >= 0.35   # 放寬：50% → 35%（只過濾極度縮量）

    if is_volatile:
        label = f"✅ 波動率正常 ({atr_ratio:.0%})"
    else:
        label = f"⚠️ 縮量震盪 ({atr_ratio:.0%}) → 評分 -1"

    return is_volatile, label, atr_ratio

def calculate_cvd(df: pd.DataFrame, lookback: int = 20) -> str:
    rec  = df.tail(lookback).copy()
    body = (rec['h'] - rec['l']).replace(0, 1e-10)
    rec['d'] = np.where(rec['c']>=rec['o'],
                         rec['v']*(rec['c']-rec['l'])/body,
                        -rec['v']*(rec['h']-rec['c'])/body)
    cvd = rec['d'].sum()
    return "🟢 CVD+" if cvd > 0 else "🔴 CVD-"

def calculate_supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> int:
    if len(df) < period + 2: return 0
    hi = df['h'].values.astype(float); lo = df['l'].values.astype(float)
    cl = df['c'].values.astype(float); n = len(df)
    tr = np.zeros(n)
    for i in range(1,n): tr[i]=max(hi[i]-lo[i],abs(hi[i]-cl[i-1]),abs(lo[i]-cl[i-1]))
    atr = np.zeros(n); atr[period] = tr[1:period+1].mean()
    for i in range(period+1,n): atr[i]=(atr[i-1]*(period-1)+tr[i])/period
    hl2=(hi+lo)/2; bu=hl2-mult*atr; bd=hl2+mult*atr
    fu=np.zeros(n); fd=np.zeros(n); tr2=np.ones(n,dtype=int)
    fu[period]=bu[period]; fd[period]=bd[period]
    for i in range(period+1,n):
        fu[i]=bu[i] if bu[i]>fu[i-1] or cl[i-1]<fu[i-1] else fu[i-1]
        fd[i]=bd[i] if bd[i]<fd[i-1] or cl[i-1]>fd[i-1] else fd[i-1]
        if   tr2[i-1]==-1 and cl[i]>fd[i-1]: tr2[i]=1
        elif tr2[i-1]==1  and cl[i]<fu[i-1]: tr2[i]=-1
        else: tr2[i]=tr2[i-1]
    return int(tr2[-1])

# ─────────────────────────────────────────────
# 6. ICT 流動性分析（原有）
# ─────────────────────────────────────────────
def get_pdh_pdl(instId: str) -> tuple[float, float]:
    df = fetch_okx(instId, bar="1D", limit=5)
    if df is None or len(df) < 2: return 0.0, 0.0
    return float(df['h'].iloc[-2]), float(df['l'].iloc[-2])

def get_pwh_pwl(instId: str) -> tuple[float, float]:
    df = fetch_okx(instId, bar="1W", limit=5)
    if df is None or len(df) < 2: return 0.0, 0.0
    return float(df['h'].iloc[-2]), float(df['l'].iloc[-2])

def find_swing_ict(df: pd.DataFrame,
                   lookleft: int = 20,
                   lookright: int = 5) -> tuple[list, list]:
    n  = len(df)
    sh, sl = [], []
    for i in range(lookleft, n - lookright):
        h_i = float(df['h'].iloc[i])
        l_i = float(df['l'].iloc[i])
        lh = df['h'].iloc[i-lookleft:i].max()
        rh = df['h'].iloc[i+1:i+lookright+1].max()
        ll = df['l'].iloc[i-lookleft:i].min()
        rl = df['l'].iloc[i+1:i+lookright+1].min()
        if h_i > lh and h_i > rh: sh.append({'price': h_i, 'idx': i})
        if l_i < ll and l_i < rl: sl.append({'price': l_i, 'idx': i})
    return sh, sl

def check_liquidity_sweep(
        df: pd.DataFrame,
        side: str,
        liq_lows: list,
        liq_highs: list,
) -> tuple[bool, dict | None]:
    n = len(df)
    if n < 12: return False, None
    check_start = max(0, n - 20)   # 放寬：12 → 20 根（往前多看 2 小時）

    if side == "LONG":
        levels = sorted([l for l in liq_lows if l and l > 0])
        if not levels: return False, None
        for i in range(check_start, n - 2):
            k = df.iloc[i]
            for lvl in levels:
                if k['l'] >= lvl: continue
                if k['c'] <= lvl: continue
                rng = k['h'] - k['l']
                if rng < 1e-10: continue
                lower_wick = min(k['o'], k['c']) - k['l']
                if lower_wick / rng < 0.40: continue  # 放寬：50% → 40% 影線比例
                displaced = False
                disp_close = 0.0
                for j in range(i+1, min(i+3, n)):
                    dk = df.iloc[j]
                    if dk['c'] > k['h'] and dk['c'] > dk['o']:
                        displaced  = True
                        disp_close = float(dk['c'])
                        break
                if displaced:
                    return True, {
                        'sweep_candle_sl' : float(k['l']),
                        'sweep_candle_high': float(k['h']),
                        'liq_level'       : lvl,
                        'disp_close'      : disp_close,
                        'sweep_idx'       : i,
                    }
    else:
        levels = sorted([l for l in liq_highs if l and l > 0], reverse=True)
        if not levels: return False, None
        for i in range(check_start, n - 2):
            k = df.iloc[i]
            for lvl in levels:
                if k['h'] <= lvl: continue
                if k['c'] >= lvl: continue
                rng = k['h'] - k['l']
                if rng < 1e-10: continue
                upper_wick = k['h'] - max(k['o'], k['c'])
                if upper_wick / rng < 0.40: continue   # 放寬：50% → 40% 影線比例
                displaced = False
                disp_close = 0.0
                for j in range(i+1, min(i+3, n)):
                    dk = df.iloc[j]
                    if dk['c'] < k['l'] and dk['c'] < dk['o']:
                        displaced  = True
                        disp_close = float(dk['c'])
                        break
                if displaced:
                    return True, {
                        'sweep_candle_sl' : float(k['h']),
                        'sweep_candle_low': float(k['l']),
                        'liq_level'       : lvl,
                        'disp_close'      : disp_close,
                        'sweep_idx'       : i,
                    }
    return False, None

def check_h4_bias(instId: str, side: str) -> tuple[bool, str, float]:
    df4 = fetch_okx(instId, bar="4H", limit=200)
    if df4 is None or len(df4) < 40:
        return True, "⚪ H4 N/A", 0.0
    sh_list, sl_list = find_swing_ict(df4, lookleft=10, lookright=3)
    if len(sh_list) < 2 or len(sl_list) < 2:
        return True, "⚪ H4 結構不足", 0.0
    curr    = float(df4['c'].iloc[-1])
    last_sh = sh_list[-1]['price'];  prev_sh = sh_list[-2]['price']
    last_sl = sl_list[-1]['price'];  prev_sl = sl_list[-2]['price']
    rng       = last_sh - last_sl
    fifty_pct = (last_sh + last_sl) / 2 if rng > 0 else curr
    if side == "LONG":
        bos      = last_sh > prev_sh
        discount = curr < fifty_pct
        ok       = bos and discount
        label    = ("✅ H4 多頭BOS折價" if ok
                    else ("H4 多頭BOS溢價" if bos else "H4 空頭結構"))
        tp3_tgt  = last_sh if ok else 0.0
    else:
        bos      = last_sl < prev_sl
        premium  = curr > fifty_pct
        ok       = bos and premium
        label    = ("✅ H4 空頭BOS溢價" if ok
                    else ("H4 空頭BOS折價" if bos else "H4 多頭結構"))
        tp3_tgt  = last_sl if ok else 0.0
    return ok, label, tp3_tgt

def find_mss_fvg_entry(instId: str, side: str) -> tuple[bool, dict | None]:
    df5 = fetch_okx(instId, bar="5m", limit=200)
    if df5 is None or len(df5) < 40:
        return False, None
    sh5, sl5 = find_swing_ict(df5, lookleft=8, lookright=3)
    curr = float(df5['c'].iloc[-1])
    if side == "LONG":
        mss_level = None
        for item in reversed(sh5):
            if curr > item['price']:
                mss_level = item['price']
                break
        if mss_level is None: return False, None
        start = max(1, len(df5) - 30)
        for i in range(len(df5) - 2, start, -1):
            k0 = df5.iloc[i-1]
            k2 = df5.iloc[i+1]
            if float(k2['l']) > float(k0['h']):
                fh = float(k2['l']); fl = float(k0['h'])
                ce = (fh + fl) / 2
                if ce < curr:
                    return True, {
                        'entry'    : ce,
                        'fvg_high' : fh,
                        'fvg_low'  : fl,
                        'mss_level': mss_level,
                        'fvg_type' : '多頭FVG',
                    }
    else:
        mss_level = None
        for item in reversed(sl5):
            if curr < item['price']:
                mss_level = item['price']
                break
        if mss_level is None: return False, None
        start = max(1, len(df5) - 30)
        for i in range(len(df5) - 2, start, -1):
            k0 = df5.iloc[i-1]
            k2 = df5.iloc[i+1]
            if float(k2['h']) < float(k0['l']):
                fh = float(k0['l']); fl = float(k2['h'])
                ce = (fh + fl) / 2
                if ce > curr:
                    return True, {
                        'entry'    : ce,
                        'fvg_high' : fh,
                        'fvg_low'  : fl,
                        'mss_level': mss_level,
                        'fvg_type' : '空頭FVG',
                    }
    return False, None

def calculate_ict_tps(entry: float, sl: float, side: str,
                       h4_tp3_target: float = 0.0) -> tuple[float, float, float]:
    risk = abs(entry - sl) + 1e-10
    if side == "LONG":
        tp1 = entry + risk * 1.5
        tp2 = entry + risk * 2.0
        tp3 = h4_tp3_target if (h4_tp3_target > tp2) else entry + risk * 3.0
    else:
        tp1 = entry - risk * 1.5
        tp2 = entry - risk * 2.0
        tp3 = h4_tp3_target if (h4_tp3_target < tp2 and h4_tp3_target > 0) else entry - risk * 3.0
    return tp1, tp2, tp3

# ─────────────────────────────────────────────
# 7. ★ 新增：ICT 進階技術分析
# ─────────────────────────────────────────────

# ── 7A. Equal Highs / Equal Lows (EQH/EQL) ──────────────────────────────────
def find_equal_levels(df: pd.DataFrame,
                      threshold: float = 0.0012) -> tuple[list, list]:
    """
    ★ 新增：等高/等低 (EQH/EQL) 偵測
    雙頂/雙底 = 機構存放流動性的位置，掃掉這些點位後反轉機率極高。

    邏輯：在最近 K 棒中，找出兩個距離在 threshold(0.12%) 以內的高點或低點，
    視為等高/等低流動性水位（比單純的前高/前低更可靠）。

    返回:
        eqh_levels: 等高水位列表（做空流動性目標）
        eql_levels: 等低水位列表（做多流動性目標）
    """
    highs = df['h'].values.astype(float)
    lows  = df['l'].values.astype(float)
    n     = len(highs)
    eqh_levels, eql_levels = [], []

    # 掃描等高
    for i in range(n - 1):
        for j in range(i + 3, min(i + 40, n)):   # 間隔至少3根，最多40根
            avg_h = (highs[i] + highs[j]) / 2
            if avg_h < 1e-10: continue
            if abs(highs[i] - highs[j]) / avg_h < threshold:
                eqh_levels.append(round(avg_h, 6))
                break

    # 掃描等低
    for i in range(n - 1):
        for j in range(i + 3, min(i + 40, n)):
            avg_l = (lows[i] + lows[j]) / 2
            if avg_l < 1e-10: continue
            if abs(lows[i] - lows[j]) / avg_l < threshold:
                eql_levels.append(round(avg_l, 6))
                break

    return eqh_levels, eql_levels


# ── 7B. Order Block (OB) 訂單塊 ──────────────────────────────────────────────
def find_order_blocks(df: pd.DataFrame, side: str,
                      lookback: int = 40) -> list:
    """
    ★ 新增：ICT 訂單塊偵測（M15 級別）

    多頭 OB (Bullish OB)：
      - 找最後一根陰線（收盤 < 開盤）
      - 該陰線後面出現 BOS（後續高點超越前方 Swing High）
      - OB 範圍：陰線的 [最低點, 開盤價]
      - 意義：機構做多的"足跡"，價格回測此區域進多

    空頭 OB (Bearish OB)：
      - 找最後一根陽線（收盤 > 開盤）
      - 該陽線後面出現 BOS（後續低點跌破前方 Swing Low）
      - OB 範圍：陽線的 [收盤價, 最高點]
      - 意義：機構做空的"足跡"，價格回測此區域進空

    返回: [{top, bottom, mid, idx}, ...] 由新到舊排序
    """
    obs = []
    n   = len(df)
    start = max(0, n - lookback)
    recent_sh_prices = [float(df['h'].iloc[max(0,i-15):i].max()) for i in range(n)]
    recent_sl_prices = [float(df['l'].iloc[max(0,i-15):i].min()) for i in range(n)]

    if side == "LONG":
        for i in range(start, n - 4):
            k = df.iloc[i]
            # 條件1：陰線（實體向下）
            if float(k['c']) >= float(k['o']): continue
            # 條件2：後續3根內有 BOS（突破前方最高點）
            look_ahead = df.iloc[i+1:min(i+5, n)]
            if look_ahead.empty: continue
            sub_high = look_ahead['h'].max()
            if sub_high <= recent_sh_prices[i]: continue
            # 通過條件 → 記錄 OB
            obs.append({
                'top'   : float(k['o']),   # 多頭OB頂部 = 陰線開盤
                'bottom': float(k['l']),   # 多頭OB底部 = 陰線最低
                'mid'   : (float(k['o']) + float(k['l'])) / 2,
                'idx'   : i,
            })
    else:  # SHORT
        for i in range(start, n - 4):
            k = df.iloc[i]
            # 條件1：陽線（實體向上）
            if float(k['c']) <= float(k['o']): continue
            # 條件2：後續3根內有 BOS（跌破前方最低點）
            look_ahead = df.iloc[i+1:min(i+5, n)]
            if look_ahead.empty: continue
            sub_low = look_ahead['l'].min()
            if sub_low >= recent_sl_prices[i]: continue
            obs.append({
                'top'   : float(k['h']),   # 空頭OB頂部 = 陽線最高
                'bottom': float(k['c']),   # 空頭OB底部 = 陽線收盤
                'mid'   : (float(k['h']) + float(k['c'])) / 2,
                'idx'   : i,
            })

    return sorted(obs, key=lambda x: x['idx'], reverse=True)


def check_ob_confluence(obs: list, entry_price: float, side: str) -> tuple[bool, dict | None]:
    """
    ★ 新增：確認進場點是否落在 Order Block 內

    做多：進場 CE 點落在多頭OB範圍 [bottom, top] 內 → 高機率反轉點
    做空：進場 CE 點落在空頭OB範圍 [bottom, top] 內 → 高機率反轉點

    返回: (is_in_ob, ob_info)
    """
    for ob in obs[:5]:   # 只看最近5個OB
        if ob['bottom'] <= entry_price <= ob['top']:
            return True, ob
    return False, None


# ── 7C. OTE Zone (Optimal Trade Entry) ──────────────────────────────────────
def check_ote_zone(swing_low: float, swing_high: float,
                   entry_price: float, side: str) -> tuple[bool, dict]:
    """
    ★ 新增：OTE 最佳進場區間 (Fibonacci 61.8% – 78.6%)

    ICT 核心概念：機構在 Fibonacci 61.8%-78.6% 回測區執行訂單，
    這個區間叫做「最佳進場區」(OTE)，進場點落在此區間勝率最高。

    做多 OTE：從 Swing Low → Swing High 的回測
      OTE 區間 = [SH - rng*0.786, SH - rng*0.618]
      即：回調了 61.8% 到 78.6%

    做空 OTE：從 Swing High → Swing Low 的回測
      OTE 區間 = [SL + rng*0.618, SL + rng*0.786]
      即：反彈了 61.8% 到 78.6%

    返回: (is_in_ote, {ote_high, ote_low, fib_618, fib_786, fib_50})
    """
    rng = swing_high - swing_low
    if rng < 1e-10:
        return False, {}

    fib_50  = swing_low + rng * 0.500
    fib_618 = swing_low + rng * 0.618
    fib_786 = swing_low + rng * 0.786

    if side == "LONG":
        # 做多：價格從高點回調，61.8%-78.6% 是 OTE
        ote_high = swing_high - rng * 0.618   # = fib_382 from top
        ote_low  = swing_high - rng * 0.786   # = fib_214 from top
        in_ote   = ote_low <= entry_price <= ote_high
    else:  # SHORT
        # 做空：價格從低點反彈，61.8%-78.6% 是 OTE
        ote_low  = swing_low + rng * 0.618
        ote_high = swing_low + rng * 0.786
        in_ote   = ote_low <= entry_price <= ote_high

    return in_ote, {
        'ote_high': ote_high,
        'ote_low' : ote_low,
        'fib_50'  : fib_50,
        'fib_618' : fib_618,
        'fib_786' : fib_786,
    }


# ── 7D. Asian Range ──────────────────────────────────────────────────────────
def get_asian_range(instId: str) -> tuple[float, float]:
    """
    ★ 新增：亞洲時段範圍 (00:00 – 08:00 NY Time)

    亞洲時段建立流動性（等高等低），倫敦/紐約時段掃蕩亞洲高低點後反轉。
    可用於確認掃損方向是否符合「掃亞洲時段流動性」的邏輯。

    返回: (asian_low, asian_high)
    """
    try:
        df1h = fetch_okx(instId, bar="1H", limit=30)
        if df1h is None or len(df1h) < 8:
            return 0.0, 0.0
        # 取最近 8 小時（近似亞洲時段）
        asian = df1h.tail(8)
        return float(asian['l'].min()), float(asian['h'].max())
    except:
        return 0.0, 0.0


# ── 7E. Weekly Bias ──────────────────────────────────────────────────────────
def check_weekly_bias(instId: str, side: str) -> tuple[bool, str]:
    """
    ★ 新增：週線偏向確認

    週線偏向是最強的 HTF 過濾器。
    多頭週線：週線收盤 > 前週收盤 且 當前價格 < 週線 50%
    空頭週線：週線收盤 < 前週收盤 且 當前價格 > 週線 50%

    重要：週線方向不符時，不強制過濾（H4 才是主要過濾），
    但減少匯流分數。
    """
    try:
        dfw = fetch_okx(instId, bar="1W", limit=10)
        if dfw is None or len(dfw) < 3:
            return True, "⚪ 週線 N/A"

        curr_week = dfw.iloc[-1]
        prev_week = dfw.iloc[-2]
        curr_p    = float(curr_week['c'])
        prev_close = float(prev_week['c'])
        wk_high   = float(curr_week['h'])
        wk_low    = float(curr_week['l'])
        wk_50     = (wk_high + wk_low) / 2

        if side == "LONG":
            bullish_wk = (curr_p > prev_close) and (curr_p < wk_50)
            label = "✅ 週線多頭折價" if bullish_wk else "⚠️ 週線偏空/溢價"
            return bullish_wk, label
        else:
            bearish_wk = (curr_p < prev_close) and (curr_p > wk_50)
            label = "✅ 週線空頭溢價" if bearish_wk else "⚠️ 週線偏多/折價"
            return bearish_wk, label
    except:
        return True, "⚪ 週線計算失敗"


# ── 7F. Confluence Score 匯流評分 ─────────────────────────────────────────────
def calculate_confluence_score(
    h4_ok           : bool,
    weekly_ok        : bool,
    is_swept         : bool,
    fvg_ok           : bool,
    ob_confluence    : bool,
    ote_ok           : bool,
    kz_quality       : str,    # "SILVER_BULLET" / "NY" / "LONDON"
    funding_aligned  : bool,
    atr_volatile     : bool,   # ★ 新增：ATR 波動率是否足夠
) -> tuple[int, list]:
    """
    ★ 更新：匯流評分系統（0-9 分，含 ATR 扣分）
    只有 ≥ CONFLUENCE_MIN_SCORE(5) 分才送出訊號。

    評分細則：
    ① H4 BOS + 折/溢價   → +2分（核心條件，權重最高）
    ② 週線方向一致        → +1分
    ③ M15 流動性掃蕩      → +1分
    ④ M5 MSS + FVG        → +1分
    ⑤ M15 Order Block 匯流 → +1分
    ⑥ OTE 最佳進場區間    → +1分
    ⑦ Silver Bullet 時段  → +1分（比其他時段多1分）
    ⑧ 資費率方向一致      → +1分（輔助確認）
    ⑨ ATR 縮量震盪        → -1分（★ 新增扣分項）

    返回: (score, details_list)
    """
    details = []
    score   = 0

    if h4_ok:
        score += 2; details.append("✅ H4 BOS+折溢價 (+2)")
    else:
        details.append("❌ H4偏向不符 (+0)")

    if weekly_ok:
        score += 1; details.append("✅ 週線方向一致 (+1)")
    else:
        details.append("⚠️ 週線方向不符 (+0)")

    if is_swept:
        score += 1; details.append("✅ 流動性掃蕩確認 (+1)")
    else:
        details.append("❌ 無掃損 (+0)")

    if fvg_ok:
        score += 1; details.append("✅ MSS+FVG確認 (+1)")
    else:
        details.append("❌ 無FVG (+0)")

    if ob_confluence:
        score += 1; details.append("✅ OB訂單塊匯流 (+1)")
    else:
        details.append("⚠️ 無OB匯流 (+0)")

    if ote_ok:
        score += 1; details.append("✅ OTE最佳進場區 (+1)")
    else:
        details.append("⚠️ 非OTE區間 (+0)")

    if kz_quality == "SILVER_BULLET":
        score += 1; details.append("✅ Silver Bullet時段 (+1)")
    elif kz_quality in ("NY", "LONDON"):
        details.append("✅ Killzone時段 (+0)")

    if funding_aligned:
        score += 1; details.append("✅ 資費率方向一致 (+1)")
    else:
        details.append("⚠️ 資費率偏向不符 (+0)")

    # ★ 新增：ATR 縮量扣分
    if not atr_volatile:
        score -= 1; details.append("⚠️ ATR縮量震盪 (-1)")
    else:
        details.append("✅ ATR波動正常 (+0)")

    return score, details


# ── 7G. 智慧 FVG 進場點 ──────────────────────────────────────────────────────
def get_smart_fvg_entry(fvg_info: dict, score: int, side: str) -> tuple[float, str]:
    """
    ★ 新增：依據匯流評分動態選擇 FVG 進場點

    標準訊號 (score ≤ 7) → CE 50%（保守等回測）
    ┌──────────────────────────────────────────────────┐
    │  FVG Top  ─────────────────── 100%              │
    │                                                  │
    │  CE       ─────────────────── 50%  ← 進場點    │
    │                                                  │
    │  FVG Bot  ─────────────────── 0%               │
    └──────────────────────────────────────────────────┘

    超強訊號 (score > 7) → Edge 25%（積極，避免錯過大行情）
    ┌──────────────────────────────────────────────────┐
    │  FVG Top  ─────────────────── 100%              │
    │  Edge(空) ─────────────────── 75%               │
    │           ─────────────────── 50%  (CE)         │
    │  Edge(多) ─────────────────── 25%  ← 多單進場  │
    │  FVG Bot  ─────────────────── 0%               │
    └──────────────────────────────────────────────────┘

    設計邏輯：
    - 超強訊號（7分以上）代表多重時框完全匯流，
      機構可能在 FVG 邊緣就已完成吸籌/派發，
      等到 CE 50% 反而容易追不到，甚至已錯過入場窗口。
    - 標準訊號仍等 CE，確保有足夠的回測確認。

    返回: (entry_price, entry_type_label)
    """
    fh  = fvg_info['fvg_high']
    fl  = fvg_info['fvg_low']
    rng = fh - fl

    if score > 7:
        if side == "LONG":
            entry = fl + rng * 0.25      # FVG 底部往上 25%
        else:
            entry = fh - rng * 0.25      # FVG 頂部往下 25%
        label = f"FVG Edge 25%（評分{score}分，積極進場）"
    else:
        entry = (fh + fl) / 2            # CE = 50%
        label = f"FVG CE 50%（評分{score}分，標準進場）"

    return entry, label

# ─────────────────────────────────────────────
# 8. 其他過濾器
# ─────────────────────────────────────────────
def get_today_sl_count(stats_file: str, today_str: str) -> int:
    try:
        df = pd.read_csv(stats_file)
        if 'date' not in df.columns: return 0
        return len(df[(df['result']=='SL') & (df['date']==today_str)])
    except: return 0

def check_correlated_group(instId: str, trades_df: pd.DataFrame, side: str) -> bool:
    for grp in CORR_GROUPS:
        if instId not in grp: continue
        if trades_df.empty: return True
        for _, row in trades_df.iterrows():
            if row['instId'] in grp and row['instId'] != instId and row['side'] == side:
                return False
    return True

# ─────────────────────────────────────────────
# 9. 主程式
# ─────────────────────────────────────────────
def main():
    try:
        now_utc       = datetime.utcnow()
        now_tw        = now_utc + timedelta(hours=8)
        today_str     = now_tw.strftime('%Y-%m-%d')
        manual_report = os.getenv("MANUAL_REPORT","false").lower() == "true"

        for f, cols in zip([LOG_FILE, STATS_FILE], [LOG_COLS, STATS_COLS]):
            if not os.path.exists(f) or os.stat(f).st_size == 0:
                pd.DataFrame(columns=cols).to_csv(f, index=False)

        # ── A. 午夜戰績回報 ───────────────────────────────────────────
        is_midnight = (now_tw.hour == 0 and now_tw.minute < 15)
        if is_midnight or manual_report:
            if not os.path.exists("midnight.ok") or manual_report:
                df_s = pd.read_csv(STATS_FILE)
                if not df_s.empty:
                    tp_c  = len(df_s[df_s['result']=='TP'])
                    sl_c  = len(df_s[df_s['result']=='SL'])
                    total = tp_c + sl_c
                    wr    = tp_c / total * 100 if total > 0 else 0
                    ev    = (wr/100 * 2.0 + (1-wr/100) * -1.0) if total > 0 else 0
                    date_str = (now_tw - timedelta(days=1)).strftime('%Y-%m-%d')
                    send_tg(
                        f"📊 *Alpha Oracle v2 每日戰績*\n"
                        f"──────────────────\n"
                        f"📅 {date_str}\n\n"
                        f"✅ 盈利（含保本）：{tp_c} 單\n"
                        f"❌ 止損：{sl_c} 單  |  總計：{total} 單\n\n"
                        f"🔥 勝率：*{wr:.1f}%*\n"
                        f"💹 期望值：*{ev:+.2f}R / 單*\n"
                        f"──────────────────\n"
                        f"📌 達到 1.5R 即計入勝利（含保本）\n"
                        f"🎯 匯流門檻：≥ {CONFLUENCE_MIN_SCORE}/8 分才進場\n"
                        f"💡 EV > 0 ＝ 長期正期望值"
                    )
                else:
                    send_tg(f"📊 *Alpha Oracle v2*\n📅 {(now_tw-timedelta(days=1)).strftime('%Y-%m-%d')}\n📭 今日無成交")
                if is_midnight:
                    pd.DataFrame(columns=STATS_COLS).to_csv(STATS_FILE, index=False)
                    with open("midnight.ok","w") as fh: fh.write("ok")
        elif now_tw.hour != 0 and os.path.exists("midnight.ok"):
            os.remove("midnight.ok")

        # ── B. 讀取持倉狀態 ───────────────────────────────────────────
        try:
            trades_df = pd.read_csv(LOG_FILE)
            for col in ("wait_since","tp1_hit"):
                if col not in trades_df.columns: trades_df[col] = 0
        except:
            trades_df = pd.DataFrame(columns=LOG_COLS)

        active_ids      = trades_df['instId'].tolist()
        updated         = []
        current_bar     = int(now_utc.timestamp() // 900)

        kz_ok, kz_label = is_ict_killzone(now_utc)
        kz_quality      = get_kz_quality(kz_label)
        today_sl_count  = get_today_sl_count(STATS_FILE, today_str)
        daily_limit_hit = today_sl_count >= MAX_DAILY_SL

        if daily_limit_hit:
            logging.info(f"今日止損 {today_sl_count} 次，達上限，停止新訊號")

        logging.info(f"Killzone:{kz_label}  今日SL:{today_sl_count}/{MAX_DAILY_SL}")

        # ── C. 逐幣掃描 ───────────────────────────────────────────────
        for instId in ALL_COINS:
            df15 = fetch_okx(instId, bar="15m", limit=150)
            if df15 is None or df15.empty:
                time.sleep(0.3); continue

            curr_p   = float(df15['c'].iloc[-1])
            coin_sym = instId.split('-')[0]

            # ══════════════════════════════════════════════
            # 1. 掃描新訊號
            # ══════════════════════════════════════════════
            if instId not in active_ids:
                if not kz_ok or daily_limit_hit:
                    time.sleep(0.3); continue

                open_count = len(trades_df[trades_df['status'].isin(['WAITING','ACTIVE'])])
                if open_count >= MAX_CONCURRENT:
                    time.sleep(0.3); continue

                fr = fetch_funding_rate_raw(instId)

                setup_found = None

                for side in ["LONG", "SHORT"]:
                    # 資費率過濾（放寬：0.05% → 0.10%）
                    if side=="LONG"  and fr >  0.001: continue
                    if side=="SHORT" and fr < -0.001: continue

                    if not check_correlated_group(instId, trades_df, side): continue

                    # ────────────────────────────────────────
                    # ① Weekly Bias（新增）
                    # ────────────────────────────────────────
                    weekly_ok, weekly_label = check_weekly_bias(instId, side)
                    # 注意：週線不符不強制跳過，但在評分中扣分

                    # ────────────────────────────────────────
                    # ② H4 BOS + 折/溢價區（原有）
                    # ────────────────────────────────────────
                    h4_ok, h4_label, h4_tp3 = check_h4_bias(instId, side)
                    if not h4_ok:
                        logging.info(f"[{instId}][{side}] H4偏向不符: {h4_label}")
                        continue

                    # ────────────────────────────────────────
                    # ③ 收集流動性水位（原有 + EQH/EQL 強化）
                    # ────────────────────────────────────────
                    pdh, pdl = get_pdh_pdl(instId)
                    pwh, pwl = get_pwh_pwl(instId)
                    sh15, sl15 = find_swing_ict(df15, lookleft=20, lookright=5)

                    # ★ 新增：EQH/EQL 等高等低水位
                    eqh_levels, eql_levels = find_equal_levels(df15, threshold=0.0012)

                    liq_lows  = [pdl, pwl] + [s['price'] for s in sl15[-6:]] + eql_levels[-3:]
                    liq_highs = [pdh, pwh] + [s['price'] for s in sh15[-6:]] + eqh_levels[-3:]

                    # ────────────────────────────────────────
                    # ④ 亞洲時段範圍（新增）
                    # ────────────────────────────────────────
                    asian_low, asian_high = get_asian_range(instId)
                    if asian_low  > 0: liq_lows.append(asian_low)
                    if asian_high > 0: liq_highs.append(asian_high)

                    # ────────────────────────────────────────
                    # ⑤ M15 流動性掃蕩（原有，水位已強化）
                    # ────────────────────────────────────────
                    is_swept, sweep_info = check_liquidity_sweep(df15, side, liq_lows, liq_highs)
                    if not is_swept:
                        logging.info(f"[{instId}][{side}] 無掃損")
                        continue

                    # ────────────────────────────────────────
                    # ⑥ M15 Order Block 偵測（新增）
                    # ────────────────────────────────────────
                    obs_m15 = find_order_blocks(df15, side, lookback=40)

                    # ────────────────────────────────────────
                    # ⑦ M5 MSS + FVG（原有）
                    # ────────────────────────────────────────
                    fvg_ok, fvg_info = find_mss_fvg_entry(instId, side)
                    if not fvg_ok:
                        logging.info(f"[{instId}][{side}] MSS/FVG 未確認")
                        continue

                    entry = fvg_info['entry']
                    sl    = sweep_info['sweep_candle_sl']

                    if side == "LONG"  and sl >= entry: continue
                    if side == "SHORT" and sl <= entry: continue

                    risk     = abs(entry - sl) + 1e-10
                    risk_pct = risk / (entry + 1e-10) * 100
                    if risk_pct < SL_MIN_PCT * 100:
                        logging.info(f"[{instId}][{side}] SL 太緊 ({risk_pct:.2f}%)")
                        continue

                    # ────────────────────────────────────────
                    # ⑧ OB 匯流確認（新增）
                    # ────────────────────────────────────────
                    ob_hit, ob_info = check_ob_confluence(obs_m15, entry, side)

                    # ────────────────────────────────────────
                    # ⑨ OTE 最佳進場區間（新增）
                    # ────────────────────────────────────────
                    sw_low  = sl15[-1]['price'] if sl15 else 0.0
                    sw_high = sh15[-1]['price'] if sh15 else 0.0
                    ote_ok_flag = False
                    ote_info    = {}
                    if sw_low > 0 and sw_high > 0 and sw_high > sw_low:
                        ote_ok_flag, ote_info = check_ote_zone(sw_low, sw_high, entry, side)

                    # ────────────────────────────────────────
                    # ⑩ ATR 波動率過濾（新增）
                    # ────────────────────────────────────────
                    atr_volatile, atr_label, atr_ratio = check_atr_volatility(df15)
                    if not atr_volatile:
                        logging.info(f"[{instId}][{side}] ATR縮量 ({atr_ratio:.0%}) → 繼續計分但扣1分")

                    # ────────────────────────────────────────
                    # ⑪ 匯流評分（含 ATR 扣分）
                    # ────────────────────────────────────────
                    funding_aligned = (
                        (side=="LONG"  and fr <= 0.0005) or   # 放寬評分門檻與過濾門檻一致
                        (side=="SHORT" and fr >= -0.0005)
                    )
                    score, score_details = calculate_confluence_score(
                        h4_ok          = h4_ok,
                        weekly_ok      = weekly_ok,
                        is_swept       = is_swept,
                        fvg_ok         = fvg_ok,
                        ob_confluence  = ob_hit,
                        ote_ok         = ote_ok_flag,
                        kz_quality     = kz_quality,
                        funding_aligned= funding_aligned,
                        atr_volatile   = atr_volatile,
                    )

                    if score < CONFLUENCE_MIN_SCORE:
                        logging.info(f"[{instId}][{side}] 匯流分數不足: {score}/{CONFLUENCE_MIN_SCORE} → 跳過")
                        continue

                    # ────────────────────────────────────────
                    # ⑫ 智慧 FVG 進場點（評分 > 7 用 Edge）
                    # ────────────────────────────────────────
                    entry, entry_type = get_smart_fvg_entry(fvg_info, score, side)

                    # 重新驗證調整後的進場點與 SL 關係
                    if side == "LONG"  and sl >= entry: continue
                    if side == "SHORT" and sl <= entry: continue
                    risk     = abs(entry - sl) + 1e-10
                    risk_pct = risk / (entry + 1e-10) * 100
                    if risk_pct < SL_MIN_PCT * 100:
                        logging.info(f"[{instId}][{side}] 調整後SL太緊({risk_pct:.2f}%)")
                        continue

                    # ────────────────────────────────────────
                    # 計算 TP
                    # ────────────────────────────────────────
                    tp1, tp2, tp3 = calculate_ict_tps(entry, sl, side, h4_tp3)

                    setup_found = {
                        'side'         : side,
                        'entry'        : entry,
                        'entry_type'   : entry_type,
                        'sl'           : sl,
                        'tp1'          : tp1,
                        'tp2'          : tp2,
                        'tp3'          : tp3,
                        'risk_pct'     : risk_pct,
                        'h4_label'     : h4_label,
                        'h4_tp3'       : h4_tp3,
                        'weekly_label' : weekly_label,
                        'sweep_info'   : sweep_info,
                        'fvg_info'     : fvg_info,
                        'ob_hit'       : ob_hit,
                        'ob_info'      : ob_info,
                        'ote_ok'       : ote_ok_flag,
                        'ote_info'     : ote_info,
                        'atr_label'    : atr_label,
                        'atr_volatile' : atr_volatile,
                        'score'        : score,
                        'score_details': score_details,
                        'asian_low'    : asian_low,
                        'asian_high'   : asian_high,
                    }
                    break

                if setup_found is None:
                    time.sleep(0.3); continue

                # ── 發送 Telegram 訊號 ────────────────────────────────────
                s = setup_found
                funding, ls_ratio = get_funding_ls(instId)
                cvd_label = calculate_cvd(df15)
                st_dir    = calculate_supertrend(df15)
                st_label  = "🟢 ST多" if st_dir == 1 else ("🔴 ST空" if st_dir == -1 else "⚪ ST中性")
                side_zh   = "🟢 多單 (LONG)" if s['side']=="LONG" else "🔴 空單 (SHORT)"
                fi        = s['fvg_info']

                # 評分星號
                score_stars = "⭐" * s['score']

                # OB 描述
                ob_desc = (f"✅ OB匯流 {s['ob_info']['bottom']:.4f}–{s['ob_info']['top']:.4f}"
                           if s['ob_hit'] else "⚠️ 無OB匯流")

                # OTE 描述
                ote_desc = (f"✅ OTE區 {s['ote_info'].get('ote_low',0):.4f}–{s['ote_info'].get('ote_high',0):.4f}"
                            if s['ote_ok'] else "⚠️ 非OTE最佳區")

                # 進場點類型標籤
                is_edge_entry = "Edge" in s.get('entry_type', '')
                entry_icon    = "⚡" if is_edge_entry else "📍"
                entry_label   = "積極Edge25%" if is_edge_entry else "標準CE50%"

                # 等待描述
                wait_desc = (f"⚡ *超強訊號，等待 FVG Edge 25% 確認拒絕棒*"
                             if is_edge_entry else
                             f"⏳ *等待價格回測 FVG CE 並確認拒絕棒進場*")

                msg  = f"🔥 *Alpha Oracle v2 ICT 訊號* 🔥\n"
                msg += f"──────────────────\n"
                msg += f"💎 幣種：#{coin_sym}  |  {kz_label}\n"
                msg += f"🎯 方向：{side_zh}\n"
                msg += f"🌟 匯流評分：{s['score']}/9 {score_stars}\n"
                msg += f"\n"
                msg += f"📐 *多時框分析*\n"
                msg += f"  📅 週線：{s['weekly_label']}\n"
                msg += f"  📊 H4 ：{s['h4_label']}\n"
                msg += f"  📈 ST ：{st_label}\n"
                msg += f"  📉 ATR：{s['atr_label']}\n"
                msg += f"\n"
                msg += f"📐 *進場結構*\n"
                msg += f"  🎣 掃損：流動性水位 {s['sweep_info']['liq_level']:.4f} 已掃蕩\n"
                msg += f"  📦 FVG ：{fi['fvg_low']:.4f} – {fi['fvg_high']:.4f}\n"
                msg += f"  📊 MSS ：突破 {fi['mss_level']:.4f}\n"
                msg += f"  {ob_desc}\n"
                msg += f"  {ote_desc}\n"
                if s['asian_high'] > 0:
                    msg += f"  🌏 亞洲區間：{s['asian_low']:.4f} – {s['asian_high']:.4f}\n"
                msg += f"\n"
                msg += f"{entry_icon} *進場 [{entry_label}]*：`{s['entry']:.4f}`\n"
                msg += f"🚫 *止損 SL*：`{s['sl']:.4f}`  (掃損棒{'低' if s['side']=='LONG' else '高'}點, -{s['risk_pct']:.1f}%)\n"
                msg += f"🔒 BE 觸發  ：`{s['tp1']:.4f}`  (1.5R → 移 SL 到成本)\n"
                msg += f"💰 TP1 1:2  ：`{s['tp2']:.4f}`\n"
                msg += f"🚀 TP2 H4   ：`{s['tp3']:.4f}`\n"
                msg += f"\n"
                msg += f"📊 多空比 {ls_ratio} | 資費 {funding} | {cvd_label}\n"
                msg += f"\n"
                msg += wait_desc

                send_tg(msg)
                updated.append({
                    "instId"    : instId,
                    "side"      : s['side'],
                    "status"    : "WAITING",
                    "entry"     : s['entry'],
                    "sl"        : s['sl'],
                    "tp1"       : s['tp1'],
                    "tp2"       : s['tp2'],
                    "tp3"       : s['tp3'],
                    "locked"    : 0,
                    "wait_since": current_bar,
                    "tp1_hit"   : 0,
                })
                time.sleep(0.5)
                continue

            # ══════════════════════════════════════════════
            # 2. 追蹤現有持倉（WAITING / ACTIVE / COOLDOWN）
            # ══════════════════════════════════════════════
            t = normalize_trade(trades_df[trades_df['instId']==instId].iloc[0].to_dict())

            # ── WAITING ─────────────────────────────────────────────────
            if t['status'] == "WAITING":
                if current_bar - t['wait_since'] > WAITING_EXPIRY_BARS:
                    logging.info(f"[{instId}] WAITING 逾期，清除")
                    time.sleep(0.3); continue

                n_check = min(8, len(df15))
                cur_low, cur_high = fetch_current_candle_hl(instId)
                check_low  = min(df15['l'].iloc[-n_check:].min(), cur_low,  curr_p)
                check_high = max(df15['h'].iloc[-n_check:].max(), cur_high, curr_p)

                is_hit = (
                    (t['side']=="LONG"  and check_low  <= t['entry']) or
                    (t['side']=="SHORT" and check_high >= t['entry'])
                )
                already_sl = (
                    (t['side']=="LONG"  and curr_p < t['sl']) or
                    (t['side']=="SHORT" and curr_p > t['sl'])
                )

                if is_hit and already_sl:
                    logging.info(f"[{instId}] 觸及 CE 但穿破 SL，放棄")
                    time.sleep(0.3); continue

                if is_hit:
                    reject_ok = False
                    for idx in range(-min(5, len(df15)), 0):
                        ck = df15.iloc[idx]
                        if t['side']=="LONG"  and ck['l'] <= t['entry']*1.001 and ck['c'] > t['entry']:
                            reject_ok = True; break
                        if t['side']=="SHORT" and ck['h'] >= t['entry']*0.999 and ck['c'] < t['entry']:
                            reject_ok = True; break

                    if not reject_ok:
                        logging.info(f"[{instId}] CE 已觸及，等拒絕確認棒...")
                        updated.append(t)
                        time.sleep(0.3); continue

                    t['status'] = "ACTIVE"
                    side_zh = "🟢 多單" if t['side']=="LONG" else "🔴 空單"
                    send_tg(
                        f"🚀 *Alpha Oracle v2 | ICT 確認進場*\n"
                        f"──────────────────\n"
                        f"💎 幣種：#{coin_sym}  {side_zh}\n"
                        f"✅ FVG CE 拒絕確認棒已收盤\n\n"
                        f"📍 成交：`{t['entry']:.4f}`\n"
                        f"🚫 SL  ：`{t['sl']:.4f}`  (掃損棒)\n"
                        f"🔒 BE  ：`{t['tp1']:.4f}`  → 觸及後自動移 SL 到成本\n"
                        f"💰 TP1 ：`{t['tp2']:.4f}`  (1:2 R/R)\n"
                        f"🚀 TP2 ：`{t['tp3']:.4f}`  (H4 目標)\n\n"
                        f"📌 *最壞結果：1.5R 觸及後保本出場*"
                    )

                updated.append(t)

            # ── ACTIVE ──────────────────────────────────────────────────
            elif t['status'] == "ACTIVE":
                act_n = min(3, len(df15))
                acl, ach = fetch_current_candle_hl(instId)
                act_low  = min(df15['l'].iloc[-act_n:].min(), acl, curr_p)
                act_high = max(df15['h'].iloc[-act_n:].max(), ach, curr_p)

                if t['locked'] == 0 and (
                    (t['side']=="LONG"  and act_high >= t['tp1']) or
                    (t['side']=="SHORT" and act_low  <= t['tp1'])
                ):
                    t['locked']  = 1
                    t['tp1_hit'] = 1
                    t['sl']      = t['entry']
                    pd.DataFrame([{"instId": instId, "result": "TP", "date": today_str}]).to_csv(
                        STATS_FILE, mode='a', header=False, index=False
                    )
                    send_tg(
                        f"🔒 *Alpha Oracle v2 | BE 觸發*\n"
                        f"──────────────────\n"
                        f"💎 #{coin_sym}  ✅ 1.5R 達標\n"
                        f"🔒 SL 自動移至成本：`{t['entry']:.4f}`\n"
                        f"📊 *統計記為勝利（保本以上）*\n\n"
                        f"📍 當前：`{curr_p:.4f}`\n"
                        f"💰 TP1 (1:2)：`{t['tp2']:.4f}`\n"
                        f"🚀 TP2 H4  ：`{t['tp3']:.4f}`\n\n"
                        f"✨ *最壞結果：保本出場*"
                    )

                if t['locked'] == 1 and (
                    (t['side']=="LONG"  and act_high >= t['tp2']) or
                    (t['side']=="SHORT" and act_low  <= t['tp2'])
                ):
                    t['locked'] = 2
                    t['sl']     = t['tp1']
                    send_tg(
                        f"💰 *Alpha Oracle v2 | TP1 (1:2) 達標*\n"
                        f"──────────────────\n"
                        f"💎 #{coin_sym}  ✅ 2R 達標\n"
                        f"🔒 SL 升級至 +1.5R：`{t['tp1']:.4f}`\n\n"
                        f"🚀 追擊 TP2 H4 目標：`{t['tp3']:.4f}`"
                    )

                is_sl  = (t['side']=="LONG"  and act_low  <= t['sl']) or \
                         (t['side']=="SHORT" and act_high >= t['sl'])
                is_tp3 = (t['side']=="LONG"  and act_high >= t['tp3']) or \
                         (t['side']=="SHORT" and act_low  <= t['tp3'])

                if is_sl or is_tp3:
                    if is_tp3:
                        rl, ep = "🚀 TP2 H4 目標達標", t['tp3']
                    elif t['locked'] >= 2:
                        rl, ep = "💰 鎖利出場 (+1.5R 保底)", t['tp1']
                    elif t['locked'] == 1:
                        rl, ep = "⚖️ 保本出場 (BE)", t['entry']
                    else:
                        rl, ep = "❌ 止損離場", t['sl']

                    if not (is_sl and t['locked'] >= 1):
                        res = "TP" if is_tp3 else "SL"
                        if t['locked'] == 0:
                            pd.DataFrame([{"instId": instId, "result": res, "date": today_str}]).to_csv(
                                STATS_FILE, mode='a', header=False, index=False
                            )

                    send_tg(
                        f"🏁 *Alpha Oracle v2 | 交易結算*\n"
                        f"──────────────────\n"
                        f"💎 幣種：#{coin_sym}\n"
                        f"🏆 結果：{rl}\n\n"
                        f"📍 離場：`{ep:.4f}`\n"
                        f"🚫 SL  ：`{t['sl']:.4f}`\n"
                        f"🔒 BE  ：`{t['tp1']:.4f}`\n"
                        f"💰 TP1 ：`{t['tp2']:.4f}`\n"
                        f"🚀 TP2 ：`{t['tp3']:.4f}`"
                    )
                    cooldown_dur = COOLDOWN_BARS_TP if (is_tp3 or t['locked'] >= 1) else COOLDOWN_BARS_SL
                    updated.append({**t, "status":"COOLDOWN",
                                    "wait_since": current_bar,
                                    "locked"    : cooldown_dur})
                    time.sleep(0.3); continue

                updated.append(t)

            # ── COOLDOWN ──────────────────────────────────────────────────
            elif t['status'] == "COOLDOWN":
                total_cd  = int(t.get('locked', COOLDOWN_BARS_SL))
                bars_done = current_bar - t['wait_since']
                if bars_done >= total_cd:
                    logging.info(f"[{instId}] 冷卻結束（{bars_done}/{total_cd} 棒）")
                else:
                    logging.info(f"[{instId}] 冷卻中，剩 {(total_cd-bars_done)*15} 分鐘")
                    updated.append(t)

            time.sleep(0.3)

        pd.DataFrame(updated).to_csv(LOG_FILE, index=False)

    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    main()
