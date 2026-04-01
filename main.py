import requests
import os
import pandas as pd
import numpy as np
import logging
import traceback
from datetime import datetime, timedelta

# --- 1. 基礎配置與幣種清單 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 5 隻主流幣 + 5 隻山寨幣
MAIN_COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP"]
ALT_COINS = ["SUI-USDT-SWAP", "AVAX-USDT-SWAP", "ADA-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP"]
ALL_MONITOR = MAIN_COINS + ALT_COINS

LOG_FILE = "active_trades.csv"
STATS_FILE = "daily_stats.csv"

# --- 2. 技術指標與專業數據 ---

def get_advanced_metrics(instId):
    """獲取資費、多空比、CVD 趨勢"""
    try:
        base_id = instId.replace("-SWAP", "")
        f_res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5).json()
        funding = f"{float(f_res['data'][0]['fundingRate']) * 100:.4f}%"
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5).json()
        ls_ratio = ls_res['data'][0]['ratio']
        cvd_status = "🟢 買盤強勁" if float(ls_ratio) < 0.9 else "🔴 賣壓較重"
        return {"funding": funding, "ls_ratio": ls_ratio, "cvd": cvd_status}
    except:
        return {"funding": "N/A", "ls_ratio": "N/A", "cvd": "N/A"}

