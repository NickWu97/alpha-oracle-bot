import requests
import os
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta

# --- 測試開關：若想「現在」立刻看到 TG 報告請設為 True，測試完請改回 False ---
TEST_MODE = False 

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 幣種配置 (5主流 + 8山寨)
MAINSTREAM = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP"]
ALTS = ["SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP", "ZAMA-USDT-SWAP", "BCH-USDT-SWAP", "ASI-USDT-SWAP", "DOGE-USDT-SWAP"]
ALL_COINS = MAINSTREAM + ALTS

# 檔案路徑
LOG_FILE = "active_trades.csv"     # 存儲目前正在監控/持倉的單子
STATS_FILE = "daily_stats.csv"   # 存儲當日已結算的 TP/SL 紀錄

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
    except: return None

def find_smc_setup(df):
    """SMC 邏輯：偵測 BOS 並定義回補區間 (OB/FVG)"""
    if len(df) < 30: return None
    # 檢查最近 10 根 K 棒
    for i in range(len(df)-3, len(df)-12, -1):
        k1, k2, k3 = df.iloc[i], df.iloc[i+1], df.iloc[i+2]
        # 多頭 BOS: K3 收盤高於前 15 根最高，且 K3 與 K1 之間有 FVG 缺口
        if k3['c'] > k3['o'] and k3['l'] > k1['h'] and k3['h'] > df.iloc[i-15:i]['h'].max():
            return {"side": "LONG", "entry_p": k1['h'], "sl": k1['l'], "tp_target": 3.0}
        # 空頭 BOS
        if k3['c'] < k3['o'] and k3['h'] < k1['l'] and k3['l'] < df.iloc[i-15:i]['l'].min():
            return {"side": "SHORT", "entry_p": k1['l'], "sl": k1['h'], "tp_target": 3.0}
    return None

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    
    # 1. 確保檔案存在，若無則建立
    if not os.path.exists(LOG_FILE):
        pd.DataFrame(columns=["instId","side","status","entry_p","sl","tp3","tp1_hit"]).to_csv(LOG_FILE, index=False)
    if not os.path.exists(STATS_FILE):
        pd.DataFrame(columns=["instId","result","time"]).to_csv(STATS_FILE, index=False)

    # 2. 每日 08:30 結算昨日勝率報告
    if (now_tw.hour == 8 and 30 <= now_tw.minute < 45) or TEST_MODE:
        if not os.path.exists("daily_report.ok") or TEST_MODE:
            stats_df = pd.read_csv(STATS_FILE)
            tp_count = len(stats_df[stats_df['result'] == 'TP'])
            sl_count = len(stats_df[stats_df['result'] == 'SL'])
            total = tp_count + sl_count
            win_rate = (tp_count / total * 100) if total > 0 else 0
            
            report = f"📊 *Alpha Oracle | 每日戰績結報*\n"
            report += f"──────────────────\n"
            report += f"🗓 日期：{now_tw.strftime('%Y/%m/%d')}\n"
            report += f"✅ 止盈次數：{tp_count}\n"
            report += f"❌ 止損次數：{sl_count}\n"
            report += f"📈 總計單數：{total}\n"
            report += f"🔥 總勝率：*{win_rate:.1f}%*\n"
            report += f"──────────────────\n"
            report += "💡 *數據已歸零，開始今日監控...*"
            send_tg(report)
            
            # 報完後清空統計檔案（開始新的一天）
            pd.DataFrame(columns=["instId","result","time"]).to_csv(STATS_FILE, index=False)
            if not TEST_MODE:
                with open("daily_report.ok", "w") as f: f.write("done")
    elif now_tw.hour != 8 and os.path.exists("daily_report.ok"):
        os.remove("daily_report.ok")

    # 3. 讀取現有監控單
    trades_df = pd.read_csv(LOG_FILE)
    active_ids = trades_df['instId'].tolist()
    new_active_list = []

    # 4. 掃描所有幣種
    for instId in ALL_COINS:
        df = fetch_okx(instId, "15m", "60")
        if df is None or len(df) < 40: continue
        curr_p = df['c'].iloc[-1]

        # A. 如果該幣種不在監控中 -> 尋找新訊號 (BOS)
        if instId not in active_ids:
            setup = find_smc_setup(df)
            if setup:
                action = "🟢 強力看多" if setup['side'] == "LONG" else "🔴 強力看空"
                msg = f"🔥 *SMC 高勝率進場訊號*\n"
                msg += "──────────────────\n\n"
                msg += f"💎 幣種：#{instId.split('-')[0]}\n"
                msg += f"🎯 動作：{action} (BOS突破)\n\n"
                msg += f"📍 建議進場位：{setup['entry_p']:.4f} (回踩點)\n"
                msg += f"🚫 止損位 (SL)：{setup['sl']:.4f}\n"
                msg += "──────────────────\n"
                msg += "💡 *狀態：等待價格回調觸碰進場位...*"
                send_tg(msg)
                
                # 加入 WAITING 狀態清單
                new_active_list.append({
                    "instId": instId, "side": setup['side'], "status": "WAITING", 
                    "entry_p": setup['entry_p'], "sl": setup['sl'], "tp3": 0, "tp1_hit": 0
                })
            continue

        # B. 處理已在監控中的幣種 (等待回補或持倉中)
        t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
        
        # 情況 1: 等待回補進場 (WAITING)
        if t['status'] == "WAITING":
            is_hit = (t['side'] == "LONG" and curr_p <= t['entry_p']) or (t['side'] == "SHORT" and curr_p >= t['entry_p'])
            if is_hit:
                t['status'] = "ACTIVE"
                risk = abs(float(t['entry_p']) - float(t['sl']))
                t['tp3'] = t['entry_p'] + (risk * 3) if t['side'] == "LONG" else t['entry_p'] - (risk * 3)
                
                msg = f"🚀 *SMC 回補成交*\n"
                msg += "──────────────────\n"
                msg += f"💎 幣種：#{instId.split('-')[0]} | {t['side']}\n"
                msg += f"✅ 成交價格：`{curr_p}`\n"
                msg += f"🎯 止盈目標 (TP3)：`{t['tp3']:.4f}`\n"
                send_tg(msg)
            new_active_list.append(t)

        # 情況 2: 持倉管理 (ACTIVE)
        elif t['status'] == "ACTIVE":
            # 止損檢查
            if (t['side'] == "LONG" and curr_p <= float(t['sl'])) or (t['side'] == "SHORT" and curr_p >= float(t['sl'])):
                send_tg(f"❌ *結算：止損離場* | #{instId.split('-')[0]} 💸")
                pd.DataFrame([{"instId": instId, "result": "SL", "time": now_tw}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                continue # 移除出監控清單
            
            # 止盈檢查 (使用 15m 高低點)
            hi, lo = df['h'].max(), df['l'].min()
            if (t['side'] == "LONG" and hi >= float(t['tp3'])) or (t['side'] == "SHORT" and lo <= float(t['tp3'])):
                send_tg(f"🚀 *結算：TP3 完美止盈！* | #{instId.split('-')[0]} 💰")
                pd.DataFrame([{"instId": instId, "result": "TP", "time": now_tw}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                continue # 移除出監控清單
            
            new_active_list.append(t)

    # 5. 儲存更新後的監控清單
    pd.DataFrame(new_active_list).to_csv(LOG_FILE, index=False)

if __name__ == "__main__":
    main()
