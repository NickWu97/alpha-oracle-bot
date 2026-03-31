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

# 監控幣種
ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP", 
    "BCH-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP"
]

LOG_FILE = "active_trades.csv"
STATS_FILE = "daily_stats.csv"

# --- 數據工具 ---
def get_market_metrics(instId):
    try:
        base_id = instId.replace("-SWAP", "")
        f_url = f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}"
        funding = requests.get(f_url, timeout=5).json()['data'][0]['fundingRate']
        ls_url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}"
        ls_ratio = requests.get(ls_url, timeout=5).json()['data'][0]['ratio']
        cvd_url = f"https://www.okx.com/api/v5/rubik/stat/taker-volume?instId={base_id}"
        cvd_data = requests.get(cvd_url, timeout=5).json()['data'][0]
        cvd_status = "🔥 買盤" if float(cvd_data['buyVol']) > float(cvd_data['sellVol']) else "🧊 賣盤"
        return {"funding": f"{float(funding)*100:.4f}%", "ls": ls_ratio, "cvd": cvd_status}
    except: return {"funding": "N/A", "ls": "N/A", "cvd": "N/A"}

def send_tg(msg):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try: requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except: pass

def fetch_okx(instId):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=15m&limit=100"
        res = requests.get(url, timeout=10).json()
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except: return None

