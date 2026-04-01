import requests
import os
import pandas as pd
import numpy as np
import logging
import traceback
import time
from datetime import datetime, timedelta

# --- 1. 基礎配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 請在下方填入你的 Telegram 資訊 ---
TG_TOKEN = "你的_BOT_TOKEN"
CHAT_ID = "你的_CHAT_ID"

# 監控清單：5 隻主流 + 5 隻潛力山寨
ALL_MONITOR = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "ADA-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP"
]

LOG_FILE = "active_trades.csv"
STATS_FILE = "daily_stats.csv"

# --- 2. 數據工具函數 ---

def fetch_okx(instId, bar='15m', limit='100'):
    """獲取 OKX K 線數據"""
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if 'data' not in res: return None
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        # 只取已確認的 K 線並轉為時間正序
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except: return None

def get_advanced_metrics(instId):
    """獲取大戶數據 (CVD/LS Ratio)"""
    try:
        base_id = instId.replace("-SWAP", "")
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5).json()
        ls_ratio = ls_res['data'][0]['ratio']
        cvd_status = "🟢 大戶吸籌" if float(ls_ratio) < 0.95 else "🔴 散戶較多"
        return {"ls_ratio": ls_ratio, "cvd": cvd_status}
    except: return {"ls_ratio": "N/A", "cvd": "N/A"}

def calculate_atr(df):
    """計算 ATR 用於動態止損"""
    hl, hc, lc = df['h']-df['l'], np.abs(df['h']-df['c'].shift()), np.abs(df['l']-df['c'].shift())
    tr = np.max(pd.concat([hl, hc, lc], axis=1), axis=1)
    return tr.rolling(window=14).mean().iloc[-1]

# --- 3. SMC 策略核心邏輯 ---

def check_mtf_trend(instId):
    """1H 趨勢濾網：價格需在 EMA200 之上(多)或之下(空)"""
    df_1h = fetch_okx(instId, bar='1H', limit='200')
    if df_1h is None or len(df_1h) < 200: return "NEUTRAL"
    ema200 = df_1h['c'].ewm(span=200, adjust=False).mean().iloc[-1]
    return "BULL" if df_1h['c'].iloc[-1] > ema200 else "BEAR"

def find_smc_setup(df, instId):
    """尋找結構破壞 (Choch) 與回踩進場位"""
    if df is None or len(df) < 60: return None
    mtf = check_mtf_trend(instId)
    atr = calculate_atr(df)
    vol_sma = df['v'].rolling(5).mean().iloc[-1]
    
    for i in range(len(df)-3, len(df)-20, -1):
        k0, k1, k2 = df.iloc[i-1], df.iloc[i], df.iloc[i+1]
        
        # 多頭：順 1H 勢 + 突破前 15 根高點 + 帶量
        if k2['c'] > df['h'].iloc[i-15:i].max() and mtf == "BULL" and k2['v'] > vol_sma:
            sweep = k1['l'] < df['l'].iloc[i-10:i].min() # 掃損標記
            low, high = df['l'].iloc[i-15:i+1].min(), k2['c']
            entry = min((k2['l'] + k0['h'])/2 if k2['l'] > k0['h'] else k1['o'], (high + low)/2)
            sl, tp = k1['l'] - (0.4 * atr), df['h'].iloc[-60:].max()
            r = abs(tp-entry)/abs(entry-sl) if abs(entry-sl)!=0 else 0
            if r >= 1.5:
                return {"side":"LONG","entry":entry,"sl":sl,"tp":tp,"r_ratio":round(r,2),"sweep":sweep}

        # 空頭：順 1H 勢 + 跌破前 15 根低點 + 帶量
        if k2['c'] < df['l'].iloc[i-15:i].min() and mtf == "BEAR" and k2['v'] > vol_sma:
            sweep = k1['h'] > df['h'].iloc[i-10:i].max()
            high, low = df['h'].iloc[i-15:i+1].max(), k2['c']
            entry = max((k2['h'] + k0['l'])/2 if k2['h'] < k0['l'] else k1['o'], (high + low)/2)
            sl, tp = k1['h'] + (0.4 * atr), df['l'].iloc[-60:].min()
            r = abs(entry-tp)/abs(sl-entry) if abs(sl-entry)!=0 else 0
            if r >= 1.5:
                return {"side":"SHORT","entry":entry,"sl":sl,"tp":tp,"r_ratio":round(r,2),"sweep":sweep}
    return None

