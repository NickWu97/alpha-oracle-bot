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
    "ZAMA-USDT-SWAP", "BCH-USDT-SWAP", "ASI-USDT-SWAP", "DOGE-USDT-SWAP"
]

LOG_FILE = "active_trades.csv"
STATS_FILE = "daily_stats.csv"

# --- 數據與風險模組 ---
def get_market_metrics(instId):
    """獲取資費、多空比、CVD"""
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
    except:
        return {"funding": "N/A", "ls": "N/A", "cvd": "N/A"}

def calculate_leverage(entry, sl):
    """根據止損空間計算建議槓桿 (風險控制)"""
    try:
        gap = abs(entry - sl) / entry
        # 邏輯：止損越遠槓桿越低，確保單筆虧損固定
        lev = int(0.1 / gap) # 基礎係數 0.1
        return max(1, min(lev, 20)) # 限制在 1-20 倍之間
    except: return 5

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

# --- SMC 邏輯 ---
def find_smc_setup(df):
    if len(df) < 30: return None
    for i in range(len(df)-3, len(df)-15, -1):
        k1, k2, k3 = df.iloc[i], df.iloc[i+1], df.iloc[i+2]
        # 多頭 BOS + FVG
        if k3['c'] > k3['o'] and k3['l'] > k1['h'] and k3['h'] > df.iloc[i-15:i]['h'].max():
            return {"side": "LONG", "entry": k1['h'], "sl": k1['l']}
        # 空頭 BOS + FVG
        if k3['c'] < k3['o'] and k3['h'] < k1['l'] and k3['l'] < df.iloc[i-15:i]['l'].min():
            return {"side": "SHORT", "entry": k1['l'], "sl": k1['h']}
    return None

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    
    # 初始化檔案
    if not os.path.exists(LOG_FILE):
        pd.DataFrame(columns=["instId","side","status","entry","sl","tp1","tp2","tp3","time"]).to_csv(LOG_FILE, index=False)
    if not os.path.exists(STATS_FILE):
        pd.DataFrame(columns=["instId","result","time"]).to_csv(STATS_FILE, index=False)

    # --- 1. 每日 08:30 戰績結算 (僅計入成交單) ---
    if now_tw.hour == 8 and 30 <= now_tw.minute < 45:
        if not os.path.exists("daily_report.ok"):
            df_s = pd.read_csv(STATS_FILE)
            if not df_s.empty:
                df_s['time'] = pd.to_datetime(df_s['time'])
                today_s = df_s[df_s['time'] > (now_tw - timedelta(days=1))]
                tp_c = len(today_s[today_s['result'] == 'TP'])
                sl_c = len(today_s[today_s['result'] == 'SL'])
                wr = (tp_c / (tp_c + sl_c) * 100) if (tp_c + sl_c) > 0 else 0
                
                msg = f"📊 *Alpha Oracle | 當日實戰回報*\n──────────────────\n"
                msg += f"✅ 成功進場並止盈：{tp_c}\n❌ 成功進場並止損：{sl_c}\n"
                msg += f"🔥 實戰總勝率：*{wr:.1f}%*\n"
                msg += "──────────────────\n💡 *註：未回補踏空的單子不計入統計。*"
                send_tg(msg)
            with open("daily_report.ok", "w") as f: f.write("done")
    elif now_tw.hour != 8 and os.path.exists("daily_report.ok"):
        os.remove("daily_report.ok")

    # --- 2. 核心循環 ---
    trades_df = pd.read_csv(LOG_FILE)
    active_ids = trades_df['instId'].tolist()
    new_active_list = []

    for instId in ALL_COINS:
        df = fetch_okx(instId)
        if df is None: continue
        curr_p = df['c'].iloc[-1]

        # A. 搜尋新訊號
        if instId not in active_ids:
            setup = find_smc_setup(df)
            if setup:
                m = get_market_metrics(instId)
                lev = calculate_leverage(setup['entry'], setup['sl'])
                risk = abs(setup['entry'] - setup['sl'])
                tp1, tp2, tp3 = setup['entry']+risk*1.5, setup['entry']+risk*2.0, setup['entry']+risk*3.0
                if setup['side'] == "SHORT":
                    tp1, tp2, tp3 = setup['entry']-risk*1.5, setup['entry']-risk*2.0, setup['entry']-risk*3.0

                msg = f"🔍 *發現潛在機會 (等待回補)*\n──────────────────\n"
                msg += f"💎 幣種：#{instId.split('-')[0]}\n🎯 動作：{'🟢 做多' if setup['side']=='LONG' else '🔴 做空'}\n"
                msg += f"📊 數據：CVD {m['cvd']} | LS {m['ls']} | 資費 {m['funding']}\n\n"
                msg += f"📍 建議進場位：`{setup['entry']:.4f}`\n🚫 止損位 (SL)：`{setup['sl']:.4f}`\n"
                msg += f"💰 止盈位 (TP3)：`{tp3:.4f}`\n⚖️ **風險建議槓桿：{lev}x**\n"
                msg += "──────────────────\n💡 *狀態：尚未成交，價格回踩後會再通知。*"
                send_tg(msg)
                new_active_list.append({"instId":instId,"side":setup['side'],"status":"WAITING","entry":setup['entry'],"sl":setup['sl'],"tp1":tp1,"tp2":tp2,"tp3":tp3,"time":now_tw})
            continue

        # B. 監控持倉狀態
        t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
        if t['status'] == "WAITING":
            is_hit = (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry'])
            if is_hit:
                t['status'] = "ACTIVE"
                send_tg(f"🚀 *【確認進場】* | #{instId.split('-')[0]}\n✅ 價格已回補至進場區間：`{curr_p}`\n🎯 最終目標 TP3：`{t['tp3']:.4f}`")
            new_active_list.append(t)
        elif t['status'] == "ACTIVE":
            # 止損與止盈檢查
            if (t['side']=="LONG" and curr_p <= t['sl']) or (t['side']=="SHORT" and curr_p >= t['sl']):
                send_tg(f"❌ *結算：止損離場* | #{instId.split('-')[0]} 💸")
                pd.DataFrame([{"instId":instId,"result":"SL","time":now_tw}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                continue
            if (t['side']=="LONG" and df['h'].max() >= t['tp3']) or (t['side']=="SHORT" and df['l'].min() <= t['tp3']):
                send_tg(f"🚀 *結算：TP3 完美止盈* | #{instId.split('-')[0]} 💰")
                pd.DataFrame([{"instId":instId,"result":"TP","time":now_tw}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                continue
            new_active_list.append(t)

    pd.DataFrame(new_active_list).to_csv(LOG_FILE, index=False)

if __name__ == "__main__":
    main()
