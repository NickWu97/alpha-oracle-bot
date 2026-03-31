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

# 監控標的：5 主流 + 7 強勢山寨
ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP", 
    "BCH-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP"
]

LOG_FILE = "active_trades.csv"

def send_tg(msg):
    if not TG_TOKEN or not CHAT_ID:
        print("⚠️ 錯誤：未設定 GitHub Secrets (TG_TOKEN 或 CHAT_ID)")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except Exception as e:
        print(f"TG 發送失敗: {e}")

def fetch_okx(instId, bar="15m", limit="100"):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if 'data' not in res or not res['data']: return None
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        # 只取已收盤資料並轉為時間正序
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except: return None

def get_market_sentiment(instId):
    """獲取數據濾網：CVD, LS Ratio, Funding Rate"""
    try:
        base = instId.replace("-SWAP", "")
        # 多空人數比
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base}", timeout=5).json()
        ls = float(ls_res['data'][0]['ratio']) if 'data' in ls_res else 1.0
        # CVD (Taker成交量)
        cvd_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/taker-volume?instId={base}", timeout=5).json()
        cvd_status = "BUY" if float(cvd_res['data'][0]['buyVol']) > float(cvd_res['data'][0]['sellVol']) else "SELL"
        # 資費
        fund_res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5).json()
        fund = float(fund_res['data'][0]['fundingRate']) if 'data' in fund_res else 0.0
        return {"ls": ls, "cvd": cvd_status, "fund": fund}
    except: return {"ls": 1.0, "cvd": "N/A", "fund": 0.0}

def calculate_atr(df, window=14):
    """計算 ATR 用於動態止損緩衝"""
    high_low = df['h'] - df['l']
    high_close = np.abs(df['h'] - df['c'].shift())
    low_close = np.abs(df['l'] - df['c'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(window=window).mean().iloc[-1]

def find_smc_setup(df):
    """尋找 Choch 結構並計算 ATR 止損"""
    if df is None or len(df) < 35: return None
    atr = calculate_atr(df)
    # 掃描最近的 K 線找尋 Choch
    for i in range(len(df)-3, len(df)-15, -1):
        k1, k2, k3 = df.iloc[i], df.iloc[i+1], df.iloc[i+2]
        # 多單：突破前 15 根高點
        if k3['c'] > k3['o'] and k3['l'] > k1['h'] and k3['h'] > df['h'].iloc[i-15:i].max():
            return {"side": "LONG", "entry": k1['h'], "sl": k1['l'] - (0.5 * atr)}
        # 空單：跌破前 15 根低點
        if k3['c'] < k3['o'] and k3['h'] < k1['l'] and k3['l'] < df['l'].iloc[i-15:i].min():
            return {"side": "SHORT", "entry": k1['l'], "sl": k1['h'] + (0.5 * atr)}
    return None

def main():
    try:
        print("🚀 Alpha Oracle Bot 啟動中...")
        if not os.path.exists(LOG_FILE):
            pd.DataFrame(columns=["instId","side","status","entry","sl","tp1","tp2","tp3","locked"]).to_csv(LOG_FILE, index=False)
        
        trades_df = pd.read_csv(LOG_FILE)
        active_ids = trades_df['instId'].tolist()
        updated_trades = []

        for instId in ALL_COINS:
            df = fetch_okx(instId)
            if df is None or df.empty: continue
            curr_p = df['c'].iloc[-1]

            # A. 掃描新信號
            if instId not in active_ids:
                setup = find_smc_setup(df)
                if setup:
                    m = get_market_sentiment(instId)
                    # 對手盤濾網邏輯
                    is_l = setup['side']=="LONG" and m['ls'] < 1.1 and m['fund'] < 0.0001 and m['cvd']=="BUY"
                    is_s = setup['side']=="SHORT" and m['ls'] > 1.3 and m['fund'] > 0.0001 and m['cvd']=="SELL"
                    
                    if is_l or is_s:
                        risk = abs(setup['entry'] - setup['sl'])
                        tp1 = setup['entry'] + (risk * 1.5) if is_l else setup['entry'] - (risk * 1.5)
                        tp2 = setup['entry'] + (risk * 2.0) if is_l else setup['entry'] - (risk * 2.0)
                        tp3 = setup['entry'] + (risk * 3.0) if is_l else setup['entry'] - (risk * 3.0)
                        lev = min(20, max(1, int(0.12 / (risk / setup['entry']))))
                        
                        send_tg(f"🔍 *SMC 信號*：#{instId}\n方向：{'🟢 多' if is_l else '🔴 空'}\n進場位：`{setup['entry']:.4f}`\n止損位：`{setup['sl']:.4f}`\n建議槓桿：{lev}x")
                        updated_trades.append({"instId":instId,"side":setup['side'],"status":"WAITING","entry":setup['entry'],"sl":setup['sl'],"tp1":tp1,"tp2":tp2,"tp3":tp3,"locked":0})
                continue

            # B. 持倉追蹤
            t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
            if t['status'] == "WAITING":
                # 觸發進場
                if (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry']):
                    t['status'] = "ACTIVE"
                    send_tg(f"🚀 *【確認進場】* | #{instId}\n當前價：`{curr_p}`")
                updated_trades.append(t)
            elif t['status'] == "ACTIVE":
                # TP2 鎖利邏輯
                if t['locked'] == 0:
                    if (t['side']=="LONG" and curr_p >= t['tp2']) or (t['side']=="SHORT" and curr_p <= t['tp2']):
                        t['locked'] = 1
                        t['sl'] = t['tp1'] # 止損移至 TP1
                        send_tg(f"🔒 *鎖利* | #{instId} 已達 2R，止損移至 1.5R")
                
                # 結算判定 (止盈或止損)
                is_sl = (t['side']=="LONG" and curr_p <= t['sl']) or (t['side']=="SHORT" and curr_p >= t['sl'])
                is_tp = (t['side']=="LONG" and curr_p >= t['tp3']) or (t['side']=="SHORT" and curr_p <= t['tp3'])
                
                if is_sl or is_tp:
                    res = "💰 TP3 完美止盈" if is_tp else ("🔒 鎖利出場" if t['locked']==1 else "❌ 止損")
                    send_tg(f"🏁 *結算* | #{instId}\n結果：{res}\n價格：`{curr_p}`")
                    continue
                updated_trades.append(t)

        pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)
        print("✅ 任務執行完畢")
    except Exception as e:
        print(f"❌ 嚴重錯誤: {e}")
        traceback.print_exc()
        exit(1)

if __name__ == "__main__":
    main()