# --- 4. 執行與通知 ---

def send_tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                      json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"Markdown"}, timeout=10)
    except: pass

def run_oracle():
    now = datetime.now()
    # 初始化檔案
    log_cols = ["instId","side","status","entry","sl","tp","r_ratio","locked"]
    for f, c in [(LOG_FILE, log_cols), (STATS_FILE, ["instId","result"])]:
        if not os.path.exists(f) or os.stat(f).st_size == 0:
            pd.DataFrame(columns=c).to_csv(f, index=False)

    # 早上 08:00 早報
    if now.hour == 8 and 0 <= now.minute < 5:
        m = "☕ *Alpha Oracle 晨間巡檢*\n──────────────────\n"
        for inst in ALL_MONITOR[:5]:
            met = get_advanced_metrics(inst)
            m += f"• #{inst.split('-')[0]}: {met['cvd']}\n"
        send_tg(m + "\n💡 *SMC 提醒：1H 趨勢同向才掛單。*")

    # 讀取現有持倉
    try: trades_df = pd.read_csv(LOG_FILE)
    except: trades_df = pd.DataFrame(columns=log_cols)
    active_ids, updated = trades_df['instId'].tolist(), []

    for instId in ALL_MONITOR:
        df = fetch_okx(instId)
        if df is None: continue
        curr_p = df['c'].iloc[-1]

        # A. 掃描新訊號
        if instId not in active_ids:
            s = find_smc_setup(df, instId)
            if s:
                met = get_advanced_metrics(instId)
                tag = "⚡ (獵取流動性)" if s['sweep'] else ""
                msg = f"🔍 *Alpha Oracle | SMC 訊號*\n──────────────────\n#{instId.split('-')[0]} {tag}\n數據: {met['cvd']}\n\n📍 進場: {s['entry']:.4f}\n🚫 止損: {s['sl']:.4f}\n💰 止盈: {s['tp']:.4f}\n📈 盈虧比: *{s['r_ratio']}R*"
                send_tg(msg)
                s.pop('sweep'); s.update({"instId":instId,"status":"WAITING","locked":0})
                updated.append(s)
            continue

        # B. 追蹤持倉與掛單
        t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
        if t['status'] == "WAITING":
            # 結構失效撤單
            if (t['side']=="LONG" and curr_p <= t['sl']) or (t['side']=="SHORT" and curr_p >= t['sl']):
                send_tg(f"⚠️ *撤單通知*: #{instId.split('-')[0]} 結構已廢。")
                continue
            # 成交
            if (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry']):
                t['status'] = "ACTIVE"; send_tg(f"🚀 *成交提醒*: #{instId.split('-')[0]} 已進入進場位。")
            updated.append(t)
            
        elif t['status'] == "ACTIVE":
            # 鎖利保本 (50% 空間)
            mid = (t['entry'] + t['tp']) / 2
            if t['locked'] == 0 and ((t['side']=="LONG" and curr_p >= mid) or (t['side']=="SHORT" and curr_p <= mid)):
                t['locked'], t['sl'] = 1, t['entry']
                send_tg(f"🔒 *鎖利保本*: #{instId.split('-')[0]} 止損已移至開倉位。")
            
            # 結算
            is_sl = (curr_p <= t['sl'] if t['side']=="LONG" else curr_p >= t['sl'])
            is_tp = (curr_p >= t['tp'] if t['side']=="LONG" else curr_p <= t['tp'])
            if is_sl or is_tp:
                res = "TP" if (is_tp or t['locked']==1) else "SL"
                send_tg(f"🏁 *結算通知*: #{instId.split('-')[0]} 結果: {res}")
                pd.DataFrame([{"instId":instId,"result":res}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                continue
            updated.append(t)

    # 儲存更新後的 CSV
    pd.DataFrame(updated).to_csv(LOG_FILE, index=False)

if __name__ == "__main__":
    print("-" * 30)
    print(f"🚀 Alpha Oracle [Windows] 啟動: {datetime.now().strftime('%H:%M:%S')}")
    print("-" * 30)
    send_tg("🤖 *Alpha Oracle Windows 系統上線*\n自動巡檢中 (每 5 分鐘一次)...")
    
    while True:
        try:
            run_oracle()
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ 掃描完成")
        except:
            logging.error(f"❌ 異常:\n{traceback.format_exc()}")
        
        time.sleep(300) # 每 5 分鐘執行一次
