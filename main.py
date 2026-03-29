import requests
import os
import pandas as pd
import numpy as np
import time
from datetime import datetime, timedelta

# 1. 系統環境變數
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 2. 監控清單 (專注高流動性幣種)
COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "SUI-USDT-SWAP", "ASI-USDT-SWAP", "XRP-USDT-SWAP"]

LOG_FILE = "active_trades.csv"

def send_tg(msg):
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                 json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})

def fetch_okx_data(instId, bar="15m", limit="100"):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        df = pd.DataFrame(res['data'], columns=['ts', 'o', 'h', 'l', 'c', 'v', 'volCcy', 'volCcyQuote', 'confirm'])
        df[['o', 'h', 'l', 'c', 'v']] = df[['o', 'h', 'l', 'c', 'v']].astype(float)
        return df.iloc[::-1].reset_index(drop=True)
    except: return None

def get_sentiment_metrics(instId):
    try:
        # 1. 資金費率
        f_url = f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}"
        funding = float(requests.get(f_url).json()['data'][0]['fundingRate'])
        
        # 2. 多空持倉人數比 (LS Ratio) - 抓取最近兩次判斷趨勢
        ls_url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={instId}&period=5m"
        ls_data = requests.get(ls_url).json()['data']
        ls_curr = float(ls_data[0][1])
        ls_prev = float(ls_data[2][1]) # 對比 10 分鐘前的數據
        
        # 3. 模擬 CVD (現貨主動買賣盤趨勢)
        base = instId.split('-')[0]
        s_url = f"https://www.okx.com/api/v5/market/candles?instId={base}-USDT&bar=5m&limit=20"
        s_data = requests.get(s_url).json()['data']
        cvd_trend = "UP" if float(s_data[0][4]) > float(s_data[10][4]) else "DOWN"
        
        return funding, ls_curr, ls_prev, cvd_trend
    except: return 0.0, 1.0, 1.0, "NEUTRAL"

