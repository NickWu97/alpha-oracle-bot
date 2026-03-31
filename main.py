import requests
import os
import pandas as pd
import numpy as np
import logging
import traceback
from datetime import datetime, timedelta

# --- 1. 基礎配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP", 
    "BCH-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP"
]

LOG_FILE = "active_trades.csv"
STATS_FILE = "daily_stats.csv"

# --- 2. 工具函數 ---
def send_tg(msg):
    if not TG_TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except: pass

def fetch_okx(instId, bar="15m", limit="100"):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if 'data' not in res or not res['data']: return None
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except: return None

def calculate_atr(df, window=14):
    high_low = df['h'] - df['l']
    high_close = np.abs(df['h'] - df['c'].shift())
    low_close = np.abs(df['l'] - df['c'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(window=window).mean().iloc[-1]

# --- 3. SMC 區域偵測 (OB/FVG) ---
def find_smc_setup(df):
    if df is None or len(df) < 40: return None
    atr = calculate_atr(df)
    for i in range(len(df)-3, len(df)-20, -1):
        k0, k1, k2 = df.iloc[i-1], df.iloc[i], df.iloc[i+1]
        # 多頭 Choch
        if k2['c'] > k2['o'] and k2['c'] > df['h'].iloc[i-15:i].max():
            entry = (k2['l'] + k0['h']) / 2 if k2['l'] > k0['h'] else (k1['l'] + k1['o']) / 2
            sl = k1['l'] - (0.4 * atr)
            return {"side": "LONG", "entry": entry, "sl": sl}
        # 空頭 Choch
        if k2['c'] < k2['o'] and k2['c'] < df['l'].iloc[i-15:i].min():
            entry = (k2['h'] + k0['l']) / 2 if k2['h'] < k0['l'] else (k1['h'] + k1['o']) / 2
            sl = k1['h'] + (0.4 * atr)
            return {"side": "SHORT", "entry": entry, "sl": sl}
    return None

# --- 4. 主程式 ---
def main():
    try:
        now_tw = datetime.utcnow() + timedelta(hours=8)
        manual_report = os.getenv("MANUAL_REPORT", "false").lower() == "true"
        print(f"🚀 Alpha Oracle 啟動 | 時間: {now_tw.strftime('%H:%M')}")
        
        # --- 檔案初始化 (防崩潰強化) ---
        log_cols = ["instId","side","status","entry","sl","tp1","tp2","tp3","locked"]
        stats_cols = ["instId","result"]
        
        if not os.path.exists(LOG_FILE) or os.stat(LOG_FILE).st_size == 0:
            pd.DataFrame(columns=log_cols).to_csv(LOG_FILE, index=False)
        if not os.path.exists(STATS_FILE) or os.stat(STATS_FILE).st_size == 0:
            pd.DataFrame(columns=stats_cols).to_csv(STATS_FILE, index=False)
        
        # --- A. 戰績回報 ---
        is_midnight = (now_tw.hour == 0 and 0 <= now_tw.minute < 15)
        if is_midnight or manual_report:
            if not os.path.exists("midnight.ok") or manual_report:
                df_s = pd.read_csv(STATS_FILE)
                if not df_s.empty:
                    tp_c = len(df_s[df_s['result'] == 'TP'])
                    sl_c = len(df_s[df_s['result'] == 'SL'])
                    wr = (tp_c / (tp_c + sl_c) * 100) if (tp_c + sl_c) > 0 else 0
                    msg = f"📊 *Alpha Oracle 戰績結算*\n──────────────────\n✅ 止盈：{tp_c} | ❌ 止損：{sl_c}\n🔥 勝率：*{wr:.1f}%*"
                    send_tg(msg)
                    if is_midnight:
                        pd.DataFrame(columns=stats_cols).to_csv(STATS_FILE, index=False)
                        with open("midnight.ok", "w") as f: f.write("ok")
                elif manual_report:
                    send_tg("📭 目前尚無結算數據。")
        elif now_tw.hour != 0 and os.path.exists("midnight.ok"):
            os.remove("midnight.ok")

        # --- B. 核心監控邏輯 ---
        trades_df = pd.read_csv(LOG_FILE)
        active_ids = trades_df['instId'].tolist()
        updated_trades = []

        for instId in ALL_COINS:
            df = fetch_okx(instId)
            if df is None or df.empty: continue
            curr_p = df['c'].iloc[-1]

            if instId not in active_ids:
                setup = find_smc_setup(df)
                if setup:
                    risk = abs(setup['entry'] - setup['sl'])
                    tp1, tp2, tp3 = setup['entry'] + risk*1.5 if setup['side']=="LONG" else setup['entry'] - risk*1.5, \
                                    setup['entry'] + risk*2.0 if setup['side']=="LONG" else setup['entry'] - risk*2.0, \
                                    setup['entry'] + risk*3.0 if setup['side']=="LONG" else setup['entry'] - risk*3.0
                    
                    send_tg(f"🎯 *掛單提醒* | #{instId.split('-')[0]}\n方向：{'🟢 多' if setup['side']=='LONG' else '🔴 空'}\n進場：`{setup['entry']:.4f}`\n止損：`{setup['sl']:.4f}`")
                    updated_trades.append({"instId":instId,"side":setup['side'],"status":"WAITING","entry":setup['entry'],"sl":setup['sl'],"tp1":tp1,"tp2":tp2,"tp3":tp3,"locked":0})
                continue

            # 持倉跟蹤
            t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
            if t['status'] == "WAITING":
                if (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry']):
                    t['status'] = "ACTIVE"
                    send_tg(f"🚀 *掛單成交* | #{instId.split('-')[0]}")
                updated_trades.append(t)
            elif t['status'] == "ACTIVE":
                if t['locked'] == 0:
                    is_tp2 = (t['side']=="LONG" and curr_p >= t['tp2']) or (t['side']=="SHORT" and curr_p <= t['tp2'])
                    if is_tp2:
                        t['locked'] = 1
                        t['sl'] = t['tp1']
                        send_tg(f"🔒 *鎖利* | #{instId.split('-')[0]} 止損移至 1.5R")
                
                is_sl = (t['side']=="LONG" and curr_p <= t['sl']) or (t['side']=="SHORT" and curr_p >= t['sl'])
                is_tp3 = (t['side']=="LONG" and curr_p >= t['tp3']) or (t['side']=="SHORT" and curr_p <= t['tp3'])
                
                if is_sl or is_tp3:
                    res_val = "TP" if is_tp3 else "SL"
                    send_tg(f"🏁 *結算* | #{instId.split('-')[0]}\n結果：{'💰 止盈' if is_tp3 else '🛡️ 離場'}")
                    pd.DataFrame([{"instId":instId,"result":res_val}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                    continue
                updated_trades.append(t)

        pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)
        
    except Exception as e:
        traceback.print_exc()
        exit(1)

if __name__ == "__main__":
    main()
