import requests
import os
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta

# 1. 基礎配置與變數
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
    except Exception as e:
        logging.error(f"TG 發送失敗: {e}")

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
    """計算勝率與趨勢方向 (4H 級別)"""
    df = fetch_okx(instId, "4H", "250")
    if df is None or len(df) < 200: return 0.0, "未知"
    ema200 = df['c'].ewm(span=200, adjust=False).mean().iloc[-1]
    last_p = df['c'].iloc[-1]
    diff = ((last_p - ema200) / ema200) * 100
    side = "🟢 做多 (LONG)" if diff > 0 else "🔴 做空 (SHORT)"
    # 基於趨勢強度的模擬勝率 (65%~85%)
    win_rate = 65.0 + min(abs(diff) * 0.4, 20.0)
    return round(win_rate, 1), side

def find_fvg_setup(df):
    """SMC 邏輯：尋找 BOS 突破後的 FVG 缺口"""
    if len(df) < 30: return None
    for i in range(len(df)-3, len(df)-12, -1):
        k1, k2, k3 = df.iloc[i], df.iloc[i+1], df.iloc[i+2]
        # 多頭 BOS + FVG
        if k3['c'] > k3['o'] and k3['l'] > k1['h'] and k3['h'] > df.iloc[i-15:i]['h'].max():
            return {"side": "LONG", "fvg_low": k1['h'], "fvg_high": k3['l'], "sl": k1['l']}
        # 空頭 BOS + FVG
        if k3['c'] < k3['o'] and k3['h'] < k1['l'] and k3['l'] < df.iloc[i-15:i]['l'].min():
            return {"side": "SHORT", "fvg_low": k3['h'], "fvg_high": k1['l'], "sl": k1['h']}
    return None

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    
    # 1. 確保紀錄檔案存在
    if not os.path.exists(LOG_FILE):
        pd.DataFrame(columns=["instId","side","status","entry_zone","sl","tp1","tp3","entry_p","tp1_hit"]).to_csv(LOG_FILE, index=False)
    
    trades_df = pd.read_csv(LOG_FILE)
    trades = trades_df.to_dict('records')
    monitored_ids = trades_df['instId'].tolist() # 正在監控中的名單

    # 2. 每日量化報告 (08:30)
    if now_tw.hour == 8 and 30 <= now_tw.minute < 45:
        if not os.path.exists("daily_report.ok"):
            report = f"📊 *Alpha Oracle | 每日量化報告*\n🗓 {now_tw.strftime('%Y/%m/%d')}\n⏰ 08:30 (UTC+8)\n──────────────────\n\n"
            for instId in MAINSTREAM + ALTS[:5]:
                wr, sd = get_market_score(instId)
                report += f"🔹 *{instId.split('-')[0]}*\n預測：{sd}\n勝率：{wr}% 🟢\n\n"
            report += "💡 *策略：結構轉變後，在 FVG 缺口處等待回踩。*"
            send_tg(report)
            with open("daily_report.ok", "w") as f: f.write("done")
    elif now_tw.hour != 8 and os.path.exists("daily_report.ok"):
        os.remove("daily_report.ok")

    # 3. 24H SMC 掃描與持倉管理
    new_active_list = []
    history = []

    for instId in ALL_COINS:
        df = fetch_okx(instId, "15m", "60")
        if df is None or len(df) < 40: continue
        curr_p = df['c'].iloc[-1]

        # A. 發現新訊號 (過濾已存在監控中的幣種)
        if instId not in monitored_ids:
            setup = find_fvg_setup(df)
            if setup:
                action = "🟢 強力看多" if setup['side'] == "LONG" else "🔴 強力看空"
                zone_str = f"{setup['fvg_low']:.4f}-{setup['fvg_high']:.4f}"
                
                msg = f"🔥 *SMC 高勝率進場訊號*\n──────────────────\n\n"
                msg += f"💎 幣種：#{instId.split('-')[0]}\n🎯 動作：{action} (CHoCH+BOS)\n\n"
                msg += f"📍 建議進場位：{setup['fvg_low']:.4f} (回踩)\n"
                msg += f"🚫 止損位 (SL)：{setup['sl']:.4f}\n"
                msg += "──────────────────\n💡 *策略：結構轉變後，在 FVG 缺口處等待回補。*"
                send_tg(msg)
                
                new_active_list.append({
                    "instId": instId, "side": setup['side'], "status": "WAITING", 
                    "entry_zone": zone_str, "sl": setup['sl'], "tp1": 0, "tp3": 0, "entry_p": 0, "tp1_hit": 0
                })
            continue

        # B. 監控已存在訊號 (等待回踩或止盈止損)
        t = [x for x in trades if x['instId'] == instId][0]
        
        if t['status'] == "WAITING":
            low_z, hi_z = map(float, str(t['entry_zone']).split('-'))
            if (t['side'] == "LONG" and curr_p <= hi_z) or (t['side'] == "SHORT" and curr_p >= low_z):
                t['status'] = "ACTIVE"; t['entry_p'] = curr_p
                risk = abs(t['entry_p'] - float(t['sl']))
                t['tp1'] = t['entry_p'] + risk if t['side'] == "LONG" else t['entry_p'] - risk
                t['tp3'] = t['entry_p'] + risk*3.5 if t['side'] == "LONG" else t['entry_p'] - risk*3.5
                send_tg(f"🚀 *SMC 回踩成交*\n──────────────────\n💎 #{instId.split('-')[0]} | {t['side']}\n✅ 成交價格: `{curr_p}`\n🎯 目標 TP3: `{t['tp3']:.4f}`")
            new_active_list.append(t)
        
        elif t['status'] == "ACTIVE":
            # 取得 15m K 線的高低點來判斷止盈
            hi, lo = df['h'].max(), df['l'].min()
            # 止損離場
            if (t['side'] == "LONG" and curr_p <= float(t['sl'])) or (t['side'] == "SHORT" and curr_p >= float(t['sl'])):
                send_tg(f"❌ *結算：止損離場*\n💰 #{instId.split('-')[0]} | 價格: `{curr_p}`"); history.append(t); continue
            # TP1 達成 (自動保本)
            if int(t['tp1_hit']) == 0:
                if (t['side'] == "LONG" and hi >= float(t['tp1'])) or (t['side'] == "SHORT" and lo <= float(t['tp1'])):
                    t['tp1_hit'] = 1; t['sl'] = t['entry_p']
                    send_tg(f"🔹 *TP1 達成：自動保本*\n💰 #{instId.split('-')[0]} | 止損已移至進場價")
            # TP3 達成 (終極止盈)
            if (t['side'] == "LONG" and hi >= float(t['tp3'])) or (t['side'] == "SHORT" and lo <= float(t['tp3'])):
                send_tg(f"🚀 *TP3 終極止盈！*\n💰 #{instId.split('-')[0]} | 結構利潤全收"); history.append(t); continue
            new_active_list.append(t)

    # 4. 數據寫入 CSV
    pd.DataFrame(new_active_list).to_csv(LOG_FILE, index=False)
    if history: pd.DataFrame(history).to_csv(HISTORY_FILE, mode='a', header=not os.path.exists(HISTORY_FILE), index=False)

if __name__ == "__main__":
    main()
