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

# 監控幣種 (主流 + 強勢山寨)
ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP", 
    "BCH-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP"
]

LOG_FILE = "active_trades.csv"

# --- 2. 數據工具 ---
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
    """計算市場波動率用於止損緩衝"""
    high_low = df['h'] - df['l']
    high_close = np.abs(df['h'] - df['c'].shift())
    low_close = np.abs(df['l'] - df['c'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(window=window).mean().iloc[-1]

# --- 3. SMC 區域偵測核心 (OB/FVG) ---
def find_smc_setup(df):
    if df is None or len(df) < 40: return None
    atr = calculate_atr(df)
    
    # 掃描最近的結構轉折 (Choch)
    for i in range(len(df)-3, len(df)-20, -1):
        k0, k1, k2 = df.iloc[i-1], df.iloc[i], df.iloc[i+1] # 前一根, 轉折根, 確認根
        
        # Bullish Choch (多頭轉折): 當前根突破前15根最高
        if k2['c'] > k2['o'] and k2['c'] > df['h'].iloc[i-15:i].max():
            # 優先找 FVG (Gap) 進場
            if k2['l'] > k0['h']:
                entry = (k2['l'] + k0['h']) / 2
            else: # 否則找 OB (Order Block) 核心進場
                entry = (k1['l'] + k1['o']) / 2
            
            sl = k1['l'] - (0.4 * atr) # ATR 緩衝止損
            return {"side": "LONG", "entry": entry, "sl": sl}

        # Bearish Choch (空頭轉折): 當前根跌破前15根最低
        if k2['c'] < k2['o'] and k2['c'] < df['l'].iloc[i-15:i].min():
            if k2['h'] < k0['l']:
                entry = (k2['h'] + k0['l']) / 2
            else:
                entry = (k1['h'] + k1['o']) / 2
            
            sl = k1['h'] + (0.4 * atr)
            return {"side": "SHORT", "entry": entry, "sl": sl}
            
    return None

# --- 4. 主程式邏輯 ---
def main():
    try:
        print("🚀 Alpha Oracle Bot 終極版啟動...")
        
        # 檔案初始化安全檢查
        if not os.path.exists(LOG_FILE) or os.stat(LOG_FILE).st_size == 0:
            pd.DataFrame(columns=["instId","side","status","entry","sl","tp1","tp2","tp3","locked"]).to_csv(LOG_FILE, index=False)
        
        trades_df = pd.read_csv(LOG_FILE)
        active_ids = trades_df['instId'].tolist()
        updated_trades = []

        for instId in ALL_COINS:
            df = fetch_okx(instId)
            if df is None or df.empty: continue
            curr_p = df['c'].iloc[-1]

            # A. 搜尋新訊號 (掛單模式)
            if instId not in active_ids:
                setup = find_smc_setup(df)
                if setup:
                    risk = abs(setup['entry'] - setup['sl'])
                    # TPSL 優化邏輯：1.5R, 2R, 3R
                    tp1 = setup['entry'] + risk*1.5 if setup['side']=="LONG" else setup['entry'] - risk*1.5
                    tp2 = setup['entry'] + risk*2.0 if setup['side']=="LONG" else setup['entry'] - risk*2.0
                    tp3 = setup['entry'] + risk*3.0 if setup['side']=="LONG" else setup['entry'] - risk*3.0
                    
                    msg = f"🎯 *Alpha Oracle | 區域掛單提醒*\n──────────────────\n"
                    msg += f"💎 幣種：#{instId.split('-')[0]}\n🎯 動作：{'🟢 多單回踩' if setup['side']=='LONG' else '🔴 空單回踩'}\n\n"
                    msg += f"📍 **掛單價格：`{setup['entry']:.4f}`**\n"
                    msg += f"🚫 **緩衝止損：`{setup['sl']:.4f}`**\n"
                    msg += f"💰 **最終目標：`{tp3:.4f}`**\n"
                    msg += f"──────────────────\n⚖️ 風盈比：1 : 3 | 區域：OB/FVG 中心"
                    send_tg(msg)
                    
                    updated_trades.append({
                        "instId":instId, "side":setup['side'], "status":"WAITING",
                        "entry":setup['entry'], "sl":setup['sl'], "tp1":tp1, "tp2":tp2, "tp3":tp3, "locked":0
                    })
                continue

            # B. 持倉監控邏輯
            t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
            
            # 等待價格回踩掛單位
            if t['status'] == "WAITING":
                is_hit = (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry'])
                if is_hit:
                    t['status'] = "ACTIVE"
                    send_tg(f"🚀 *掛單成交* | #{instId.split('-')[0]}\n✅ 價格已觸及 OB/FVG 區域，正式進場！")
                updated_trades.append(t)
            
            # 進行中：鎖利與結算
            elif t['status'] == "ACTIVE":
                # 2R 鎖利邏輯：將止損移至 1.5R
                if t['locked'] == 0:
                    is_tp2 = (t['side']=="LONG" and curr_p >= t['tp2']) or (t['side']=="SHORT" and curr_p <= t['tp2'])
                    if is_tp2:
                        t['locked'] = 1
                        t['sl'] = t['tp1']
                        send_tg(f"🔒 *鎖利啟動* | #{instId.split('-')[0]}\n價格達 2R，止損已強制移至 TP1 (`{t['tp1']:.4f}`) 保護利潤！")
                
                # 離場判定
                is_sl = (t['side']=="LONG" and curr_p <= t['sl']) or (t['side']=="SHORT" and curr_p >= t['sl'])
                is_tp3 = (t['side']=="LONG" and curr_p >= t['tp3']) or (t['side']=="SHORT" and curr_p <= t['tp3'])
                
                if is_sl or is_tp3:
                    res = "💰 TP3 完美止盈" if is_tp3 else ("🛡️ 鎖利離場" if t['locked'] == 1 else "❌ 止損離場")
                    send_tg(f"🏁 *訂單結算* | #{instId.split('-')[0]}\n結果：{res}\n結算價：`{curr_p}`")
                    continue
                updated_trades.append(t)

        # 寫回 CSV
        pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)
        print("✅ 任務執行成功。")
        
    except Exception as e:
        print(f"❌ 嚴重錯誤: {e}")
        traceback.print_exc()
        exit(1)

if __name__ == "__main__":
    main()
