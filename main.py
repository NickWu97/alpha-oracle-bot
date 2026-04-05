"""
Alpha Oracle — ICT Smart Money Bot
架構：ICT Killzones + H4 Bias (BOS+Discount) + M15 Liquidity Sweep + M5 MSS/FVG Entry

進場 SOP:
  1. H4 BOS + 折價/溢價區 → 確定方向
  2. ICT Killzone → 僅在倫敦/紐約開盤時段偵測
  3. M15 流動性掃蕩 → 4 條件驗證（穿透+收回+影線+動能）
  4. M5 MSS + FVG → 掃損後結構突破 + 公平價值缺口
  5. 進場：等待價格回測 FVG 50%（CE）並確認拒絕棒

風控：
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

MAX_DAILY_SL        = 2
MAX_CONCURRENT      = 3
SL_MIN_PCT          = 0.005   # 0.5% 最小止損（ICT 精確 SL，可以比 ATR 緊）
WAITING_EXPIRY_BARS = 24      # 6 小時等待 FVG CE 回測
COOLDOWN_BARS_SL    = 8       # 止損後冷卻 2 小時
COOLDOWN_BARS_TP    = 4       # 止盈後冷卻 1 小時

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
    dst_s = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)   # 3 月第二個週日
    nov1  = datetime(y, 11, 1)
    dst_e = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)        # 11 月第一個週日
    return dst_s <= d.replace(tzinfo=None) < dst_e

def utc_to_ny(now_utc: datetime) -> datetime:
    """UTC → New York Time（自動處理 EDT/EST）"""
    return now_utc + timedelta(hours=-4 if is_us_dst(now_utc) else -5)

def is_ict_killzone(now_utc: datetime) -> tuple[bool, str]:
    """
    ICT 殺手時段（New York Time 為準）
    ✅ London Open  : 03:00 – 05:00 NY
    ✅ New York Open: 07:00 – 10:00 NY
    ❌ 其他時段一律忽略新訊號
    """
    h = utc_to_ny(now_utc).hour
    if 3 <= h < 5:
        return True, f"🇬🇧 London Open ({h:02d}:xx NY)"
    if 7 <= h < 10:
        return True, f"🗽 New York Open ({h:02d}:xx NY)"
    return False, f"⏸ 非 Killzone ({h:02d}:xx NY)"


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
                return float(row[3]), float(row[2])   # low, high
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
    hl2=( hi+lo)/2; bu=hl2-mult*atr; bd=hl2+mult*atr
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
# 6. ICT 流動性分析
# ─────────────────────────────────────────────

def get_pdh_pdl(instId: str) -> tuple[float, float]:
    """Previous Day High / Low"""
    df = fetch_okx(instId, bar="1D", limit=5)
    if df is None or len(df) < 2: return 0.0, 0.0
    return float(df['h'].iloc[-2]), float(df['l'].iloc[-2])

def get_pwh_pwl(instId: str) -> tuple[float, float]:
    """Previous Week High / Low"""
    df = fetch_okx(instId, bar="1W", limit=5)
    if df is None or len(df) < 2: return 0.0, 0.0
    return float(df['h'].iloc[-2]), float(df['l'].iloc[-2])

def find_swing_ict(df: pd.DataFrame,
                   lookleft: int = 20,
                   lookright: int = 5) -> tuple[list, list]:
    """
    ICT 分型偵測（確認型）
    Swing High: i 的高點嚴格高於左 lookleft 根 + 右 lookright 根所有 K 棒高點
    Swing Low : i 的低點嚴格低於左 lookleft 根 + 右 lookright 根所有 K 棒低點

    因需要 lookright 根右側 K 棒確認，最新的幾個分型尚未確認。
    """
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
    """
    ICT 流動性掃蕩偵測（四條件完整驗證）

    做多（Bullish Sweep）—— 掃低點後反轉做多：
    ① 穿透 (Penetration) : K 棒 Low  < 流動性低點
    ② 收回 (Rejection)   : K 棒 Close > 流動性低點
    ③ 影線比例 (Wick)    : 下影線（min(O,C) - Low）≥ 整根 K 棒範圍的 50%
    ④ 動能確認 (Disp.)   : 掃損棒後 2 根內，出現陽線且 Close > 掃損棒 High

    做空（Bearish Sweep）相反邏輯。

    返回: (is_swept, sweep_info)
    sweep_info 包含 sweep_candle_sl（作為最終止損位）
    """
    n = len(df)
    if n < 12: return False, None

    check_start = max(0, n - 12)   # 只看最近 12 根（太老的掃損失效）

    if side == "LONG":
        levels = sorted([l for l in liq_lows if l and l > 0])
        if not levels: return False, None

        for i in range(check_start, n - 2):
            k = df.iloc[i]
            for lvl in levels:
                # ① 穿透
                if k['l'] >= lvl: continue
                # ② 收回
                if k['c'] <= lvl: continue
                # ③ 影線比例
                rng = k['h'] - k['l']
                if rng < 1e-10: continue
                lower_wick = min(k['o'], k['c']) - k['l']
                if lower_wick / rng < 0.50: continue
                # ④ 動能確認：後 2 根內有陽線收盤 > 掃損棒高點
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
                        'sweep_candle_sl' : float(k['l']),   # 做多 SL = 掃損棒最低點
                        'sweep_candle_high': float(k['h']),
                        'liq_level'       : lvl,
                        'disp_close'      : disp_close,
                        'sweep_idx'       : i,
                    }
    else:  # SHORT
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
                if upper_wick / rng < 0.50: continue
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
                        'sweep_candle_sl' : float(k['h']),   # 做空 SL = 掃損棒最高點
                        'sweep_candle_low': float(k['l']),
                        'liq_level'       : lvl,
                        'disp_close'      : disp_close,
                        'sweep_idx'       : i,
                    }
    return False, None

def check_h4_bias(instId: str, side: str) -> tuple[bool, str, float]:
    """
    H4 市場偏向分析

    多頭偏向（LONG）條件：
    1. BOS: 最近 H4 Swing High > 前一個 Swing High（更高的高點 = Higher High）
    2. 折價區（Discount）: 當前價格 < 最近 SL→SH 範圍的 50% 水位（值得做多的低位）

    空頭偏向（SHORT）條件：
    1. BOS: 最近 H4 Swing Low < 前一個 Swing Low（更低的低點 = Lower Low）
    2. 溢價區（Premium）: 當前價格 > 最近 SL→SH 範圍的 50% 水位（值得做空的高位）

    返回: (bias_ok, label, h4_tp3_target)
    h4_tp3_target: H4 對向流動性目標（作為 TP3 參考）
    """
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
        bos      = last_sh > prev_sh           # Higher High → 多頭 BOS
        discount = curr < fifty_pct            # 折價區
        ok       = bos and discount
        label    = ("✅ H4 多頭BOS折價" if ok
                    else ("H4 多頭BOS溢價" if bos else "H4 空頭結構"))
        # TP3 目標：最近的 H4 Swing High（對向流動性）
        tp3_tgt  = last_sh if ok else 0.0

    else:  # SHORT
        bos      = last_sl < prev_sl           # Lower Low → 空頭 BOS
        premium  = curr > fifty_pct            # 溢價區
        ok       = bos and premium
        label    = ("✅ H4 空頭BOS溢價" if ok
                    else ("H4 空頭BOS折價" if bos else "H4 多頭結構"))
        tp3_tgt  = last_sl if ok else 0.0

    if ok:
        logging.info(f"[{instId}] H4 偏向: {label} | 50%={fifty_pct:.4f} | curr={curr:.4f}")

    return ok, label, tp3_tgt

def find_mss_fvg_entry(instId: str, side: str) -> tuple[bool, dict | None]:
    """
    M5 市場結構突破 (MSS) + 公平價值缺口 (FVG) 進場

    做多流程：
    1. 找到 M5 Swing High（掃損發生之後形成的）
    2. 確認 M5 收盤突破了那個 Swing High（MSS）
    3. 在突破棒前後尋找 Bullish FVG（k2.Low > k0.High）
    4. 進場點 = FVG 的 50%（Consequent Encroachment, CE）

    做空流程相反。

    返回: (found, entry_info)
    entry_info = {'entry': CE 進場點, 'fvg_high', 'fvg_low', 'mss_level', 'fvg_type'}
    """
    df5 = fetch_okx(instId, bar="5m", limit=200)
    if df5 is None or len(df5) < 40:
        return False, None

    sh5, sl5 = find_swing_ict(df5, lookleft=8, lookright=3)
    curr = float(df5['c'].iloc[-1])

    if side == "LONG":
        # MSS: 找最近被當前價格突破的 M5 Swing High
        mss_level = None
        for item in reversed(sh5):
            if curr > item['price']:
                mss_level = item['price']
                break
        if mss_level is None: return False, None

        # 在最近 30 根 M5 K 棒內找 Bullish FVG
        start = max(1, len(df5) - 30)
        for i in range(len(df5) - 2, start, -1):
            k0 = df5.iloc[i-1]
            k2 = df5.iloc[i+1]
            if float(k2['l']) > float(k0['h']):            # FVG 條件
                fh = float(k2['l']); fl = float(k0['h'])
                ce = (fh + fl) / 2
                if ce < curr:                               # CE 在當前價以下（等回測）
                    return True, {
                        'entry'    : ce,
                        'fvg_high' : fh,
                        'fvg_low'  : fl,
                        'mss_level': mss_level,
                        'fvg_type' : '多頭FVG',
                    }
    else:  # SHORT
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
    """
    ICT TP 結構：
    tp1 (BE 觸發) = 1.5R → 觸及後立即移 SL 到成本，統計記為「勝利」
    tp2 (主目標)  = 2.0R (1:2 R/R)
    tp3 (延伸)    = H4 對向流動性（若可用）或 3.0R

    為何先記 TP 再保本？
    1.5R 代表你的 R:R 已經 > 1，從統計意義上已是成功交易，
    後續交易變成「免費的期望值」，不影響勝率帳面數字。
    """
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
# 7. 其他過濾器
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
# 8. 主程式
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
                    # 平均 TP 約 2R（主目標），SL = -1R
                    ev    = (wr/100 * 2.0 + (1-wr/100) * -1.0) if total > 0 else 0
                    date_str = (now_tw - timedelta(days=1)).strftime('%Y-%m-%d')
                    send_tg(
                        f"📊 *Alpha Oracle 每日戰績*\n"
                        f"──────────────────\n"
                        f"📅 {date_str}\n\n"
                        f"✅ 盈利（含保本）：{tp_c} 單\n"
                        f"❌ 止損：{sl_c} 單  |  總計：{total} 單\n\n"
                        f"🔥 勝率：*{wr:.1f}%*\n"
                        f"💹 期望值：*{ev:+.2f}R / 單*\n"
                        f"──────────────────\n"
                        f"📌 達到 1.5R 即計入勝利（含保本）\n"
                        f"💡 EV > 0 ＝ 長期正期望值"
                    )
                else:
                    send_tg(f"📊 *Alpha Oracle*\n📅 {(now_tw-timedelta(days=1)).strftime('%Y-%m-%d')}\n📭 今日無成交")
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

        # ICT Killzone 檢查（只影響新訊號）
        kz_ok, kz_label = is_ict_killzone(now_utc)

        # 今日止損次數
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

                # 基本關口
                if not kz_ok or daily_limit_hit:
                    time.sleep(0.3); continue
                open_count = len(trades_df[trades_df['status'].isin(['WAITING','ACTIVE'])])
                if open_count >= MAX_CONCURRENT:
                    time.sleep(0.3); continue
                if not check_correlated_group(instId, trades_df, "LONG") and \
                   not check_correlated_group(instId, trades_df, "SHORT"):
                    time.sleep(0.3); continue

                # 資費率過濾（兩個方向都可能用到）
                fr = fetch_funding_rate_raw(instId)

                # ── 嘗試 LONG 和 SHORT 兩個方向 ──────────────────────
                setup_found = None
                for side in ["LONG", "SHORT"]:

                    # 資費率
                    if side=="LONG"  and fr >  0.0005: continue
                    if side=="SHORT" and fr < -0.0005: continue

                    # 相關幣去重
                    if not check_correlated_group(instId, trades_df, side): continue

                    # ① HTF Bias: H4 BOS + 折/溢價區
                    h4_ok, h4_label, h4_tp3 = check_h4_bias(instId, side)
                    if not h4_ok:
                        logging.info(f"[{instId}][{side}] H4偏向不符: {h4_label}")
                        continue

                    # ② 收集流動性水位
                    pdh, pdl = get_pdh_pdl(instId)
                    pwh, pwl = get_pwh_pwl(instId)
                    sh15, sl15 = find_swing_ict(df15, lookleft=20, lookright=5)
                    liq_lows  = [pdl, pwl] + [s['price'] for s in sl15[-6:]]
                    liq_highs = [pdh, pwh] + [s['price'] for s in sh15[-6:]]

                    # ③ M15 流動性掃蕩（四條件）
                    is_swept, sweep_info = check_liquidity_sweep(df15, side, liq_lows, liq_highs)
                    if not is_swept:
                        logging.info(f"[{instId}][{side}] 無掃損")
                        continue

                    # ④ M5 MSS + FVG → 進場點
                    fvg_ok, fvg_info = find_mss_fvg_entry(instId, side)
                    if not fvg_ok:
                        logging.info(f"[{instId}][{side}] MSS/FVG 未確認")
                        continue

                    # 確認進場點與 SL 合理性
                    entry = fvg_info['entry']
                    sl    = sweep_info['sweep_candle_sl']

                    # SL 必須在進場點「正確一側」
                    if side == "LONG"  and sl >= entry: continue
                    if side == "SHORT" and sl <= entry: continue

                    risk     = abs(entry - sl) + 1e-10
                    risk_pct = risk / (entry + 1e-10) * 100
                    if risk_pct < SL_MIN_PCT * 100:
                        logging.info(f"[{instId}][{side}] SL 太緊 ({risk_pct:.2f}%)")
                        continue

                    # 計算 TP
                    tp1, tp2, tp3 = calculate_ict_tps(entry, sl, side, h4_tp3)

                    setup_found = {
                        'side'      : side,
                        'entry'     : entry,
                        'sl'        : sl,
                        'tp1'       : tp1,
                        'tp2'       : tp2,
                        'tp3'       : tp3,
                        'risk_pct'  : risk_pct,
                        'h4_label'  : h4_label,
                        'h4_tp3'    : h4_tp3,
                        'sweep_info': sweep_info,
                        'fvg_info'  : fvg_info,
                    }
                    break   # 找到一個方向就停止

                if setup_found is None:
                    time.sleep(0.3); continue

                s = setup_found
                funding, ls_ratio = get_funding_ls(instId)
                cvd_label = calculate_cvd(df15)
                side_zh   = "🟢 多單 (LONG)" if s['side']=="LONG" else "🔴 空單 (SHORT)"
                fi        = s['fvg_info']

                msg  = f"🔥 *Alpha Oracle ICT 訊號* 🔥\n"
                msg += f"──────────────────\n"
                msg += f"💎 幣種：#{coin_sym}  |  {kz_label}\n"
                msg += f"🎯 方向：{side_zh}\n"
                msg += f"📐 形態：掃損反轉 + {fi['fvg_type']}\n"
                msg += f"\n"
                msg += f"🏗️ H4 偏向：{s['h4_label']}\n"
                msg += f"🎣 流動性水位：{s['sweep_info']['liq_level']:.4f} 已掃蕩\n"
                msg += f"📊 M5 MSS 突破：{fi['mss_level']:.4f}\n"
                msg += f"📦 FVG：{fi['fvg_low']:.4f} – {fi['fvg_high']:.4f}\n"
                msg += f"\n"
                msg += f"📍 *進場 CE*：`{s['entry']:.4f}`  (FVG 50%)\n"
                msg += f"🚫 *止損 SL*：`{s['sl']:.4f}`  (掃損棒 {'低' if s['side']=='LONG' else '高'}點, -{s['risk_pct']:.1f}%)\n"
                msg += f"🔒 BE 觸發  ：`{s['tp1']:.4f}`  (1.5R → 移 SL 到成本)\n"
                msg += f"💰 TP1 1:2  ：`{s['tp2']:.4f}`\n"
                msg += f"🚀 TP2 H4   ：`{s['tp3']:.4f}`\n"
                msg += f"\n"
                msg += f"📊 多空比 {ls_ratio} | 資費 {funding} | {cvd_label}\n"
                msg += f"\n"
                msg += f"⏳ *等待價格回測 FVG CE 並確認拒絕棒進場*"
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

            # ── WAITING：等待 FVG CE 回測 ──────────────────────────────
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
                    # 等拒絕確認棒（收盤確認方向）
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

                    # 確認棒出現 → 進場
                    t['status'] = "ACTIVE"
                    side_zh = "🟢 多單" if t['side']=="LONG" else "🔴 空單"
                    send_tg(
                        f"🚀 *Alpha Oracle | ICT 確認進場*\n"
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

            # ── ACTIVE：持倉管理 ──────────────────────────────────────
            elif t['status'] == "ACTIVE":
                act_n = min(3, len(df15))
                acl, ach = fetch_current_candle_hl(instId)
                act_low  = min(df15['l'].iloc[-act_n:].min(), acl, curr_p)
                act_high = max(df15['h'].iloc[-act_n:].max(), ach, curr_p)

                # ─ TP1 觸發：1.5R BE 點 ─────────────────────────────
                if t['locked'] == 0 and (
                    (t['side']=="LONG"  and act_high >= t['tp1']) or
                    (t['side']=="SHORT" and act_low  <= t['tp1'])
                ):
                    t['locked']   = 1
                    t['tp1_hit']  = 1
                    t['sl']       = t['entry']   # SL 移到成本

                    # ⭐ 統計上記為勝利（達到 1.5R）
                    pd.DataFrame([{"instId": instId, "result": "TP", "date": today_str}]).to_csv(
                        STATS_FILE, mode='a', header=False, index=False
                    )
                    send_tg(
                        f"🔒 *Alpha Oracle | BE 觸發*\n"
                        f"──────────────────\n"
                        f"💎 #{coin_sym}  ✅ 1.5R 達標\n"
                        f"🔒 SL 自動移至成本：`{t['entry']:.4f}`\n"
                        f"📊 *統計記為勝利（保本以上）*\n\n"
                        f"📍 當前：`{curr_p:.4f}`\n"
                        f"💰 TP1 (1:2)：`{t['tp2']:.4f}`\n"
                        f"🚀 TP2 H4  ：`{t['tp3']:.4f}`\n\n"
                        f"✨ *最壞結果：保本出場*"
                    )

                # ─ TP2 觸發：2R 主目標 ──────────────────────────────
                if t['locked'] == 1 and (
                    (t['side']=="LONG"  and act_high >= t['tp2']) or
                    (t['side']=="SHORT" and act_low  <= t['tp2'])
                ):
                    t['locked'] = 2
                    t['sl']     = t['tp1']   # SL 升級到 +1.5R
                    send_tg(
                        f"💰 *Alpha Oracle | TP1 (1:2) 達標*\n"
                        f"──────────────────\n"
                        f"💎 #{coin_sym}  ✅ 2R 達標\n"
                        f"🔒 SL 升級至 +1.5R：`{t['tp1']:.4f}`\n\n"
                        f"🚀 追擊 TP2 H4 目標：`{t['tp3']:.4f}`"
                    )

                # ─ 判斷最終出場 ─────────────────────────────────────
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

                    # 統計只在 SL（locked==0）時再寫，BE 以上的在 1.5R 時已寫
                    if not (is_sl and t['locked'] >= 1):   # 排除保本出場
                        res = "TP" if is_tp3 else "SL"
                        if t['locked'] == 0:  # 原始止損，還沒寫過統計
                            pd.DataFrame([{"instId": instId, "result": res, "date": today_str}]).to_csv(
                                STATS_FILE, mode='a', header=False, index=False
                            )

                    send_tg(
                        f"🏁 *Alpha Oracle | 交易結算*\n"
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

            # ── COOLDOWN ──────────────────────────────────────────────
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
