import requests
import os
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta

# --- 測試模式：手動執行若想立刻看排版請設為 True，正式運行請設為 False ---
TEST_MODE = False 

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 幣種清單 (5主流 + 8山寨)
ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP", 
    "ZAMA-USDT-SWAP", "BCH-USDT-SWAP", "ASI-USDT-SWAP", "DOGE-USDT-SWAP"
]

LOG_FILE = "active_trades.csv"
STATS_FILE = "daily_stats.csv"

# --- 數據獲取模組 ---
def get_market_metrics(instId):
    """獲取資費、多空比、CVD 主動買賣情況"""
    try:
        base_id = instId.replace("-SWAP", "")
        # 1. 資金費率
        f_url = f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}"
        funding = requests.get(f_url, timeout=5).json()['data'][0]['fundingRate']
        # 2. 多空持倉人數比
        ls_url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}"
        ls_ratio = requests.get(ls_url, timeout=5).json()['data'][0]['ratio']
        # 3. Taker 成交量 (模擬 CVD)
        cvd_url = f"https://www.okx.com/api/v5/rubik/stat/taker-volume?instId={base_id}"
        cvd_data = requests.get(cvd_url, timeout=5).json()['data'][0]
        cvd_status = "🔥 買盤強勢" if float(cvd_data['buyVol']) > float(cvd_data['sellVol']) else "🧊 賣盤強勢"
        
        return {"funding": f"{float(funding)*100:.4f}%", "ls": ls_ratio, "cvd": cvd_status}
    except:
        return {"funding": "N/A", "ls": "N/A", "cvd": "N/A"}

def send_tg(msg):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try: requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except: pass

def fetch_okx(instId, bar="15m", limit="100"):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except: return None

def find_smc_setup(df):
    """SMC 核心邏輯：BOS 突破 + FVG 識別"""
    if len(df) < 30: return None
    for i in range(len(df)-3, len(df)-12, -1):
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
    
    # 檔案初始化
    for f in [LOG_FILE, STATS_FILE]:
        if not os.path.exists(f):
            pd.DataFrame(columns=["instId","side","status","entry","sl","tp1","tp2","tp3"]).to_csv(f, index=False)

    # --- 1. 每日 08:30 總結勝率 ---
    if (now_tw.hour == 8 and 30 <= now_tw.minute < 45) or TEST_MODE:
        if not os.path.exists("daily_report.ok") or TEST_MODE:
            df_s = pd.read_csv(STATS_FILE)
            tp_c, sl_c = len(df_s[df_s['result'] == 'TP']), len(df_s[df_s['result'] == 'SL'])
            total = tp_c + sl_c
            wr = (tp_c / total * 100) if total > 0 else 0
            
            report = f"📊 *Alpha Oracle | 每日戰績結報*\n──────────────────\n"
            report += f"🗓 日期：{now_tw.strftime('%Y/%m/%d')}\n"
            report += f"✅ 止盈：{tp_c} | ❌ 止損：{sl_c}\n🔥 總勝率：*{wr:.1f}%*\n"
            report += "──────────────────\n💡 *昨日數據已歸零，開始新輪詢...*"
            send_tg(report)
            pd.DataFrame(columns=["instId","result"]).to_csv(STATS_FILE, index=False)
            if not TEST_MODE:
                with open("daily_report.ok", "w") as f: f.write("done")
    elif now_tw.hour != 8 and os.path.exists("daily_report.ok"):
        os.remove("daily_report.ok")

    # --- 2. 核心監控邏輯 ---
    trades_df = pd.read_csv(LOG_FILE)
    active_ids = trades_df['instId'].tolist()
    new_trades = []

    for instId in ALL_COINS:
        df = fetch_okx(instId)
        if df is None: continue
        curr_p = df['c'].iloc[-1]

        # A. 搜尋新訊號 (不重複報單)
        if instId not in active_ids:
            setup = find_smc_setup(df)
            if setup:
                m = get_market_metrics(instId)
                risk = abs(setup['entry'] - setup['sl'])
                tp1 = setup['entry'] + risk*1.5 if setup['side']=="LONG" else setup['entry'] - risk*1.5
                tp2 = setup['entry'] + risk*2.0 if setup['side']=="LONG" else setup['entry'] - risk*2.0
                tp3 = setup['entry'] + risk*3.0 if setup['side']=="LONG" else setup['entry'] - risk*3.0
                
                msg = f"🔥 *SMC 高勝率進場訊號*\n──────────────────\n\n"
                msg += f"💎 幣種：#{instId.split('-')[0]}\n🎯 動作：{'🟢 多' if setup['side']=='LONG' else '🔴 空'} (BOS + FVG)\n\n"
                msg += f"📊 市場數據：\n📈 CVD：{m['cvd']}\n👥 多空比：{m['ls']}\n💰 資費：{m['funding']}\n\n"
                msg += f"📍 建議進場位：{setup['entry']:.4f}\n🚫 止損位 (SL)：{setup['sl']:.4f}\n"
                msg += f"💰 止盈 (TP1)：{tp1:.4f}\n💰 止盈 (TP2)：{tp2:.4f}\n💰 止盈 (TP3)：{tp3:.4f}\n\n"
                msg += "💡 *等待價格回踩區間成交...*"
                send_tg(msg)
                new_trades.append({"instId":instId,"side":setup['side'],"status":"WAITING","entry":setup['entry'],"sl":setup['sl'],"tp1":tp1,"tp2":tp2,"tp3":tp3})
            continue

        # B. 監控成交與止盈損
        t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
        if t['status'] == "WAITING":
            if (t['side'] == "LONG" and curr_p <= t['entry']) or (t['side'] == "SHORT" and curr_p >= t['entry']):
                t['status'] = "ACTIVE"
                send_tg(f"🚀 *SMC 回踩成交* | #{instId.split('-')[0]}\n✅ 成交價：`{curr_p}`")
            new_trades.append(t)
        elif t['status'] == "ACTIVE":
            if (t['side'] == "LONG" and curr_p <= t['sl']) or (t['side'] == "SHORT" and curr_p >= t['sl']):
                send_tg(f"❌ *止損離場* | #{instId.split('-')[0]} 💸")
                pd.DataFrame([{"instId":instId,"result":"SL"}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                continue
            if (t['side'] == "LONG" and df['h'].max() >= t['tp3']) or (t['side'] == "SHORT" and df['l'].min() <= t['tp3']):
                send_tg(f"🚀 *TP3 終極止盈* | #{instId.split('-')[0]} 💰")
                pd.DataFrame([{"instId":instId,"result":"TP"}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                continue
            new_trades.append(t)

    pd.DataFrame(new_trades).to_csv(LOG_FILE, index=False)

if __name__ == "__main__":
    main()
