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

# --- 2. 進階數據工具 ---
def get_extra_metrics(instId):
    """獲取資費、多空人數比、持倉量"""
    try:
        base_id = instId.replace("-SWAP", "")
        # 1. 資費
        f_res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5).json()
        funding = f"{float(f_res['data'][0]['fundingRate']) * 100:.4f}%"
        # 2. 多空人數比
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5).json()
        ls_ratio = ls_res['data'][0]['ratio']
        # 3. 持倉量 (Open Interest)
        oi_res = requests.get(f"https://www.okx.com/api/v5/public/open-interest?instId={instId}", timeout=5).json()
        oi = oi_res['data'][0]['oi']
        return {"funding": funding, "ls_ratio": ls_ratio, "oi": oi}
    except:
        return {"funding": "N/A", "ls_ratio": "N/A", "oi": "N/A"}

def send_tg(msg):
    if not TG_TOKEN or not CHAT_ID: return
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

def calculate_atr(df):
    high_low = df['h'] - df['l']
    high_close = np.abs(df['h'] - df['c'].shift())
    low_close = np.abs(df['l'] - df['c'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(window=14).mean().iloc[-1]

def find_smc_setup(df):
    if df is None or len(df) < 40: return None
    atr = calculate_atr(df)
    for i in range(len(df)-3, len(df)-20, -1):
        k0, k1, k2 = df.iloc[i-1], df.iloc[i], df.iloc[i+1]
        # 多頭 Choch 偵測
        if k2['c'] > k2['o'] and k2['c'] > df['h'].iloc[i-15:i].max():
            entry = (k2['l'] + k0['h']) / 2 if k2['l'] > k0['h'] else (k1['l'] + k1['o']) / 2
            sl = k1['l'] - (0.4 * atr)
            return {"side": "LONG", "entry": entry, "sl": sl}
        # 空頭 Choch 偵測
        if k2['c'] < k2['o'] and k2['c'] < df['l'].iloc[i-15:i].min():
            entry = (k2['h'] + k0['l']) / 2 if k2['h'] < k0['l'] else (k1['h'] + k1['o']) / 2
            sl = k1['h'] + (0.4 * atr)
            return {"side": "SHORT", "entry": entry, "sl": sl}
    return None

# --- 4. 主程式邏輯 ---
def main():
    try:
        now_tw = datetime.utcnow() + timedelta(hours=8)
        manual_report = os.getenv("MANUAL_REPORT", "false").lower() == "true"
        
        # 強制初始化防呆
        log_cols = ["instId","side","status","entry","sl","tp1","tp2","tp3","locked"]
        if not os.path.exists(LOG_FILE) or os.stat(LOG_FILE).st_size == 0:
            pd.DataFrame(columns=log_cols).to_csv(LOG_FILE, index=False)
        if not os.path.exists(STATS_FILE) or os.stat(STATS_FILE).st_size == 0:
            pd.DataFrame(columns=["instId","result"]).to_csv(STATS_FILE, index=False)

        # 戰績結算報表 (午夜)
        if now_tw.hour == 0 and 0 <= now_tw.minute < 15 or manual_report:
            if not os.path.exists("midnight.ok") or manual_report:
                df_s = pd.read_csv(STATS_FILE)
                if not df_s.empty:
                    tp_c, sl_c = len(df_s[df_s['result'] == 'TP']), len(df_s[df_s['result'] == 'SL'])
                    wr = (tp_c / (tp_c + sl_c) * 100) if (tp_c + sl_c) > 0 else 0
                    send_tg(f"📊 *Alpha Oracle 戰績回報*\n──────────────────\n✅ 止盈：{tp_c} | ❌ 止損：{sl_c}\n🔥 勝率：*{wr:.1f}%*")
                    if not manual_report:
                        pd.DataFrame(columns=["instId","result"]).to_csv(STATS_FILE, index=False)
                        with open("midnight.ok", "w") as f: f.write("ok")
        elif now_tw.hour != 0 and os.path.exists("midnight.ok"):
            os.remove("midnight.ok")

        # 核心監控
        trades_df = pd.read_csv(LOG_FILE)
        active_ids = trades_df['instId'].tolist()
        updated_trades = []

        for instId in ALL_COINS:
            df = fetch_okx(instId)
            if df is None or df.empty: continue
            curr_p, m = df['c'].iloc[-1], get_extra_metrics(instId)

            if instId not in active_ids:
                setup = find_smc_setup(df)
                if setup:
                    risk = abs(setup['entry'] - setup['sl'])
                    tp1, tp2, tp3 = setup['entry'] + risk*1.5, setup['entry'] + risk*2.0, setup['entry'] + risk*3.0
                    if setup['side'] == "SHORT": tp1, tp2, tp3 = setup['entry'] - risk*1.5, setup['entry'] - risk*2.0, setup['entry'] - risk*3.0
                    
                    msg = f"🎯 *Alpha Oracle | SMC 掛單提醒*\n──────────────────\n"
                    msg += f"💎 幣種：#{instId.split('-')[0]} | {'🟢 多' if setup['side']=='LONG' else '🔴 空'}\n"
                    msg += f"📍 進場位：`{setup['entry']:.4f}`\n"
                    msg += f"🛡️ 止損位：`{setup['sl']:.4f}` (**-1.0R**)\n\n"
                    msg += f"📊 **市場數據：**\n├ 💰 資費：`{m['funding']}`\n├ 👥 多空比：`{m['ls_ratio']}`\n└ 📈 持倉量：`{m['oi']}`\n\n"
                    msg += f"💰 **目標：** TP1(+1.5R) | TP2(+2.0R) | TP3(+3.0R)"
                    send_tg(msg)
                    updated_trades.append({"instId":instId,"side":setup['side'],"status":"WAITING","entry":setup['entry'],"sl":setup['sl'],"tp1":tp1,"tp2":tp2,"tp3":tp3,"locked":0})
                continue

            t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
            if t['status'] == "WAITING":
                if (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry']):
                    t['status'] = "ACTIVE"
                    msg = f"🚀 *掛單成交提醒 | #{instId.split('-')[0]}*\n──────────────────\n"
                    msg += f"✅ **成交價格：`{curr_p:.4f}`**\n📈 方向：{'多單進場' if t['side']=='LONG' else '空單進場'}\n"
                    msg += f"📊 當時多空比：`{m['ls_ratio']}`\n💰 當時資費：`{m['funding']}`\n──────────────────"
                    send_tg(msg)
                updated_trades.append(t)
            elif t['status'] == "ACTIVE":
                if t['locked'] == 0 and ((t['side']=="LONG" and curr_p >= t['tp2']) or (t['side']=="SHORT" and curr_p <= t['tp2'])):
                    t['locked'], t['sl'] = 1, t['tp1']
                    send_tg(f"🔒 *鎖利啟動* | #{instId.split('-')[0]} 止損移至 1.5R (`{t['tp1']:.4f}`)")
                
                is_sl = (t['side']=="LONG" and curr_p <= t['sl']) or (t['side']=="SHORT" and curr_p >= t['sl'])
                is_tp3 = (t['side']=="LONG" and curr_p >= t['tp3']) or (t['side']=="SHORT" and curr_p <= t['tp3'])
                if is_sl or is_tp3:
                    res = "TP" if is_tp3 else "SL"
                    send_tg(f"🏁 *結算 | #{instId.split('-')[0]}*\n結果：{'💰 止盈' if is_tp3 else '🛡️ 離場'}\n出場價：`{curr_p:.4f}`")
                    pd.DataFrame([{"instId":instId,"result":res}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                    continue
                updated_trades.append(t)
        pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)
    except: traceback.print_exc()

if __name__ == "__main__": main()
