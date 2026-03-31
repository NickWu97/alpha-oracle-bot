import requests
import os
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta

# --- 1. 基礎配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 監控幣種清單
ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP", 
    "BCH-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP"
]

LOG_FILE = "active_trades.csv"
STATS_FILE = "daily_stats.csv"

# --- 2. 工具函數 ---
def send_tg(msg):
    """發送 Telegram 通知"""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except Exception as e:
        logging.error(f"TG發送失敗: {e}")

def fetch_okx(instId, bar="15m", limit="100"):
    """獲取 OKX K線數據"""
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except Exception as e:
        logging.error(f"獲取數據失敗 {instId}: {e}")
        return None

def get_metrics(instId):
    """獲取 CVD 與 多空持倉人數比"""
    try:
        base = instId.replace("-SWAP", "")
        # 多空比
        ls_url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base}"
        ls = float(requests.get(ls_url, timeout=5).json()['data'][0]['ratio'])
        # CVD (主動買賣量)
        cvd_url = f"https://www.okx.com/api/v5/rubik/stat/taker-volume?instId={base}"
        cvd_data = requests.get(cvd_url, timeout=5).json()['data'][0]
        cvd = "BUY" if float(cvd_data['buyVol']) > float(cvd_data['sellVol']) else "SELL"
        return {"ls": ls, "cvd": cvd}
    except:
        return {"ls": 1.0, "cvd": "NEUTRAL"}

# --- 3. 核心戰略判斷 (MA28 + 12H 兩陰兩陽) ---
def check_strategy(instId):
    df_12h = fetch_okx(instId, "12H", "60")
    df_1d = fetch_okx(instId, "1D", "60")
    if df_12h is None or df_1d is None or len(df_1d) < 30: return None

    # 日線 MA28 判斷大趨勢
    df_1d['ma28'] = df_1d['c'].rolling(window=28).mean()
    c_1d = df_1d.iloc[-1]
    ma_up = c_1d['ma28'] > df_1d.iloc[-5]['ma28']
    ma_down = c_1d['ma28'] < df_1d.iloc[-5]['ma28']
    
    # 12H K 棒組合 (最近三根，排除未收盤)
    c_12h, p1_12h, p2_12h = df_12h.iloc[-1], df_12h.iloc[-2], df_12h.iloc[-3]
    
    # 【看跌判斷】日線MA28下彎 + 12H兩陰
    if ma_down and p1_12h['c'] < p1_12h['o'] and p2_12h['c'] < p2_12h['o']:
        entry = c_12h['c']
        sl = max(p1_12h['h'], p2_12h['h'])
        if (sl - entry) / entry <= 0.10: # 風險控管：止損超過 10% 放棄
            return {"side": "SHORT", "entry": entry, "sl": sl}

    # 【看漲判斷】日線MA28上揚 + 12H兩陽
    if ma_up and p1_12h['c'] > p1_12h['o'] and p2_12h['c'] > p2_12h['o']:
        entry = c_12h['c']
        sl = min(p1_12h['l'], p2_12h['l'])
        if (entry - sl) / entry <= 0.10:
            return {"side": "LONG", "entry": entry, "sl": sl}
            
    return None

