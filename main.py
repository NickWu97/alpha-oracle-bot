import requests
import os
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta

# --- 基礎配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 監控幣種 (5主流 + 8山寨)
ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP", 
    "BCH-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP"
]

LOG_FILE = "active_trades.csv"
STATS_FILE = "daily_stats.csv"

# --- 數據模組 ---
def get_market_metrics(instId):
    try:
        base_id = instId.replace("-SWAP", "")
        f_url = f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}"
        funding = requests.get(f_url, timeout=5).json()['data'][0]['fundingRate']
        ls_url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}"
        ls_ratio = requests.get(ls_url, timeout=5).json()['data'][0]['ratio']
        cvd_url = f"https://www.okx.com/api/v5/rubik/stat/taker-volume?instId={base_id}"
        cvd_data = requests.get(cvd_url, timeout=5).json()['data'][0]
        cvd_status = "🔥 買盤強勢" if float(cvd_data['buyVol']) > float(cvd_data['sellVol']) else "🧊 賣盤強勢"
        return {"funding": f"{float(funding)*100:.4f}%", "ls": ls_ratio, "cvd": cvd_status}
    except: return {"funding": "N/A", "ls": "N/A", "cvd": "N/A"}

def calculate_leverage(entry, sl):
    try:
        gap = abs(entry - sl) / entry
        lev = int(0.12 / gap) 
        return max(1, min(lev, 20))
    except: return 5

def send_tg(msg):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try: requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except: pass

def fetch_okx(instId):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=15m&limit=100"
        res = requests.get(url, timeout=10).json()
        if 'data' not in res or not res['data']: return None
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        valid_df = df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
        return valid_df if not valid_df.empty else None
    except: return None

