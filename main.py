import requests
import os
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta

# 1. 基礎配置
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 幣種清單
MAINSTREAM = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP"]
ALTS = ["SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP", "PEPE-USDT-SWAP", "ASI-USDT-SWAP", "DOGE-USDT-SWAP"]
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
        if 'data' not in res: return None
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except: return None

def get_market_score(instId):
    """計算趨勢強度 (距離 4H EMA200 乖離)"""
    df = fetch_okx(instId, "4H", "250")
    if df is None or len(df) < 200: return -999
    ema200 = df['c'].ewm(span=200, adjust=False).mean().iloc[-1]
    return round(((df['c'].iloc[-1] - ema200) / ema200) * 100, 2)

def find_fvg_setup(df):
    """偵測 SMC 結構：BOS 突破 + FVG 缺口形成"""
    if len(df) < 10: return None
    # 檢查最近 5 根 K 棒
    for i in range(len(df)-3, len(df)-6, -1):
        k1, k2, k3 = df.iloc[i], df.iloc[i+1], df.iloc[i+2]
        
        # 多頭 FVG: K3 低點 > K1 高點 (強勢陽線 BOS)
        if k3['c'] > k3['o'] and k3['l'] > k1['h']:
            if k3['h'] > df.iloc[i-10:i]['h'].max(): # 確認是突破 BOS
                return {"side": "LONG", "fvg_low": k1['h'], "fvg_high": k3['l'], "sl": k1['l']}
        
        # 空頭 FVG: K3 高點 < K1 低點 (強勢陰線 BOS)
        if k3['c'] < k3['o'] and k3['h'] < k1['l']:
            if k3['l'] < df.iloc[i-10:i]['l'].min(): # 確認是跌破 BOS
                return {"side": "SHORT", "fvg_low": k3['h'], "fvg_high": k1['l'], "sl": k1['h']}
    return None

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    
    # --- 1. 08:30 高勝率 5+5 日報 ---
    if now_tw.hour == 8 and 30 <= now_tw.minute < 45:
        if not os.path.exists("daily_report.ok"):
            m_list = sorted([(c, get_market_score(c)) for c in MAINSTREAM], key=lambda x: x[1], reverse=True)[:5]
            a_list = sorted([(c, get_market_score(c)) for c in ALTS], key=lambda x: x[1], reverse=True)[:5]
            
            msg = f"☀️ *Alpha Oracle 晨間高勝率篩選*\n📅 `{now_tw.strftime('%m/%d')}` | ⏰ `08:30` AM\n\n"
            msg += "🔵 *主流幣 Top 5:*\n" + "\n".join([f"• #{x[0].split('-')[0]} (`{x[1]}%`)" for x in m_list])
            msg += "\n\n🟠 *山寨幣 Top 5:*\n" + "\n".join([f"• #{x[0].split('-')[0]} (`{x[1]}%`)" for x in a_list])
            msg += "\n\n🚀 *SMC 24H 狙擊系統已鎖定回踩區間...*"
            send_tg(msg)
            with open("daily_report.ok", "w") as f: f.write("done")
    elif now_tw.hour != 8 and os.path.exists("daily_report.ok"):
        os.remove("daily_report.ok")

    # --- 2. 載入與初始化持倉檔案 ---
    if not os.path.exists(LOG_FILE):
        pd.DataFrame(columns=["instId","side","status","entry_zone","sl","tp1","tp3","entry_p","tp1_hit"]).to_csv(LOG_FILE, index=False)
    
    trades = pd.read_csv(LOG_FILE).to_dict('records')
    new_active_list = []
    history = []

    # --- 3. 全天候監控與進場邏輯 ---
    active_ids = [t['instId'] for t in trades]
    
    for instId in ALL_COINS:
        df_15 = fetch_okx(instId, "15m", "60")
        if df_15 is None or len(df_15) < 30: continue
        curr_p = df_15['c'].iloc[-1]

        # A. 若該幣種不在監控中，尋找新訊號 (BOS + FVG)
        if instId not in active_ids:
            setup = find_fvg_setup(df_15)
            if setup:
                zone = f"{setup['fvg_low']:.4f}-{setup['fvg_high']:.4f}"
                send_tg(f"🔍 *SMC 結構突破 (BOS)*\n💎 #{instId.split('-')[0]} | {setup['side']}\n📐 FVG 回踩區: `{zone}`\n⏳ *等待價格進入區間進場...*")
                new_active_list.append({"instId": instId, "side": setup['side'], "status": "WAITING", 
                                        "entry_zone": zone, "sl": setup['sl'], "tp1": 0, "tp3": 0, "entry_p": 0, "tp1_hit": 0})
            continue

        # B. 若在監控中，處理 WAITING (回踩) 或 ACTIVE (持倉)
        t = [x for x in trades if x['instId'] == instId][0]
        
        if t['status'] == "WAITING":
            low_z, hi_z = map(float, str(t['entry_zone']).split('-'))
            # 判斷回踩進場
            if (t['side'] == "LONG" and curr_p <= hi_z) or (t['side'] == "SHORT" and curr_p >= low_z):
                t['status'] = "ACTIVE"
                t['entry_p'] = curr_p
                risk = abs(t['entry_p'] - t['sl'])
                t['tp1'] = t['entry_p'] + risk if t['side'] == "LONG" else t['entry_p'] - risk
                t['tp3'] = t['entry_p'] + risk*3 if t['side'] == "LONG" else t['entry_p'] - risk*3
                send_tg(f"🚀 *SMC 回踩成功：成交進場*\n💎 #{instId.split('-')[0]} | {t['side']}\n✅ 價格: `{curr_p}`\n🚫 止損: `{t['sl']:.4f}`\n🎯 TP3: `{t['tp3']:.4f}`")
            new_active_list.append(t)

        elif t['status'] == "ACTIVE":
            hi, lo = df_15['h'].max(), df_15['l'].min()
            # 止損判定
            if (t['side'] == "LONG" and curr_p <= t['sl']) or (t['side'] == "SHORT" and curr_p >= t['sl']):
                send_tg(f"❌ *止損離場*\n💰 #{instId} | 價格: `{curr_p}`"); history.append(t); continue
            # TP1 保本
            if t['tp1_hit'] == 0:
                if (t['side'] == "LONG" and hi >= t['tp1']) or (t['side'] == "SHORT" and lo <= t['tp1']):
                    t['tp1_hit'] = 1; t['sl'] = t['entry_p']
                    send_tg(f"🔹 *TP1 達成：自動保本*\n💰 #{instId} | 止損移至: `{t['sl']:.4f}`")
            # TP3 止盈
            if (t['side'] == "LONG" and hi >= t['tp3']) or (t['side'] == "SHORT" and lo <= t['tp3']):
                send_tg(f"🚀 *TP3 終極止盈！*\n💰 #{instId}"); history.append(t); continue
            new_active_list.append(t)

    # 4. 存檔與更新
    pd.DataFrame(new_active_list).to_csv(LOG_FILE, index=False)
    if history: pd.DataFrame(history).to_csv(HISTORY_FILE, mode='a', header=not os.path.exists(HISTORY_FILE), index=False)

if __name__ == "__main__":
    main()
