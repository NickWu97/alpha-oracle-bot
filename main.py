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
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 監控清單
MAIN_COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP"]
ALT_COINS = ["SUI-USDT-SWAP", "AVAX-USDT-SWAP", "ADA-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP"]
ALL_MONITOR = MAIN_COINS + ALT_COINS

LOG_FILE = "active_trades.csv"
STATS_FILE = "daily_stats.csv"

# --- 2. 數據工具函數 ---

def fetch_okx(instId, bar='15m', limit='100'):
    """獲取 K 線數據"""
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if 'data' not in res: return None
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except Exception as e:
        logging.error(f"Fetch OKX Error for {instId}: {e}")
        return None

def get_advanced_metrics(instId):
    """獲取 LS Ratio 與 CVD 傾向"""
    try:
        base_id = instId.replace("-SWAP", "")
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5).json()
        ls_ratio = ls_res['data'][0]['ratio']
        cvd_status = "🟢 大戶吸籌" if float(ls_ratio) < 0.95 else "🔴 散戶較多"
        return {"ls_ratio": ls_ratio, "cvd": cvd_status}
    except: return {"ls_ratio": "N/A", "cvd": "N/A"}

def calculate_atr(df):
    """計算 14 週期 ATR"""
    hl = df['h'] - df['l']
    hc = np.abs(df['h'] - df['c'].shift())
    lc = np.abs(df['l'] - df['c'].shift())
    tr = np.max(pd.concat([hl, hc, lc], axis=1), axis=1)
    return tr.rolling(window=14).mean().iloc[-1]

# --- 3. SMC 策略核心 ---

def check_mtf_trend(instId):
    """1H 大趨勢濾網 (EMA 200)"""
    df_1h = fetch_okx(instId, bar='1H', limit='200')
    if df_1h is None or len(df_1h) < 200: return "NEUTRAL"
    ema200 = df_1h['c'].ewm(span=200, adjust=False).mean().iloc[-1]
    return "BULL" if df_1h['c'].iloc[-1] > ema200 else "BEAR"

def find_smc_setup(df, instId):
    """SMC 結構偵測邏輯：偵測突破並尋找回踩點"""
    if df is None or len(df) < 60: return None
    
    mtf_trend = check_mtf_trend(instId)
    atr = calculate_atr(df)
    vol_sma = df['v'].rolling(5).mean().iloc[-1]
    
    # 尋找最近 20 根 K 線內的結構破壞
    for i in range(len(df)-3, len(df)-20, -1):
        k0, k1, k2 = df.iloc[i-1], df.iloc[i], df.iloc[i+1]
        
        # 多頭判斷：價格突破前 15 根高點 + 順 1H 趨勢 + 量增
        if k2['c'] > df['h'].iloc[i-15:i].max() and mtf_trend == "BULL" and k2['v'] > vol_sma:
            sweep = k1['l'] < df['l'].iloc[i-10:i].min() # 掃損標記
            swing_low, swing_high = df['l'].iloc[i-15:i+1].min(), k2['c']
            # 取 FVG 中心與 50% 折價區的較佳位
            entry = min((k2['l'] + k0['h']) / 2 if k2['l'] > k0['h'] else k1['o'], (swing_high + swing_low) / 2)
            sl = k1['l'] - (0.4 * atr)
            tp = df['h'].iloc[-60:].max()
            r = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) != 0 else 0
            
            if r >= 1.5 and (swing_high - entry) / entry < 0.025:
                return {"side": "LONG", "entry": entry, "sl": sl, "tp": tp, "r_ratio": round(r, 2), "sweep": sweep}

        # 空頭判斷
        if k2['c'] < df['l'].iloc[i-15:i].min() and mtf_trend == "BEAR" and k2['v'] > vol_sma:
            sweep = k1['h'] > df['h'].iloc[i-10:i].max()
            swing_high, swing_low = df['h'].iloc[i-15:i+1].max(), k2['c']
            entry = max((k2['h'] + k0['l']) / 2 if k2['h'] < k0['l'] else k1['o'], (swing_high + swing_low) / 2)
            sl = k1['h'] + (0.4 * atr)
            tp = df['l'].iloc[-60:].min()
            r = abs(entry - tp) / abs(sl - entry) if abs(sl - entry) != 0 else 0
            
            if r >= 1.5 and (entry - swing_low) / entry < 0.025:
                return {"side": "SHORT", "entry": entry, "sl": sl, "tp": tp, "r_ratio": round(r, 2), "sweep": sweep}
    return None

# --- 4. 主循環邏輯 ---

def send_tg(msg):
    if not TG_TOKEN or not CHAT_ID: return
    try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except: pass