def find_smc_setup(df):
    if df is None or len(df) < 35: return None
    for i in range(len(df)-3, len(df)-15, -1):
        k1, k2, k3 = df.iloc[i], df.iloc[i+1], df.iloc[i+2]
        if k3['c'] > k3['o'] and k3['l'] > k1['h'] and k3['h'] > df.iloc[i-15:i]['h'].max():
            return {"side": "LONG", "entry": k1['h'], "sl": k1['l']}
        if k3['c'] < k3['o'] and k3['h'] < k1['l'] and k3['l'] < df.iloc[i-15:i]['l'].min():
            return {"side": "SHORT", "entry": k1['l'], "sl": k1['h']}
    return None

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    
    if not os.path.exists(LOG_FILE):
        pd.DataFrame(columns=["instId","side","status","entry","sl","tp1","tp2","tp3","locked","time"]).to_csv(LOG_FILE, index=False)
    if not os.path.exists(STATS_FILE):
        pd.DataFrame(columns=["instId","result","time"]).to_csv(STATS_FILE, index=False)

    # --- 1. 每日 00:00 結算 ---
    if now_tw.hour == 0 and 0 <= now_tw.minute < 15:
        if not os.path.exists("midnight_report.ok"):
            df_s = pd.read_csv(STATS_FILE)
            if not df_s.empty:
                df_s['time'] = pd.to_datetime(df_s['time'])
                yesterday = now_tw - timedelta(days=1)
                today_s = df_s[df_s['time'] > yesterday]
                tp_c, sl_c = len(today_s[today_s['result'] == 'TP']), len(today_s[today_s['result'] == 'SL'])
                wr = (tp_c / (tp_c + sl_c) * 100) if (tp_c + sl_c) > 0 else 0
                msg = f"📊 *Alpha Oracle | 每日實戰結報*\n──────────────────\n🗓 結算：{yesterday.strftime('%Y/%m/%d')}\n✅ 止盈：{tp_c} | ❌ 止損：{sl_c}\n🔥 勝率：*{wr:.1f}%*\n──────────────────"
                send_tg(msg)
                pd.DataFrame(columns=["instId","result","time"]).to_csv(STATS_FILE, index=False)
            with open("midnight_report.ok", "w") as f: f.write("done")
    elif now_tw.hour != 0 and os.path.exists("midnight_report.ok"):
        os.remove("midnight_report.ok")

    # --- 2. 核心監控 ---
    trades_df = pd.read_csv(LOG_FILE)
    active_ids = trades_df['instId'].tolist()
    new_active_list = []

    for instId in ALL_COINS:
        df = fetch_okx(instId)
        if df is None or df.empty: continue
        curr_p = df['c'].iloc[-1]

        if instId not in active_ids:
            setup = find_smc_setup(df)
            if setup:
                m = get_market_metrics(instId)
                lev = calculate_leverage(setup['entry'], setup['sl'])
                risk = abs(setup['entry'] - setup['sl'])
                
                # 計算點位 1.5R / 2R / 3R
                tp1 = setup['entry'] + (risk * 1.5) if setup['side'] == "LONG" else setup['entry'] - (risk * 1.5)
                tp2 = setup['entry'] + (risk * 2.0) if setup['side'] == "LONG" else setup['entry'] - (risk * 2.0)
                tp3 = setup['entry'] + (risk * 3.0) if setup['side'] == "LONG" else setup['entry'] - (risk * 3.0)
                sl = setup['sl']
                
                msg = f"🔍 *發現機會 (等待回補)*\n──────────────────\n"
                msg += f"💎 幣種：#{instId.split('-')[0]}\n🎯 動作：{'🟢 多' if setup['side']=='LONG' else '🔴 空'}\n"
                msg += f"📊 數據：CVD {m['cvd']} | LS {m['ls']}\n\n"
                msg += f"📍 **進場位：`{setup['entry']:.4f}`**\n"
                msg += f"🚫 **止損位 (SL)：`{sl:.4f}`**\n\n"
                msg += f"💰 **TP1 (1.5R)：`{tp1:.4f}`**\n"
                msg += f"💰 **TP2 (2.0R)：`{tp2:.4f}`**\n"
                msg += f"💰 **TP3 (3.0R)：`{tp3:.4f}`**\n"
                msg += f"⚖️ **建議槓桿：{lev}x**\n──────────────────"
                
                send_tg(msg)
                new_active_list.append({"instId":instId,"side":setup['side'],"status":"WAITING","entry":setup['entry'],"sl":sl,"tp1":tp1,"tp2":tp2,"tp3":tp3,"locked":0,"time":now_tw})
            continue

        t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
        if t['status'] == "WAITING":
            is_hit = (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry'])
            if is_hit:
                t['status'] = "ACTIVE"
                send_tg(f"🚀 *【確認進場】* | #{instId.split('-')[0]}\n✅ 已回補成交：`{curr_p}`")
            new_active_list.append(t)
        elif t['status'] == "ACTIVE":
            # TP2 鎖利邏輯
            is_tp2 = (t['side']=="LONG" and curr_p >= t['tp2']) or (t['side']=="SHORT" and curr_p <= t['tp2'])
            if is_tp2 and t['locked'] == 0:
                t['locked'] = 1
                send_tg(f"🔒 *鎖定利潤提醒* | #{instId.split('-')[0]}\n⚠️ 已達 **TP2 (2.0R)**，請將 SL 移至 **TP1 (`{t['tp1']:.4f}`)** 鎖定獲利！")
            
            current_sl = t['tp1'] if t['locked'] == 1 else t['sl']
            is_sl = (t['side']=="LONG" and curr_p <= current_sl) or (t['side']=="SHORT" and curr_p >= current_sl)
            is_tp3 = (t['side']=="LONG" and df['h'].max() >= t['tp3']) or (t['side']=="SHORT" and df['l'].min() <= t['tp3'])
            
            if is_sl:
                reason = "鎖利離場 (1.5R)" if t['locked'] == 1 else "止損離場"
                send_tg(f"⚠️ *結算：{reason}* | #{instId.split('-')[0]}")
                pd.DataFrame([{"instId":instId,"result":"SL","time":now_tw}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                continue
            if is_tp3:
                send_tg(f"🚀 *結算：TP3 完美止盈 (3.0R)* | #{instId.split('-')[0]} 💰")
                pd.DataFrame([{"instId":instId,"result":"TP","time":now_tw}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                continue
            new_active_list.append(t)

    pd.DataFrame(new_active_list).to_csv(LOG_FILE, index=False)

if __name__ == "__main__":
    main()