def check_sniper_logic(instId):
    # --- 步驟 1: 4H EMA200 趨勢過濾 ---
    df_4h = fetch_okx_data(instId, bar="4H", limit="200")
    if df_4h is None: return None
    ema200 = df_4h['c'].ewm(span=200, adjust=False).mean().iloc[-1]
    curr_p = df_4h['c'].iloc[-1]
    trend = "BULL" if curr_p > ema200 else "BEAR"

    # --- 步驟 2: 籌碼面背離檢查 ---
    funding, ls_curr, ls_prev, cvd_trend = get_sentiment_metrics(instId)
    
    # 做多背離：現貨吸籌(CVD UP) + 散戶下車(LS Ratio DOWN) + 資費不熱(<0.01%)
    bull_div = (trend == "BULL") and (cvd_trend == "UP") and (ls_curr < ls_prev) and (funding < 0.0001)
    # 做空背離：現貨拋售(CVD DOWN) + 散戶接盤(LS Ratio UP) + 資費過高(>0.03%)
    bear_div = (trend == "BEAR") and (cvd_trend == "DOWN") and (ls_curr > ls_prev) and (funding > 0.0003)

    if not (bull_div or bear_div): return None

    # --- 步驟 3: 15m BoS 結構確認與 ATR 計算 ---
    df_15m = fetch_okx_data(instId, bar="15m", limit="50")
    h_max = df_15m['h'].iloc[-20:-2].max()
    l_min = df_15m['l'].iloc[-20:-2].min()
    
    # ATR 計算
    df_15m['tr'] = np.maximum(df_15m['h'] - df_15m['l'], np.maximum(abs(df_15m['h'] - df_15m['c'].shift(1)), abs(df_15m['l'] - df_15m['c'].shift(1))))
    atr = df_15m['tr'].rolling(window=14).mean().iloc[-1]

    if bull_div and curr_p > h_max:
        sl = curr_p - (atr * 1.5)
        return {"side": "LONG", "entry": curr_p, "sl": sl, "tp": curr_p + (curr_p - sl) * 2.0, "f": funding, "ls": ls_curr}
    
    if bear_div and curr_p < l_min:
        sl = curr_p + (atr * 1.5)
        return {"side": "SHORT", "entry": curr_p, "sl": sl, "tp": curr_p - (sl - curr_p) * 2.0, "f": funding, "ls": ls_curr}

    return None

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    
    # 讀取現有持倉紀錄
    if os.path.exists(LOG_FILE):
        active_trades = pd.read_csv(LOG_FILE).to_dict('records')
    else: active_trades = []
    
    new_active_trades = []

    # --- 檢查結算狀態 ---
    for trade in active_trades:
        df = fetch_okx_data(trade['instId'], "15m", "5")
        if df is None:
            new_active_trades.append(trade)
            continue
        
        curr_p = df['c'].iloc[-1]
        closed = False
        if trade['side'] == "LONG":
            if df['h'].max() >= trade['tp']:
                send_tg(f"✅ *結算：止盈 (TP)*\n💰 #{trade['instId'].split('-')[0]} | 離場: `{trade['tp']:.4f}`")
                closed = True
            elif df['l'].min() <= trade['sl']:
                send_tg(f"❌ *結算：止損 (SL)*\n💰 #{trade['instId'].split('-')[0]} | 離場: `{trade['sl']:.4f}`")
                closed = True
        else:
            if df['l'].min() <= trade['tp']:
                send_tg(f"✅ *結算：止盈 (TP)*\n💰 #{trade['instId'].split('-')[0]} | 離場: `{trade['tp']:.4f}`")
                closed = True
            elif df['h'].max() >= trade['sl']:
                send_tg(f"❌ *結算：止損 (SL)*\n💰 #{trade['instId'].split('-')[0]} | 離場: `{trade['sl']:.4f}`")
                closed = True
        
        if not closed: new_active_trades.append(trade)

    # --- 掃描新狙擊機會 ---
    holding_ids = [t['instId'] for t in new_active_trades]
    for instId in COINS:
        if instId in holding_ids: continue
        
        signal = check_sniper_logic(instId)
        if signal:
            msg = (f"🎯 *Alpha 籌碼背離狙擊單*\n"
                   f"──────────────────\n"
                   f"💎 幣種：#{instId.split('-')[0]}\n"
                   f"⚖️ 動作：{signal['side']}\n\n"
                   f"📍 進場位：`{signal['entry']:.4f}`\n"
                   f"🚫 止損位：`{signal['sl']:.4f}`\n"
                   f"💰 止盈位：`{signal['tp']:.4f}`\n\n"
                   f"📊 *籌碼背離數據：*\n"
                   f"📈 現貨 CVD：`🟢 強力吸籌` (與散戶方向相反)\n"
                   f"👥 人數比：`{signal['ls']:.2f}` (散戶離場中)\n"
                   f"🧧 資費：`{signal['f']*100:.4f}%` (冷卻中)\n"
                   f"🛡️ 趨勢：4H EMA200 支撐中")
            send_tg(msg)
            signal['instId'] = instId
            new_active_trades.append(signal)

    pd.DataFrame(new_active_trades).to_csv(LOG_FILE, index=False)

    # --- 早上八點報表 ---
    if now_tw.hour == 8 and not os.path.exists("daily_ok.txt"):
        report = f"📊 *Alpha Oracle 籌碼日報*\n🗓️ {now_tw.strftime('%Y/%m/%d')}\n──────────────────\n"
        for instId in COINS:
            f, ls, _, _ = get_sentiment_metrics(instId)
            report += f"🔹 *{instId.split('-')[0]}*\n 🧧 資費：`{f*100:.3f}%` | 👥 LS比：`{ls:.2f}`\n"
        send_tg(report)
        with open("daily_ok.txt", "w") as f: f.write("1")
    elif now_tw.hour != 8 and os.path.exists("daily_ok.txt"):
        os.remove("daily_ok.txt")

if __name__ == "__main__":
    main()
