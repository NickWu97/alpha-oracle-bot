import requests
import os
import pandas as pd
import numpy as np
import logging
import traceback
import time
from datetime import datetime

# --- 0. 環境配置 ---
os.environ['TZ'] = 'Asia/Taipei'
try:
    time.tzset()
except:
    pass

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
LOG_FILE = "active_trades.csv"

# 監控清單
ALL_MONITOR = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "XRP-USDT-SWAP", "LINK-USDT-SWAP",
    "APT-USDT-SWAP", "OP-USDT-SWAP", "ARB-USDT-SWAP", "NEAR-USDT-SWAP",
    "PEPE-USDT-SWAP", "WIF-USDT-SWAP", "ORDI-USDT-SWAP", "TON-USDT-SWAP"
]

# --- 1. 數據獲取模組 ---

def fetch_okx(instId, bar='15m', limit='100'):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if 'data' not in res or not res['data']: return None
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        # 過濾未確認 K 線並轉為正序
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except: return None

def get_data_metrics(instId):
    base_id = instId.replace("-SWAP", "")
    data = {"ls_ratio": 1.0, "funding": 0.0}
    try:
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5).json()
        if 'data' in ls_res and ls_res['data']: data['ls_ratio'] = float(ls_res['data'][0]['ratio'])
        fr_res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5).json()
        if 'data' in fr_res and fr_res['data']: data['funding'] = float(fr_res['data'][0]['fundingRate'])
        return data
    except: return data

# --- 2. SMC 核心偵測 (支援長短線邏輯) ---

def find_smc_setup(df, metrics, mode="短線"):
    if df is None or len(df) < 50: return None
    
    # 結構參數差異：長線需要更嚴謹的突破確認
    lookback = 20 if mode == "短線" else 50
    recent_high = df['h'].iloc[-lookback:-3].max()
    recent_low = df['l'].iloc[-lookback:-3].min()
    
    curr_k, prev_k, old_k = df.iloc[-1], df.iloc[-2], df.iloc[-3]
    
    # --- 多頭：BOS突破 + 數據反向(散戶空) ---
    if curr_k['c'] > recent_high and metrics['ls_ratio'] < 1.1:
        if prev_k['l'] > old_k['h']: # 尋找 Bullish FVG
            entry = (prev_k['l'] + old_k['h']) / 2
            sl = old_k['l']
            risk = abs(entry - sl)
            # 長線設定更高盈虧比預期
            r_mult = 1.5 if mode == "短線" else 3.0
            return {
                "mode": mode, "side": "LONG", "entry": entry, "sl": sl,
                "tp1": entry + (risk * r_mult), 
                "tp2": entry + (risk * r_mult * 1.5), 
                "tp3": entry + (risk * r_mult * 2.5)
            }

    # --- 空頭：BOS跌破 + 數據反向(散戶多) ---
    if curr_k['c'] < recent_low and metrics['ls_ratio'] > 1.1:
        if prev_k['h'] < old_k['l']: # 尋找 Bearish FVG
            entry = (prev_k['h'] + old_k['l']) / 2
            sl = old_k['h']
            risk = abs(sl - entry)
            r_mult = 1.5 if mode == "短線" else 3.0
            return {
                "mode": mode, "side": "SHORT", "entry": entry, "sl": sl,
                "tp1": entry - (risk * r_mult), 
                "tp2": entry - (risk * r_mult * 1.5), 
                "tp3": entry - (risk * r_mult * 2.5)
            }
    return None

# --- 3. 通知與監控執行 ---

def send_tg(msg):
    if not TG_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                      json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except: pass

def run_oracle():
    # 檔案格式化
    cols = ["instId","mode","side","status","entry","sl","tp1","tp2","tp3"]
    if not os.path.exists(LOG_FILE):
        pd.DataFrame(columns=cols).to_csv(LOG_FILE, index=False)
    
    try:
        trades_df = pd.read_csv(LOG_FILE)
        # 🛡️ 修復舊版 CSV 缺失欄位
        for c in cols:
            if c not in trades_df.columns: trades_df[c] = None
    except:
        trades_df = pd.DataFrame(columns=cols)
    
    active_ids = trades_df['instId'].tolist()
    updated_trades = []

    print(f"[{datetime.now()}] 長短線雙重監控啟動...")

    for instId in ALL_MONITOR:
        metrics = get_data_metrics(instId)
        
        # 定義長短線掃描配置
        scan_modes = [("短線", "15m"), ("長線", "4H")]
        
        for mode_name, timeframe in scan_modes:
            df = fetch_okx(instId, bar=timeframe)
            if df is None: continue
            curr_p = df['c'].iloc[-1]

            # A. 發現新機會
            if instId not in active_ids:
                setup = find_smc_setup(df, metrics, mode=mode_name)
                if setup:
                    lev = "🚀 推薦槓桿：10-20x" if mode_name == "短線" else "🐢 推薦槓桿：3-5x"
                    msg = f"🔥 *Alpha Oracle | {mode_name}訊號*\n"
                    msg += f"──────────────────\n"
                    msg += f"🪙 幣種：#{instId.split('-')[0]}\n"
                    msg += f"📈 方向：{'🟢多單' if setup['side']=='LONG' else '🔴空單'}\n"
                    msg += f"📊 數據：LS {metrics['ls_ratio']} | FR {metrics['funding']:.4%}\n"
                    msg += f"──────────────────\n"
                    msg += f"📍 預定進場：{setup['entry']:.4f}\n"
                    msg += f"💰 最終目標：{setup['tp3']:.4f}\n"
                    msg += f"🚫 結構止損：{setup['sl']:.4f}\n"
                    msg += f"──────────────────\n"
                    msg += f"{lev}"
                    send_tg(msg)
                    setup.update({"instId": instId, "status": "WAITING"})
                    updated_trades.append(setup)
                    # 為了避免重複，同個幣種偵測到一個模式後就跳下一個幣種
                    break
            else:
                # B. 持倉追蹤
                t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
                
                if t['status'] == "WAITING":
                    # 檢查回踩成交
                    is_hit = (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry'])
                    if is_hit:
                        t['status'] = "ACTIVE"
                        send_tg(f"🔔 *成功進場 ({t['mode']})*\n──────────────────\n✅ #{instId.split('-')[0]} 已回踩成交！\n📍 進場價格：{curr_p:.4f}")
                    updated_trades.append(t)
                elif t['status'] == "ACTIVE":
                    # 檢查 TP/SL
                    is_sl = (curr_p <= t['sl'] if t['side']=="LONG" else curr_p >= t['sl'])
                    is_tp3 = (curr_p >= t['tp3'] if t['side']=="LONG" else curr_p <= t['tp3'])
                    
                    if is_sl or is_tp3:
                        res = "💰 獲利 (TP3)" if is_tp3 else "❌ 止損 (SL)"
                        send_tg(f"🏁 *結算 ({t['mode']})*\n──────────────────\n#{instId.split('-')[0]} 交易結束\n🏆 結果：{res}\n📍 出場價格：{curr_p:.4f}")
                    else:
                        updated_trades.append(t)
                break # 處理完持倉後跳下一個幣種

    pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)

if __name__ == "__main__":
    try:
        run_oracle()
    except Exception as e:
        print(f"程式崩潰: {e}")
        traceback.print_exc()
if __name__ == "__main__":
    send_tg("機器人連線測試成功！") # 先加這行測試
    try:
        run_oracle()
    except Exception as e:
        # ...