def calculate_atr(df):
    """計算 ATR 用於動態止損"""
    high_low = df['h'] - df['l']
    high_close = np.abs(df['h'] - df['c'].shift())
    low_close = np.abs(df['l'] - df['c'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(window=14).mean().iloc[-1]

def fetch_okx(instId):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=15m&limit=100"
        res = requests.get(url, timeout=10).json()
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except: return None

# --- 3. 雙重策略核心 (SMC Sweep + Choch) ---

def find_oracle_setup(df):
    if df is None or len(df) < 60: return None
    atr = calculate_atr(df)
    lookback = 40
    high_liq = df['h'].iloc[-lookback:-5].max()
    low_liq = df['l'].iloc[-lookback:-5].min()
    
    for i in range(len(df)-2, len(df)-15, -1):
        k_prev, k_mid, k_next = df.iloc[i-1], df.iloc[i], df.iloc[i+1]
        
        # 邏輯 A: SMC 流動性掃描 (Sweep + FVG)
        # 邏輯 B: 結構轉變 (Choch) - 突破前 15 根 K 高/低點
        
        # --- 多頭信號 ---
        is_liq_long = df['l'].iloc[-12:].min() < low_liq and k_next['l'] > k_prev['h']
        is_choch_long = k_next['c'] > k_next['o'] and k_next['c'] > df['h'].iloc[i-15:i].max()
        
        if is_liq_long or is_choch_long:
            entry = k_next['l'] if is_liq_long else (k_next['l'] + k_prev['h'])/2
            sl = k_mid['l'] - (0.4 * atr)
            tp = df['h'].iloc[-60:].max()
            r_val = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) != 0 else 0
            if entry > sl and tp > entry and r_val >= 1.5:
                return {"side": "LONG", "entry": entry, "sl": sl, "tp": tp, "r_ratio": round(r_val, 2), "reason": "SMC/Choch Bullish"}

        # --- 空頭信號 ---
        is_liq_short = df['h'].iloc[-12:].max() > high_liq and k_next['h'] < k_prev['l']
        is_choch_short = k_next['c'] < k_next['o'] and k_next['c'] < df['l'].iloc[i-15:i].min()

        if is_liq_short or is_choch_short:
            entry = k_next['h'] if is_liq_short else (k_next['h'] + k_prev['l'])/2
            sl = k_mid['h'] + (0.4 * atr)
            tp = df['l'].iloc[-60:].min()
            r_val = abs(entry - tp) / abs(sl - entry) if abs(sl - entry) != 0 else 0
            if entry < sl and tp < entry and r_val >= 1.5:
                return {"side": "SHORT", "entry": entry, "sl": sl, "tp": tp, "r_ratio": round(r_val, 2), "reason": "SMC/Choch Bearish"}
    return None

# --- 4. 報表與通知邏輯 ---

def send_tg(msg):
    if not TG_TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try: requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except: pass

def main():
    try:
        now_tw = datetime.utcnow() + timedelta(hours=8)
        manual_mode = os.getenv("REPORT_TYPE", "none")
        
        # 初始化檔案
        log_cols = ["instId","side","status","entry","sl","tp","r_ratio","locked"]
        for f, cols in [(LOG_FILE, log_cols), (STATS_FILE, ["instId","result"])]:
            if not os.path.exists(f) or os.stat(f).st_size == 0:
                pd.DataFrame(columns=cols).to_csv(f, index=False)

        # 🌙 [00:00 戰報]
        if (now_tw.hour == 0 and 0 <= now_tw.minute < 15) or manual_mode == "midnight":
            df_s = pd.read_csv(STATS_FILE)
            if not df_s.empty:
                tp_c, sl_c = len(df_s[df_s['result'] == 'TP']), len(df_s[df_s['result'] == 'SL'])
                wr = (tp_c / (tp_c + sl_c) * 100) if (tp_c + sl_c) > 0 else 0
                msg = f"📊 *Alpha Oracle 戰績回報*\n──────────────────\n✅ 獲利/保本：{tp_c}\n❌ 虧損離場：{sl_c}\n🔥 昨日勝率：*{wr:.1f}%*"
                send_tg(msg)
                if now_tw.hour == 0: pd.DataFrame(columns=["instId","result"]).to_csv(STATS_FILE, index=False)

        # ☕ [08:00 早報] 5主流 + 5山寨
        if (now_tw.hour == 8 and 0 <= now_tw.minute < 15) or manual_mode == "morning":
            m_msg = f"☕ *Alpha Oracle 晨間報報*\n──────────────────\n"
            for label, coins in [("💎 主流強勢", MAIN_COINS), ("🚀 山寨潛力", ALT_COINS)]:
                m_msg += f"\n【{label}】\n"
                for inst in coins:
                    df = fetch_okx(inst)
                    if df is not None:
                        curr_p, metrics = df['c'].iloc[-1], get_advanced_metrics(inst)
                        m_msg += f"• #{inst.split('-')[0]}: {curr_p} (LS: {metrics['ls_ratio']})\n"
            send_tg(m_msg + "\n💡 *今日思路：專注結構轉變後的反轉機會。*")

        # 核心追蹤邏輯
        try: trades_df = pd.read_csv(LOG_FILE)
        except: trades_df = pd.DataFrame(columns=log_cols)
        active_ids, updated_trades = trades_df['instId'].tolist(), []

        for instId in ALL_MONITOR:
            df = fetch_okx(instId)
            if df is None: continue
            curr_p, metrics = df['c'].iloc[-1], get_advanced_metrics(instId)

            if instId not in active_ids:
                setup = find_oracle_setup(df)
                if setup:
                    msg = f"🔍 *Alpha Oracle | 發現機會*\n──────────────────\n💎 幣種：#{instId.split('-')[0]}\n🎯 動作：{'🟢 多' if setup['side']=='LONG' else '🔴 空'}\n"
                    msg += f"📊 數據：{metrics['cvd']} | LS {metrics['ls_ratio']}\n📍 進場：{setup['entry']:.4f}\n🚫 止損：{setup['sl']:.4f}\n💰 止盈：{setup['tp']:.4f}\n📈 預期：*{setup['r_ratio']}R*\n\n⚠️ *等待結構回踩成交...*"
                    send_tg(msg)
                    setup.update({"instId": instId, "status": "WAITING", "locked": 0})
                    updated_trades.append(setup)
                continue

            t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
            if t['status'] == "WAITING":
                is_hit = (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry'])
                if is_hit:
                    t['status'] = "ACTIVE"
                    send_tg(f"🚀 *Alpha Oracle | 成交提醒*\n#{instId.split('-')[0]} 已成交，R值 {t['r_ratio']}")
                updated_trades.append(t)
            elif t['status'] == "ACTIVE":
                mid_point = (t['entry'] + t['tp']) / 2
                if t['locked'] == 0 and ((t['side']=="LONG" and curr_p >= mid_point) or (t['side']=="SHORT" and curr_p <= mid_point)):
                    t['locked'], t['sl'] = 1, t['entry']
                    send_tg(f"🔒 *Alpha Oracle | 鎖利保護*\n#{instId.split('-')[0]} 已鎖定保本。")
                
                is_sl, is_tp = (curr_p <= t['sl'] if t['side']=="LONG" else curr_p >= t['sl']), (curr_p >= t['tp'] if t['side']=="LONG" else curr_p <= t['tp'])
                if is_sl or is_tp:
                    res = "TP" if (is_tp or t['locked'] == 1) else "SL"
                    msg = f"🏁 *Alpha Oracle | 結算*\n#{instId.split('-')[0]} 結果：{'獲利/保本' if res=='TP' else '止損'}"
                    send_tg(msg)
                    pd.DataFrame([{"instId":instId,"result":res}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                    continue
                updated_trades.append(t)
        pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)
    except: traceback.print_exc()

if __name__ == "__main__": main()