def run_oracle():
    now_tw = datetime.now()
    log_cols = ["instId","side","status","entry","sl","tp","r_ratio","locked"]
    
    # 確保 CSV 檔案存在
    for f, cols in [(LOG_FILE, log_cols), (STATS_FILE, ["instId","result"])]:
        if not os.path.exists(f) or os.stat(f).st_size == 0:
            pd.DataFrame(columns=cols).to_csv(f, index=False)

    # 每天 08:00 發送早報 (掃描頻率 5 分鐘，確保只發一次)
    if now_tw.hour == 8 and 0 <= now_tw.minute < 5:
        m_msg = "☕ *Alpha Oracle 晨間巡檢*\n──────────────────\n"
        for inst in ALL_MONITOR[:6]: # 晨報顯示前 6 隻
            met = get_advanced_metrics(inst)
            m_msg += f"• #{inst.split('-')[0]}: {met['cvd']} | LS {met['ls_ratio']}\n"
        send_tg(m_msg + "\n💡 *SMC 提醒：順勢而為，回調入場。*")

    # 讀取當前持倉/掛單
    try: trades_df = pd.read_csv(LOG_FILE)
    except: trades_df = pd.DataFrame(columns=log_cols)
    
    active_ids = trades_df['instId'].tolist()
    updated_trades = []

    for instId in ALL_MONITOR:
        df = fetch_okx(instId)
        if df is None: continue
        curr_p = df['c'].iloc[-1]

        # A. 發現新訊號 (若該幣種目前無紀錄)
        if instId not in active_ids:
            setup = find_smc_setup(df, instId)
            if setup:
                met = get_advanced_metrics(instId)
                tag = "⚡ (獵取流動性)" if setup['sweep'] else ""
                msg = f"🔍 *Alpha Oracle | SMC 訊號*\n──────────────────\n#{instId.split('-')[0]} {tag}\n數據：{met['cvd']} | LS {met['ls_ratio']}\n\n📍 進場：{setup['entry']:.4f}\n🚫 止損：{setup['sl']:.4f}\n💰 止盈：{setup['tp']:.4f}\n📈 盈虧比：*{setup['r_ratio']}R*"
                send_tg(msg)
                
                setup.pop('sweep') # 移除 TG 標記欄位再存入 CSV
                setup.update({"instId": instId, "status": "WAITING", "locked": 0})
                updated_trades.append(setup)
            continue

        # B. 追蹤現有訊號 (持倉或掛單)
        # 獲取該幣種的 CSV 資料
        t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
        
        if t['status'] == "WAITING":
            # 結構失效判定 (尚未成交就先破了止損)
            if (t['side']=="LONG" and curr_p <= t['sl']) or (t['side']=="SHORT" and curr_p >= t['sl']):
                send_tg(f"⚠️ *撤單通知*：#{instId.split('-')[0]} 結構在成交前失效。")
                continue # 不加入 updated_trades 等於從 CSV 刪除
                
            # 成交判定 (回踩進場位)
            if (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry']):
                t['status'] = "ACTIVE"
                send_tg(f"🚀 *成交提醒*：#{instId.split('-')[0]} 已回踩進場位成交！")
            updated_trades.append(t)
            
        elif t['status'] == "ACTIVE":
            # 50% 空間鎖利/保本
            mid = (t['entry'] + t['tp']) / 2
            if t['locked'] == 0:
                is_halfway = (t['side']=="LONG" and curr_p >= mid) or (t['side']=="SHORT" and curr_p <= mid)
                if is_halfway:
                    t['locked'], t['sl'] = 1, t['entry']
                    send_tg(f"🔒 *鎖利保本*：#{instId.split('-')[0]} 已達 50% 目標，止損移至開倉位。")
            
            # 結算判定
            is_sl = (curr_p <= t['sl'] if t['side']=="LONG" else curr_p >= t['sl'])
            is_tp = (curr_p >= t['tp'] if t['side']=="LONG" else curr_p <= t['tp'])
            
            if is_sl or is_tp:
                res = "TP" if (is_tp or t['locked'] == 1) else "SL"
                emoji = "💰 獲利" if res == "TP" else "❌ 止損"
                send_tg(f"🏁 *交易結算*：#{instId.split('-')[0]} 結果：{emoji}")
                pd.DataFrame([{"instId":instId,"result":res}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                continue # 完成結算，不再加入更新清單
            updated_trades.append(t)

    # 將所有狀態存回 CSV
    pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)

if __name__ == "__main__":
    print(f"🚀 Alpha Oracle 正式上線時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    send_tg("🤖 *Alpha Oracle 啟動完成*\n目前的掃描頻率為：每 5 分鐘一次。")
    
    while True:
        try:
            run_oracle()
            print(f"✅ 循環完成：{datetime.now().strftime('%H:%M:%S')} - 監控中...")
        except Exception as e:
            logging.error(f"⚠️ 運行異常: {e}")
            traceback.print_exc()
        
        time.sleep(300) # 每 5 分鐘掃描一次
