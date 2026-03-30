import requests
import os
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta

# 1. 系統與日誌配置
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 幣種清單 (5主流 + 8山寨)
MAINSTREAM = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP"]
ALTS = ["SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP", "ZAMA-USDT-SWAP", "BCH-USDT-SWAP", "ASI-USDT-SWAP", "DOGE-USDT-SWAP"]
ALL_COINS = MAINSTREAM + ALTS

LOG_FILE = "active_trades.csv"
HISTORY_FILE = "trade_history.csv"

def send_tg(msg):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except:
        logging.error("Telegram 發送失敗")

def fetch_okx(instId, bar="15m", limit="100"):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if 'data' not in res or not res['data']: return None
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except Exception as e:
        logging.error(f"獲取 {instId} 數據失敗: {e}")
        return None

def get_market_score(instId):
    """計算趨勢強度：當前價格距離 4H EMA200 的百分比"""
    df = fetch_okx(instId, "4H", "250")
    if df is None or len(df) < 200: return -999
    ema200 = df['c'].ewm(span=200, adjust=False).mean().iloc[-1]
    return round(((df['c'].iloc[-1] - ema200) / ema200) * 100, 2)

def find_fvg_setup(df):
    """SMC 核心：尋找 BOS (結構突破) 與 FVG (公允價值缺口)"""
    if len(df) < 20: return None
    # 檢查最近 5-8 根 K 棒，尋找強勢推動產生的缺口
    for i in range(len(df)-3, len(df)-8, -1):
        k1, k2, k3 = df.iloc[i], df.iloc[i+1], df.iloc[i+2]
        
        # 多頭 FVG: K3 低點 > K1 高點 (強勢陽線突破 BOS)
        if k3['c'] > k3['o'] and k3['l'] > k1['h']:
            if k3['h'] > df.iloc[i-15:i]['h'].max(): # 確認為 BOS 突破
                return {"side": "LONG", "fvg_low": k1['h'], "fvg_high": k3['l'], "sl": k1['l']}
        
        # 空頭 FVG: K3 高點 < K1 低點 (強勢陰線跌破 BOS)
        if k3['c'] < k3['o'] and k3['h'] < k1['l']:
            if k3['l'] < df.iloc[i-15:i]['l'].min(): # 確認為 BOS 跌破
                return {"side": "SHORT", "fvg_low": k3['h'], "fvg_high": k1['l'], "sl": k1['h']}
    return None

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    
    # --- 1. 每日 08:30 高勝率 5+5 篩選日報 ---
    if now_tw.hour == 8 and 30 <= now_tw.minute < 45:
        if not os.path.exists("daily_report.ok"):
            m_ranked = sorted([(c, get_market_score(c)) for c in MAINSTREAM], key=lambda x: x[1], reverse=True)[:5]
            a_ranked = sorted([(c, get_market_score(c)) for c in ALTS], key=lambda x: x[1], reverse=True)[:5]
            
            report = f"☀️ *Alpha Oracle 晨間篩選報告*\n📅 `{now_tw.strftime('%Y/%m/%d')}` | ⏰ `08:30` AM\n\n"
            report += "🔵 *主流幣 Top 5 (趨勢強):*\n" + "\n".join([f"• #{x[0].split('-')[0]} (乖離: `{x[1]}%`)" for x in m_ranked])
            report += "\n\n🟠 *山寨幣 Top 5 (爆發力):*\n" + "\n".join([f"• #{x[0].split('-')[0]} (乖離: `{x[1]}%`)" for x in a_ranked])
            report += "\n\n📐 *SMC 系統已鎖定以上幣種，等待 FVG 回補進場...*"
            send_tg(report)
            with open("daily_report.ok", "w") as f: f.write("done")
    elif now_tw.hour != 8 and os.path.exists("daily_report.ok"):
        os.remove("daily_report.ok")

    # --- 2. 初始化資料檔案 ---
    if not os.path.exists(LOG_FILE):
        pd.DataFrame(columns=["instId","side","status","entry_zone","sl","tp1","tp3","entry_p","tp1_hit"]).to_csv(LOG_FILE, index=False)
    
    trades = pd.read_csv(LOG_FILE).to_dict('records')
    active_list, history = [], []
    monitored_ids = [t['instId'] for t in trades]

    # --- 3. 24H SMC 技術分析掃描 ---
    for instId in ALL_COINS:
        df_15 = fetch_okx(instId, "15m", "60")
        if df_15 is None or len(df_15) < 40: continue
        curr_p = df_15['c'].iloc[-1]

        # 如果此幣種目前沒單：尋找突破結構
        if instId not in monitored_ids:
            setup = find_fvg_setup(df_15)
            if setup:
                zone_str = f"{setup['fvg_low']:.4f}-{setup['fvg_high']:.4f}"
                send_tg(f"🔍 *SMC 結構突破 (BOS)*\n💎 #{instId.split('-')[0]} | {setup['side']}\n📐 回踩進場區: `{zone_str}`\n⏳ *等待價格回測區間中...*")
                active_list.append({"instId": instId, "side": setup['side'], "status": "WAITING", 
                                    "entry_zone": zone_str, "sl": setup['sl'], "tp1": 0, "tp3": 0, "entry_p": 0, "tp1_hit": 0})
            continue

        # 如果此幣種已經在監控中
        t = [x for x in trades if x['instId'] == instId][0]
        
        # 狀態：等待回補 (WAITING)
        if t['status'] == "WAITING":
            try:
                low_z, hi_z = map(float, str(t['entry_zone']).split('-'))
                if (t['side'] == "LONG" and curr_p <= hi_z) or (t['side'] == "SHORT" and curr_p >= low_z):
                    t['status'] = "ACTIVE"
                    t['entry_p'] = curr_p
                    risk = abs(t['entry_p'] - float(t['sl']))
                    t['tp1'] = t['entry_p'] + risk if t['side'] == "LONG" else t['entry_p'] - risk
                    t['tp3'] = t['entry_p'] + risk*3.5 if t['side'] == "LONG" else t['entry_p'] - risk*3.5
                    send_tg(f"🚀 *SMC 回補成功：成交進場*\n💎 #{instId.split('-')[0]} | {t['side']}\n✅ 價格: `{curr_p}`\n🎯 TP3 目標: `{t['tp3']:.4f}`")
                active_list.append(t)
            except: continue

        # 狀態：持倉管理 (ACTIVE)
        elif t['status'] == "ACTIVE":
            hi, lo = df_15['h'].max(), df_15['l'].min()
            # 止損
            if (t['side'] == "LONG" and curr_p <= float(t['sl'])) or (t['side'] == "SHORT" and curr_p >= float(t['sl'])):
                send_tg(f"❌ *結算：止損離場*\n💰 #{instId} | 價格: `{curr_p}`"); history.append(t); continue
            # TP1 自動保本
            if int(t['tp1_hit']) == 0:
                if (t['side'] == "LONG" and hi >= float(t['tp1'])) or (t['side'] == "SHORT" and lo <= float(t['tp1'])):
                    t['tp1_hit'] = 1; t['sl'] = t['entry_p']
                    send_tg(f"🔹 *TP1 達成：已移至保本*\n💰 #{instId} | 止損更新: `{t['sl']:.4f}`")
            # TP3 最終止盈
            if (t['side'] == "LONG" and hi >= float(t['tp3'])) or (t['side'] == "SHORT" and lo <= float(t['tp3'])):
                send_tg(f"🚀 *TP3 終極止盈！*\n💰 #{instId} | 結構利潤完美收割"); history.append(t); continue
            active_list.append(t)

    # 4. 資料更新與寫入
    pd.DataFrame(active_list).to_csv(LOG_FILE, index=False)
    if history: pd.DataFrame(history).to_csv(HISTORY_FILE, mode='a', header=not os.path.exists(HISTORY_FILE), index=False)

if __name__ == "__main__":
    main()
