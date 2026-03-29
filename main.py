import requests
import os
import pandas as pd
import numpy as np
import random
import time
from datetime import datetime, timedelta

# 1. 系統環境變數
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 2. 監控清單
COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
         "SUI-USDT-SWAP", "ORDI-USDT-SWAP", "PEPE-USDT-SWAP", "WIF-USDT-SWAP", "ASI-USDT-SWAP"]

LOG_FILE = "active_trades.csv"

def load_trades():
    if os.path.exists(LOG_FILE):
        return pd.read_csv(LOG_FILE).to_dict('records')
    return []

def save_trades(trades):
    pd.DataFrame(trades).to_csv(LOG_FILE, index=False)

def send_tg(msg):
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                 json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})

def fetch_data(instId, bar="15m"):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit=100"
        res = requests.get(url, timeout=10).json()
        df = pd.DataFrame(res['data'], columns=['ts', 'o', 'h', 'l', 'c', 'v', 'volCcy', 'volCcyQuote', 'confirm'])
        df[['o', 'h', 'l', 'c', 'v']] = df[['o', 'h', 'l', 'c', 'v']].astype(float)
        return df.iloc[::-1].reset_index(drop=True)
    except: return None

def check_logic(instId):
    df = fetch_data(instId, "15m")
    if df is None: return None
    curr_p = df['c'].iloc[-1]
    
    # ATR 波動率計算
    df['tr'] = np.maximum(df['h'] - df['l'], np.maximum(abs(df['h'] - df['c'].shift(1)), abs(df['l'] - df['c'].shift(1))))
    atr = df['tr'].rolling(window=14).mean().iloc[-1]

    # 雙重結構確認 (CHoCH + BoS)
    h_max = df['h'].iloc[-20:-1].max()
    l_min = df['l'].iloc[-20:-1].min()
    
    is_bull = df['c'].iloc[-5] > h_max and curr_p > df['h'].iloc[-5:-1].max()
    is_bear = df['c'].iloc[-5] < l_min and curr_p < df['l'].iloc[-5:-1].min()

    if is_bull:
        sl = curr_p - (atr * 1.5)
        return {"side": "LONG", "entry": curr_p, "sl": sl, "tp": curr_p + (curr_p - sl) * 2.0}
    if is_bear:
        sl = curr_p + (atr * 1.5)
        return {"side": "SHORT", "entry": curr_p, "sl": sl, "tp": curr_p - (sl - curr_p) * 2.0}
    return None

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    active_trades = load_trades()
    new_active_trades = []

    # --- 第一部分：檢查現有持倉狀態 (TP/SL) ---
    for trade in active_trades:
        instId = trade['instId']
        df = fetch_data(instId, "15m")
        if df is None:
            new_active_trades.append(trade)
            continue
        
        curr_p = df['c'].iloc[-1]
        high_p = df['h'].iloc[-1]
        low_p = df['l'].iloc[-1]
        
        closed = False
        if trade['side'] == "LONG":
            if high_p >= trade['tp']:
                send_tg(f"✅ *結算通知：止盈 (TP)*\n💰 幣種：#{instId.split('-')[0]}\n📈 方向：做多\n🎯 離場價：`{trade['tp']:.4f}`")
                closed = True
            elif low_p <= trade['sl']:
                send_tg(f"❌ *結算通知：止損 (SL)*\n💰 幣種：#{instId.split('-')[0]}\n📉 方向：做多\n🎯 離場價：`{trade['sl']:.4f}`")
                closed = True
        else:
            if low_p <= trade['tp']:
                send_tg(f"✅ *結算通知：止盈 (TP)*\n💰 幣種：#{instId.split('-')[0]}\n📉 方向：做空\n🎯 離場價：`{trade['tp']:.4f}`")
                closed = True
            elif high_p >= trade['sl']:
                send_tg(f"❌ *結算通知：止損 (SL)*\n💰 幣種：#{instId.split('-')[0]}\n📈 方向：做空\n🎯 離場價：`{trade['sl']:.4f}`")
                closed = True
        
        if not closed:
            new_active_trades.append(trade)

    # --- 第二部分：掃描新訊號 (排除已持倉幣種) ---
    current_holding_coins = [t['instId'] for t in new_active_trades]
    for instId in COINS:
        if instId in current_holding_coins: continue
        
        signal = check_logic(instId)
        if signal:
            msg = (f"🚀 *Alpha 雙重結構進場單*\n──────────────────\n"
                   f"💎 幣種：#{instId.split('-')[0]}\n🎯 動作：{signal['side']}\n\n"
                   f"📍 進場位：`{signal['entry']:.4f}`\n🚫 止損位：`{signal['sl']:.4f}`\n💰 止盈位：`{signal['tp']:.4f}`\n\n"
                   f"⏳ 狀態：已進入追蹤，結算前不再重複報單。")
            send_tg(msg)
            signal['instId'] = instId
            new_active_trades.append(signal)

    save_trades(new_active_trades)

    # --- 第三部分：早上八點報表 ---
    if now_tw.hour == 8 and not os.path.exists("report_done.txt"):
        report = f"📊 *Alpha Oracle 每日趨勢*\n🗓️ {now_tw.strftime('%m/%d')} 08:30\n──────────────────\n"
        for instId in COINS:
            df_12h = fetch_data(instId, "12H")
            if df_12h is not None:
                side = "做多" if df_12h['c'].iloc[-1] > df_12h['c'].rolling(20).mean().iloc[-1] else "做空"
                report += f"🔹 {instId.split('-')[0]}: {side}\n"
        send_tg(report)
        with open("report_done.txt", "w") as f: f.write("1")
    elif now_tw.hour != 8 and os.path.exists("report_done.txt"):
        os.remove("report_done.txt")

if __name__ == "__main__":
    main()
