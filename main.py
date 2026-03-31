import requests
import os
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta

# --- 基礎配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 監控幣種
ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP", 
    "BCH-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP"
]

LOG_FILE = "active_trades.csv"
STATS_FILE = "daily_stats.csv"

# --- 數據工具 ---
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
        if 'data' not in res: return None
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        # 只取已收盤數據並轉為正序
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except:
        return None

def find_smc_setup(df):
    if df is None or len(df) < 35: return None
    # 掃描結構：判斷 BOS 突破與 FVG 區間
    for i in range(len(df)-3, len(df)-15, -1):
        k1, k2, k3 = df.iloc[i], df.iloc[i+1], df.iloc[i+2]
        # 多頭趨勢：收盤高於前 15 根最高點
        if k3['c'] > k3['o'] and k3['l'] > k1['h'] and k3['h'] > df.iloc[i-15:i]['h'].max():
            return {"side": "LONG", "entry": k1['h'], "sl": k1['l']}
        # 空頭趨勢：收盤低於前 15 根最低點
        if k3['c'] < k3['o'] and k3['h'] < k1['l'] and k3['l'] < df.iloc[i-15:i]['l'].min():
            return {"side": "SHORT", "entry": k1['l'], "sl": k1['h']}
    return None

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    
    # 檔案初始化
    if not os.path.exists(LOG_FILE):
        pd.DataFrame(columns=["instId","side","status","entry","sl","tp1","tp2","tp3","locked"]).to_csv(LOG_FILE, index=False)
    if not os.path.exists(STATS_FILE):
        pd.DataFrame(columns=["instId","result"]).to_csv(STATS_FILE, index=False)

    # --- 1. ☀️ 08:30 趨勢早報 (使用 12H 大週期) ---
    if now_tw.hour == 8 and 30 <= now_tw.minute < 45:
        if not os.path.exists("morning.ok"):
            report = f"☀️ *Alpha Oracle | 12H 趨勢早報*\n──────────────────\n🗓 {now_tw.strftime('%Y/%m/%d')}\n\n🎯 **今日高勝率觀察名單：**\n"
            found_trend = False
            for coin in ALL_COINS:
                df_12h = fetch_okx(coin, bar="12H", limit="40")
                setup_12h = find_smc_setup(df_12h)
                if setup_12h:
                    found_trend = True
                    side_icon = "🟢 強勢看多" if setup_12h['side'] == "LONG" else "🔴 強勢看空"
                    report += f"• #{coin.split('-')[0]} | {side_icon}\n"
            
            if not found_trend:
                report += "🧊 目前大週期趨勢不明朗，建議保守操作。"
            
            report += "\n💡 *執行邏輯：* 15M 機器人將 24H 持續掃描上述幣種的回補機會。"
            send_tg(report)
            with open("morning.ok", "w") as f: f.write("done")
    elif now_tw.hour != 8 and os.path.exists("morning.ok"):
        os.remove("morning.ok")

    # --- 2. 🌙 00:00 勝率晚報 (每日總結) ---
    if now_tw.hour == 0 and 0 <= now_tw.minute < 15:
        if not os.path.exists("midnight.ok"):
            df_s = pd.read_csv(STATS_FILE)
            tp_c = len(df_s[df_s['result'] == 'TP'])
            sl_c = len(df_s[df_s['result'] == 'SL'])
            wr = (tp_c / (tp_c + sl_c) * 100) if (tp_c + sl_c) > 0 else 0
            
            summary = f"🌙 *Alpha Oracle | 每日實戰結算*\n──────────────────\n"
            summary += f"🗓 結算日期：{(now_tw - timedelta(days=1)).strftime('%Y/%m/%d')}\n"
            summary += f"✅ 止盈次數：{tp_c} | ❌ 止損次數：{sl_c}\n"
            summary += f"🔥 今日勝率：*{wr:.1f}%*\n──────────────────"
            send_tg(summary)
            # 重置今日戰績
            pd.DataFrame(columns=["instId","result"]).to_csv(STATS_FILE, index=False)
            with open("midnight.ok", "w") as f: f.write("done")
    elif now_tw.hour != 0 and os.path.exists("midnight.ok"):
        os.remove("midnight.ok")

    # --- 3. 核心 24H 做單監控 (使用 15M 小週期) ---
    trades_df = pd.read_csv(LOG_FILE)
    active_ids = trades_df['instId'].tolist()
    new_trades = []

    for instId in ALL_COINS:
        df_15m = fetch_okx(instId, bar="15m")
        if df_15m is None or df_15m.empty: continue
        curr_p = df_15m['c'].iloc[-1]

        # A. 搜尋 15M 回踩訊號
        if instId not in active_ids:
            setup = find_smc_setup(df_15m)
            if setup:
                risk = abs(setup['entry'] - setup['sl'])
                tp1 = setup['entry'] + risk*1.5 if setup['side']=="LONG" else setup['entry'] - risk*1.5
                tp2 = setup['entry'] + risk*2.0 if setup['side']=="LONG" else setup['entry'] - risk*2.0
                tp3 = setup['entry'] + risk*3.0 if setup['side']=="LONG" else setup['entry'] - risk*3.0
                
                prec = 4 if setup['entry'] < 10 else 2
                msg = f"🔍 *Alpha Oracle | 15M 發現訊號*\n──────────────────\n"
                msg += f"💎 幣種：#{instId.split('-')[0]}\n🎯 動作：{'🟢 多' if setup['side']=='LONG' else '🔴 空'}\n\n"
                msg += f"📍 **進場點：`{setup['entry']:.{prec}f}`**\n"
                msg += f"🚫 **止損點：`{setup['sl']:.{prec}f}`**\n\n"
                msg += f"💰 **TP1 (1.5R)：`{tp1:.{prec}f}`**\n"
                msg += f"💰 **TP2 (2.0R)：`{tp2:.{prec}f}`**\n"
                msg += f"💰 **TP3 (3.0R)：`{tp3:.{prec}f}`**\n"
                msg += "──────────────────\n💡 *觸及 TP2 後將自動提醒鎖利。*"
                send_tg(msg)
                
                new_trades.append({"instId":instId,"side":setup['side'],"status":"WAITING","entry":setup['entry'],"sl":setup['sl'],"tp1":tp1,"tp2":tp2,"tp3":tp3,"locked":0})
            continue

        # B. 既存訂單追蹤
        t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
        prec = 4 if t['entry'] < 10 else 2
        
        if t['status'] == "WAITING":
            # 判斷價格是否回補成交
            hit_long = (t['side'] == "LONG" and curr_p <= t['entry'])
            hit_short = (t['side'] == "SHORT" and curr_p >= t['entry'])
            if hit_long or hit_short:
                t['status'] = "ACTIVE"
                send_tg(f"🚀 *SMC 回補成交* | #{instId.split('-')[0]}\n✅ 價格：`{curr_p:.{prec}f}`")
            new_trades.append(t)
            
        elif t['status'] == "ACTIVE":
            # TP2 鎖利提醒
            is_tp2 = (t['side']=="LONG" and curr_p >= t['tp2']) or (t['side']=="SHORT" and curr_p <= t['tp2'])
            if is_tp2 and t['locked'] == 0:
                t['locked'] = 1
                send_tg(f"🔒 *獲利保全提醒* | #{instId.split('-')[0]}\n⚠️ 價格已達 **TP2 (2.0R)**，請將止損移至 **TP1 (`{t['tp1']:.{prec}f}`)**！")
            
            # 止損與 TP1 鎖盈判定
            current_sl = t['tp1'] if t['locked'] == 1 else t['sl']
            is_sl = (t['side']=="LONG" and curr_p <= current_sl) or (t['side']=="SHORT" and curr_p >= current_sl)
            # TP3 止盈判定
            is_tp3 = (t['side']=="LONG" and df_15m['h'].max() >= t['tp3']) or (t['side']=="SHORT" and df_15m['l'].min() <= t['tp3'])
            
            if is_sl:
                res_txt = "🔒 鎖盈" if t['locked'] == 1 else "❌ 止損"
                send_tg(f"⚠️ *離場公告* | #{instId.split('-')[0]}\n結果：{res_txt}")
                pd.DataFrame([{"instId":instId,"result":"SL"}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                continue
            if is_tp3:
                send_tg(f"🔥 *終極結算：TP3 止盈成功* | #{instId.split('-')[0]} 💰")
                pd.DataFrame([{"instId":instId,"result":"TP"}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                continue
            new_trades.append(t)

    # 儲存結果
    pd.DataFrame(new_trades).to_csv(LOG_FILE, index=False)

if __name__ == "__main__":
    main()