def find_smc_setup(df):
    if df is None or len(df) < 35: return None
    for i in range(len(df)-3, len(df)-15, -1):
        k1, k2, k3 = df.iloc[i], df.iloc[i+1], df.iloc[i+2]
        # 多頭 BOS
        if k3['c'] > k3['o'] and k3['l'] > k1['h'] and k3['h'] > df.iloc[i-15:i]['h'].max():
            return {"side": "LONG", "entry": k1['h'], "sl": k1['l']}
        # 空頭 BOS
        if k3['c'] < k3['o'] and k3['h'] < k1['l'] and k3['l'] < df.iloc[i-15:i]['l'].min():
            return {"side": "SHORT", "entry": k1['l'], "sl": k1['h']}
    return None

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    
    # 檔案初始化 (確保 CSV 格式正確)
    if not os.path.exists(LOG_FILE):
        pd.DataFrame(columns=["instId","side","status","entry","sl","tp1","tp2","tp3","locked"]).to_csv(LOG_FILE, index=False)
    if not os.path.exists(STATS_FILE):
        pd.DataFrame(columns=["instId","result"]).to_csv(STATS_FILE, index=False)

    # --- 1. ☀️ 08:30 市場早報 ---
    if now_tw.hour == 8 and 30 <= now_tw.minute < 45:
        if not os.path.exists("morning.ok"):
            report = f"☀️ *Alpha Oracle | 市場早報*\n──────────────────\n🗓 {now_tw.strftime('%Y/%m/%d %H:%M')}\n\n"
            for coin in ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]:
                m = get_market_metrics(coin)
                report += f"🪙 {coin.split('-')[0]}: {m['cvd']} | LS: {m['ls']}\n"
            report += "\n📊 *今日策略：* 關注盤面回踩，嚴守止損。"
            send_tg(report)
            with open("morning.ok", "w") as f: f.write("done")
    elif now_tw.hour != 8 and os.path.exists("morning.ok"):
        os.remove("morning.ok")

    # --- 2. 🌙 00:00 勝率總結晚報 ---
    if now_tw.hour == 0 and 0 <= now_tw.minute < 15:
        if not os.path.exists("midnight.ok"):
            df_s = pd.read_csv(STATS_FILE)
            tp_c = len(df_s[df_s['result'] == 'TP'])
            sl_c = len(df_s[df_s['result'] == 'SL'])
            wr = (tp_c / (tp_c + sl_c) * 100) if (tp_c + sl_c) > 0 else 0
            
            summary = f"🌙 *Alpha Oracle | 每日實戰結算*\n──────────────────\n"
            summary += f"🗓 結算日期：{(now_tw - timedelta(days=1)).strftime('%Y/%m/%d')}\n"
            summary += f"✅ 止盈次數：{tp_c}\n❌ 止損次數：{sl_c}\n🔥 今日勝率：*{wr:.1f}%*\n──────────────────"
            send_tg(summary)
            # 每日結算完後重置戰績
            pd.DataFrame(columns=["instId","result"]).to_csv(STATS_FILE, index=False)
            with open("midnight.ok", "w") as f: f.write("done")
    elif now_tw.hour != 0 and os.path.exists("midnight.ok"):
        os.remove("midnight.ok")

    # --- 3. 核心監控邏輯 ---
    trades_df = pd.read_csv(LOG_FILE)
    active_ids = trades_df['instId'].tolist()
    new_trades = []

    for instId in ALL_COINS:
        df = fetch_okx(instId)
        if df is None or df.empty: continue
        curr_p = df['c'].iloc[-1]

        # 搜尋新訊號
        if instId not in active_ids:
            setup = find_smc_setup(df)
            if setup:
                m = get_market_metrics(instId)
                risk = abs(setup['entry'] - setup['sl'])
                tp1 = setup['entry'] + risk*1.5 if setup['side']=="LONG" else setup['entry'] - risk*1.5
                tp2 = setup['entry'] + risk*2.0 if setup['side']=="LONG" else setup['entry'] - risk*2.0
                tp3 = setup['entry'] + risk*3.0 if setup['side']=="LONG" else setup['entry'] - risk*3.0
                
                msg = f"🔍 *Alpha Oracle | 發現機會*\n──────────────────\n"
                msg += f"💎 幣種：#{instId.split('-')[0]}\n🎯 動作：{'🟢 多' if setup['side']=='LONG' else '🔴 空'}\n"
                msg += f"📊 數據：CVD {m['cvd']} | LS {m['ls']}\n\n"
                msg += f"📍 **建議進場：`{setup['entry']:.4f}`**\n"
                msg += f"🚫 **初期止損：`{setup['sl']:.4f}`**\n"
                msg += f"💰 **目標 TP3：`{setup['tp3'] if 'tp3' in setup else tp3:.4f}`**\n\n"
                msg += "💡 *等待回踩成交，達 TP2 自動移動止損。*"
                send_tg(msg)
                new_trades.append({"instId":instId,"side":setup['side'],"status":"WAITING","entry":setup['entry'],"sl":setup['sl'],"tp1":tp1,"tp2":tp2,"tp3":tp3,"locked":0})
            continue

        # 監控進行中的訂單
        t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
        if t['status'] == "WAITING":
            if (t['side'] == "LONG" and curr_p <= t['entry']) or (t['side'] == "SHORT" and curr_p >= t['entry']):
                t['status'] = "ACTIVE"
                send_tg(f"🚀 *回補成交* | #{instId.split('-')[0]}\n✅ 成交價：`{curr_p}`")
            new_trades.append(t)
        elif t['status'] == "ACTIVE":
            # 鎖利判定 (TP2)
            is_tp2 = (t['side']=="LONG" and curr_p >= t['tp2']) or (t['side']=="SHORT" and curr_p <= t['tp2'])
            if is_tp2 and t['locked'] == 0:
                t['locked'] = 1
                send_tg(f"🔒 *鎖定利潤提醒* | #{instId.split('-')[0]}\n⚠️ 達 TP2，將止損移至 **TP1 (`{t['tp1']:.4f}`)**！")
            
            # 止盈損判定 (若鎖利則看 TP1)
            current_sl = t['tp1'] if t['locked'] == 1 else t['sl']
            is_sl = (t['side']=="LONG" and curr_p <= current_sl) or (t['side']=="SHORT" and curr_p >= current_sl)
            is_tp3 = (t['side']=="LONG" and df['h'].max() >= t['tp3']) or (t['side']=="SHORT" and df['l'].min() <= t['tp3'])
            
            if is_sl:
                res = "🔒 鎖盈離場" if t['locked'] == 1 else "❌ 止損離場"
                send_tg(f"⚠️ *離場公告* | #{instId.split('-')[0]}\n結果：{res}")
                pd.DataFrame([{"instId":instId,"result":"SL"}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                continue
            if is_tp3:
                send_tg(f"🚀 *止盈公告：TP3 已達成* | #{instId.split('-')[0]} 💰")
                pd.DataFrame([{"instId":instId,"result":"TP"}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                continue
            new_trades.append(t)

    pd.DataFrame(new_trades).to_csv(LOG_FILE, index=False)

if __name__ == "__main__":
    main()
