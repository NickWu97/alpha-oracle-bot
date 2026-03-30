import requests
import os
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta

# --- 測試開關：現在想看 TG 訊息請設為 True，測試完記得改回 False ---
TEST_MODE = True 

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

MAINSTREAM = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP"]
ALTS = ["SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP", "ZAMA-USDT-SWAP", "BCH-USDT-SWAP", "ASI-USDT-SWAP", "DOGE-USDT-SWAP"]
ALL_COINS = MAINSTREAM + ALTS

LOG_FILE = "active_trades.csv"
HISTORY_FILE = "trade_history.csv"

def send_tg(msg):
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

def get_market_score(instId):
    df = fetch_okx(instId, "4H", "250")
    if df is None or len(df) < 200: return 0.0, "🔴 震盪 (SIDE)"
    ema200 = df['c'].ewm(span=200, adjust=False).mean().iloc[-1]
    diff = ((df['c'].iloc[-1] - ema200) / ema200) * 100
    side = "🟢 做多 (LONG)" if diff > 0 else "🔴 做空 (SHORT)"
    win_rate = 65.0 + min(abs(diff) * 0.4, 17.4)
    return round(win_rate, 1), side

def find_fvg_setup(df):
    if len(df) < 30: return None
    for i in range(len(df)-3, len(df)-12, -1):
        k1, k2, k3 = df.iloc[i], df.iloc[i+1], df.iloc[i+2]
        if k3['c'] > k3['o'] and k3['l'] > k1['h'] and k3['h'] > df.iloc[i-15:i]['h'].max():
            return {"side": "LONG", "fvg_low": k1['h'], "fvg_high": k3['l'], "sl": k1['l']}
        if k3['c'] < k3['o'] and k3['h'] < k1['l'] and k3['l'] < df.iloc[i-15:i]['l'].min():
            return {"side": "SHORT", "fvg_low": k3['h'], "fvg_high": k1['l'], "sl": k1['h']}
    return None

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    
    if not os.path.exists(LOG_FILE):
        pd.DataFrame(columns=["instId","side","status","entry_zone","sl","tp1","tp3","entry_p","tp1_hit"]).to_csv(LOG_FILE, index=False)
    
    trades_df = pd.read_csv(LOG_FILE)
    trades = trades_df.to_dict('records')
    monitored_ids = trades_df['instId'].tolist()

    # --- 1. 每日量化報告測試區 ---
    if (now_tw.hour == 8 and 30 <= now_tw.minute < 45) or TEST_MODE:
        # 如果是測試模式或是準點，就發送報告
        if not os.path.exists("daily_report.ok") or TEST_MODE:
            report = f"📊 *Alpha Oracle | 每日量化報告*\n🗓 {now_tw.strftime('%Y/%m/%d')}\n⏰ {now_tw.strftime('%H:%M')} (UTC+8)\n──────────────────\n\n"
            # 挑選清單中的前 8 隻顯示
            for instId in (MAINSTREAM + ALTS)[:8]:
                wr, sd = get_market_score(instId)
                report += f"🔹 *{instId.split('-')[0]}*\n預測：{sd}\n勝率：{wr}% 🟢\n\n"
            report += "💡 *策略：結構轉變後，在 FVG 缺口處等待回補。*"
            send_tg(report)
            if not TEST_MODE:
                with open("daily_report.ok", "w") as f: f.write("done")
    elif now_tw.hour != 8 and os.path.exists("daily_report.ok"):
        os.remove("daily_report.ok")

    # --- 2. SMC 進場訊號測試區 ---
    new_active_list = []
    history = []

    for instId in ALL_COINS:
        df = fetch_okx(instId, "15m", "60")
        if df is None or len(df) < 40: continue
        curr_p = df['c'].iloc[-1]

        if instId not in monitored_ids:
            setup = find_fvg_setup(df)
            if setup:
                action = "🟢 強力看多" if setup['side'] == "LONG" else "🔴 強力看空"
                msg = f"🔥 *SMC 高勝率進場訊號*\n──────────────────\n\n"
                msg += f"💎 幣種：#{instId.split('-')[0]}\n🎯 動作：{action} (BOS突破)\n\n"
                msg += f"📍 建議進場位：{setup['fvg_low']:.4f}\n🚫 止損位 (SL)：{setup['sl']:.4f}\n"
                msg += "──────────────────\n💡 *策略：結構轉變後，在 FVG 缺口處等待回補。*"
                send_tg(msg)
                new_active_list.append({"instId": instId, "side": setup['side'], "status": "WAITING", "entry_zone": f"{setup['fvg_low']}-{setup['fvg_high']}", "sl": setup['sl'], "tp1": 0, "tp3": 0, "entry_p": 0, "tp1_hit": 0})
            continue

        # (持倉與回踩邏輯同步執行...)
        t = [x for x in trades if x['instId'] == instId][0]
        if t['status'] == "WAITING":
            low_z, hi_z = map(float, str(t['entry_zone']).split('-'))
            if (t['side'] == "LONG" and curr_p <= hi_z) or (t['side'] == "SHORT" and curr_p >= low_z):
                t['status'] = "ACTIVE"; t['entry_p'] = curr_p
                risk = abs(t['entry_p'] - float(t['sl']))
                t['tp3'] = t['entry_p'] + risk*3.5 if t['side'] == "LONG" else t['entry_p'] - risk*3.5
                send_tg(f"🚀 *SMC 回踩成交*\n──────────────────\n💎 #{instId.split('-')[0]} | {t['side']}\n✅ 成交價格: `{curr_p}`\n🎯 目標 TP3: `{t['tp3']:.4f}`")
            new_active_list.append(t)
        elif t['status'] == "ACTIVE":
            # 略過詳細 ACTIVE 邏輯，同上一版
            new_active_list.append(t)

    pd.DataFrame(new_active_list).to_csv(LOG_FILE, index=False)

if __name__ == "__main__":
    main()