# --- 4. 主程式流程 ---
def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    
    # 初始化文件
    if not os.path.exists(LOG_FILE):
        pd.DataFrame(columns=["instId","side","entry","sl","tp1","tp2","tp3","locked"]).to_csv(LOG_FILE, index=False)
    if not os.path.exists(STATS_FILE):
        pd.DataFrame(columns=["instId","result"]).to_csv(STATS_FILE, index=False)

    # --- A. 08:30 戰略早報 ---
    if now_tw.hour == 8 and 30 <= now_tw.minute < 45:
        if not os.path.exists("morning.ok"):
            report = "☀️ *Alpha Oracle | 戰略篩選早報*\n──────────────────\n"
            found_count = 0
            for coin in ALL_COINS:
                res = check_strategy(coin)
                if res:
                    found_count += 1
                    icon = "🔴" if res['side'] == "SHORT" else "🟢"
                    report += f"{icon} #{coin.split('-')[0]} 符合策略\n   預計點位：`{res['entry']}` | 止損：`{res['sl']}`\n"
            
            send_tg(report if found_count > 0 else "☀️ 目前無符合策略標的。")
            with open("morning.ok", "w") as f: f.write("done")
    elif now_tw.hour != 8 and os.path.exists("morning.ok"):
        os.remove("morning.ok")

    # --- B. 24H 實時監控 (入場與 TP/SL 執行) ---
    trades_df = pd.read_csv(LOG_FILE)
    active_ids = trades_df['instId'].tolist()
    new_trades = []

    for instId in ALL_COINS:
        df_15 = fetch_okx(instId, "15m")
        if df_15 is None or df_15.empty: continue
        curr_p = df_15['c'].iloc[-1]

        # 1. 進場掃描
        if instId not in active_ids:
            res = check_strategy(instId)
            if res:
                m = get_metrics(instId)
                # 數據濾網：多單(CVD BUY & LS<1.2) / 空單(CVD SELL & LS>1.2)
                is_l = res['side']=="LONG" and m['cvd']=="BUY" and m['ls'] < 1.2
                is_s = res['side']=="SHORT" and m['cvd']=="SELL" and m['ls'] > 1.2
                
                if is_l or is_s:
                    # 計算階梯止盈 (1.5R / 2.0R / 3.0R)
                    r = abs(res['entry'] - res['sl'])
                    tp1 = res['entry'] + (1.5*r) if is_l else res['entry'] - (1.5*r)
                    tp2 = res['entry'] + (2.0*r) if is_l else res['entry'] - (2.0*r)
                    tp3 = res['entry'] + (3.0*r) if is_l else res['entry'] - (3.0*r)
                    
                    send_tg(f"🚀 *策略激活入場*\n幣種：#{instId} | 方向：{res['side']}\n📍 入場位：`{res['entry']}`\n🚫 初始止損：`{res['sl']}`\n🎯 獲利目標：`{tp3}`")
                    new_trades.append({
                        "instId": instId, "side": res['side'], "entry": res['entry'], 
                        "sl": res['sl'], "tp1": tp1, "tp2": tp2, "tp3": tp3, "locked": 0
                    })
            continue

        # 2. 持倉管理 (固定點位與 TP2 鎖盈)
        t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()

        # A. 鎖盈邏輯：觸及 TP2 後，將止損移至 TP1
        if t['locked'] == 0:
            hit_tp2 = (t['side']=="LONG" and curr_p >= t['tp2']) or (t['side']=="SHORT" and curr_p <= t['tp2'])
            if hit_tp2:
                t['locked'] = 1
                t['sl'] = t['tp1'] # 止損保本鎖盈
                send_tg(f"🔒 *鎖盈通知* | #{instId}\n價格已觸及 TP2，止損點同步移動至 TP1 (`{t['tp1']}`) 以確保獲利！")

        # B. 離場判定 (觸及 SL 或 終極止盈 TP3)
        is_sl = (t['side']=="LONG" and curr_p <= t['sl']) or (t['side']=="SHORT" and curr_p >= t['sl'])
        is_tp3 = (t['side']=="LONG" and curr_p >= t['tp3']) or (t['side']=="SHORT" and curr_p <= t['tp3'])
        
        if is_sl or is_tp3:
            status_msg = "✅ 恭喜！TP3 完美止盈離場" if is_tp3 else "⚠️ 觸及止損/鎖盈點離場"
            send_tg(f"🏁 *結算通知* | #{instId}\n結果：{status_msg}\n最終價格：`{curr_p}`")
            pd.DataFrame([{"instId":instId, "result":"OUT"}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
            continue

        new_trades.append(t)

    # 更新進行中訂單數據
    pd.DataFrame(new_trades).to_csv(LOG_FILE, index=False)

if __name__ == "__main__":
    main()
