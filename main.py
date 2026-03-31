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
    """抓取資費、多空比與合約持倉數據"""
    try:
        base_id = instId.replace("-SWAP", "")
        # 1. 抓取資費
        fund_url = f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}"
        fund_data = requests.get(fund_url, timeout=5).json()
        funding_rate = float(fund_data['data'][0]['fundingRate']) * 100 # 轉百分比
        
        # 2. 抓取多空人數比 (Long/Short Ratio)
        ls_url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}"
        ls_data = requests.get(ls_url, timeout=5).json()
        ls_ratio = ls_data['data'][0]['ratio']
        
        # 3. 抓取持倉量 (Open Interest) 變化作為 CVD 參考
        oi_url = f"https://www.okx.com/api/v5/public/open-interest?instId={instId}"
        oi_data = requests.get(oi_url, timeout=5).json()
        oi = oi_data['data'][0]['oi']
        
        return {
            "funding": f"{funding_rate:.4f}%",
            "ls_ratio": ls_ratio,
            "oi": oi
        }
    except:
        return {"funding": "N/A", "ls_ratio": "N/A", "oi": "N/A"}

def send_tg(msg):
    if not TG_TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
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
        if k2['c'] > k2['o'] and k2['c'] > df['h'].iloc[i-15:i].max():
            entry = (k2['l'] + k0['h']) / 2 if k2['l'] > k0['h'] else (k1['l'] + k1['o']) / 2
            sl = k1['l'] - (0.4 * atr)
            return {"side": "LONG", "entry": entry, "sl": sl}
        if k2['c'] < k2['o'] and k2['c'] < df['l'].iloc[i-15:i].min():
            entry = (k2['h'] + k0['l']) / 2 if k2['h'] < k0['l'] else (k1['h'] + k1['o']) / 2
            sl = k1['h'] + (0.4 * atr)
            return {"side": "SHORT", "entry": entry, "sl": sl}
    return None

# --- 3. 主程式 ---
def main():
    try:
        now_tw = datetime.utcnow() + timedelta(hours=8)
        manual_report = os.getenv("MANUAL_REPORT", "false").lower() == "true"
        
        # 檔案初始化
        log_cols = ["instId","side","status","entry","sl","tp1","tp2","tp3","locked"]
        if not os.path.exists(LOG_FILE) or os.stat(LOG_FILE).st_size == 0:
            pd.DataFrame(columns=log_cols).to_csv(LOG_FILE, index=False)
        if not os.path.exists(STATS_FILE) or os.stat(STATS_FILE).st_size == 0:
            pd.DataFrame(columns=["instId","result"]).to_csv(STATS_FILE, index=False)

        # A. 戰績回報 (略過，保持原有邏輯)

        # B. 核心交易監控
        trades_df = pd.read_csv(LOG_FILE)
        active_ids = trades_df['instId'].tolist()
        updated_trades = []

        for instId in ALL_COINS:
            df = fetch_okx(instId)
            if df is None or df.empty: continue
            curr_p = df['c'].iloc[-1]
            metrics = get_extra_metrics(instId) # 抓取額外數據

            # 1. 發現新訊號
            if instId not in active_ids:
                setup = find_smc_setup(df)
                if setup:
                    risk = abs(setup['entry'] - setup['sl'])
                    tp1, tp2, tp3 = setup['entry'] + risk*1.5, setup['entry'] + risk*2.0, setup['entry'] + risk*3.0
                    if setup['side'] == "SHORT":
                        tp1, tp2, tp3 = setup['entry'] - risk*1.5, setup['entry'] - risk*2.0, setup['entry'] - risk*3.0
                    
                    msg = f"🎯 *Alpha Oracle | SMC 掛單提醒*\n──────────────────\n"
                    msg += f"💎 幣種：#{instId.split('-')[0]} | {'🟢 多' if setup['side']=='LONG' else '🔴 空'}\n"
                    msg += f"📍 進場位：`{setup['entry']:.4f}`\n"
                    msg += f"🛡️ 止損位：`{setup['sl']:.4f}` (**-1.0R**)\n\n"
                    msg += f"📊 **市場數據：**\n"
                    msg += f"├ 💰 資費：`{metrics['funding']}`\n"
                    msg += f"├ 👥 多空比：`{metrics['ls_ratio']}`\n"
                    msg += f"└ 📈 持倉量：`{metrics['oi']}`\n\n"
                    msg += f"💰 **目標：** TP1(+1.5R) | TP2(+2.0R) | TP3(+3.0R)"
                    send_tg(msg)
                    updated_trades.append({"instId":instId,"side":setup['side'],"status":"WAITING","entry":setup['entry'],"sl":setup['sl'],"tp1":tp1,"tp2":tp2,"tp3":tp3,"locked":0})
                continue

            # 2. 追蹤掛單與成交
            t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
            if t['status'] == "WAITING":
                is_hit = (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry'])
                if is_hit:
                    t['status'] = "ACTIVE"
                    # 成交訊息加入成交價格與即時數據
                    msg = f"🚀 *掛單成交提醒 | #{instId.split('-')[0]}*\n──────────────────\n"
                    msg += f"✅ **成交價格：`{curr_p:.4f}`**\n"
                    msg += f"📈 方向：{'多單進場' if t['side']=='LONG' else '空單進場'}\n"
                    msg += f"📊 當時多空比：`{metrics['ls_ratio']}`\n"
                    msg += f"💰 當時資費：`{metrics['funding']}`\n"
                    msg += f"──────────────────\n⚖️ 正朝向 TP1 目標前進..."
                    send_tg(msg)
                updated_trades.append(t)
            
            elif t['status'] == "ACTIVE":
                # (鎖利與結算邏輯保持不變...)
                # ...
                updated_trades.append(t)

        pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)
    except:
        traceback.print_exc()

if __name__ == "__main__":
    main()
