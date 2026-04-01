import requests
import os
import pandas as pd
import numpy as np
import logging
import traceback
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
def get_extra_metrics(instId):
    """獲取資費與多空比"""
    try:
        base_id = instId.replace("-SWAP", "")
        f_res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5).json()
        funding = f"{float(f_res['data'][0]['fundingRate']) * 100:.4f}%"
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5).json()
        ls_ratio = ls_res['data'][0]['ratio']
        return {"funding": funding, "ls_ratio": ls_ratio}
    except:
        return {"funding": "N/A", "ls_ratio": "N/A"}

def send_tg(msg):
    """發送 Telegram 訊息"""
    if not TG_TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except: pass

def fetch_okx(instId):
    """獲取 K 線數據"""
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=15m&limit=100"
        res = requests.get(url, timeout=10).json()
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except: return None

def calculate_atr(df):
    """計算 ATR 用於止損"""
    high_low = df['h'] - df['l']
    high_close = np.abs(df['h'] - df['c'].shift())
    low_close = np.abs(df['l'] - df['c'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(window=14).mean().iloc[-1]

def find_smc_setup(df):
    """SMC 結構發現邏輯"""
    if df is None or len(df) < 40: return None
    atr = calculate_atr(df)
    for i in range(len(df)-3, len(df)-25, -1):
        k0, k1, k2 = df.iloc[i-1], df.iloc[i], df.iloc[i+1]
        # 多頭 Choch
        if k2['c'] > k2['o'] and k2['c'] > df['h'].iloc[i-15:i].max():
            entry = (k2['l'] + k0['h']) / 2 if k2['l'] > k0['h'] else (k1['l'] + k1['o']) / 2
            sl = k1['l'] - (0.4 * atr)
            return {"side": "LONG", "entry": entry, "sl": sl}
        # 空頭 Choch
        if k2['c'] < k2['o'] and k2['c'] < df['l'].iloc[i-15:i].min():
            entry = (k2['h'] + k0['l']) / 2 if k2['h'] < k0['l'] else (k1['h'] + k1['o']) / 2
            sl = k1['h'] + (0.4 * atr)
            return {"side": "SHORT", "entry": entry, "sl": sl}
    return None

# --- 3. 主程式邏輯 ---
def main():
    try:
        now_tw = datetime.utcnow() + timedelta(hours=8)
        manual_report = os.getenv("MANUAL_REPORT", "false").lower() == "true"
        
        log_cols = ["instId","side","status","entry","sl","tp1","tp2","tp3","locked"]
        stats_cols = ["instId","result"]

        # 檔案物理檢查與初始化 (防止 EmptyDataError)
        for f, cols in zip([LOG_FILE, STATS_FILE], [log_cols, stats_cols]):
            if not os.path.exists(f) or os.stat(f).st_size == 0:
                pd.DataFrame(columns=cols).to_csv(f, index=False)

        # 🌙 A. 午夜勝率報表 (00:00 - 00:15 觸發)
        is_midnight = (now_tw.hour == 0 and 0 <= now_tw.minute < 15)
        if is_midnight or manual_report:
            if not os.path.exists("midnight.ok") or manual_report:
                df_s = pd.read_csv(STATS_FILE)
                if not df_s.empty:
                    tp_c = len(df_s[df_s['result'] == 'TP'])
                    sl_c = len(df_s[df_s['result'] == 'SL'])
                    total = tp_c + sl_c
                    wr = (tp_c / total * 100) if total > 0 else 0
                    
                    report_msg = f"📊 *Alpha Oracle 戰績回報*\n"
                    report_msg += f"──────────────────\n\n"
                    report_msg += f"✅ 獲利/保本：{tp_c} 單\n"
                    report_msg += f"❌ 虧損離場：{sl_c} 單\n"
                    report_msg += f"🔥 昨日勝率：*{wr:.1f}%*\n\n"
                    report_msg += f"🕒 統計時間：{now_tw.strftime('%Y-%m-%d')}"
                    send_tg(report_msg)
                    
                    if is_midnight:
                        pd.DataFrame(columns=stats_cols).to_csv(STATS_FILE, index=False)
                        with open("midnight.ok", "w") as f: f.write("ok")
        elif now_tw.hour != 0 and os.path.exists("midnight.ok"):
            os.remove("midnight.ok")

        # B. 核心監控邏輯
        try:
            trades_df = pd.read_csv(LOG_FILE)
        except:
            trades_df = pd.DataFrame(columns=log_cols)

        active_ids = trades_df['instId'].tolist()
        updated_trades = []

        for instId in ALL_COINS:
            df = fetch_okx(instId)
            if df is None or df.empty: continue
            curr_p, m = df['c'].iloc[-1], get_extra_metrics(instId)

            # 1. 掃描新機會
            if instId not in active_ids:
                setup = find_smc_setup(df)
                if setup:
                    risk = abs(setup['entry'] - setup['sl'])
                    tp1 = setup['entry'] + risk*1.5 if setup['side']=="LONG" else setup['entry'] - risk*1.5
                    tp2 = setup['entry'] + risk*2.0 if setup['side']=="LONG" else setup['entry'] - risk*2.0
                    tp3 = setup['entry'] + risk*3.0 if setup['side']=="LONG" else setup['entry'] - risk*3.0
                    
                    msg = f"🔍 *Alpha Oracle | 發現機會*\n"
                    msg += f"──────────────────\n\n"
                    msg += f"💎 幣種：#{instId.split('-')[0]}\n"
                    msg += f"🎯 動作：{'🟢 多' if setup['side']=='LONG' else '🔴 空'}\n"
                    msg += f"📊 數據：CVD {m['funding']} | LS {m['ls_ratio']}\n\n"
                    msg += f"📍 進場位：{setup['entry']:.4f}\n"
                    msg += f"🚫 止損位：{setup['sl']:.4f}\n\n"
                    msg += f"💰 TP1 (1.5R)：{tp1:.4f}\n"
                    msg += f"💰 TP2 (2.0R)：{tp2:.4f}\n"
                    msg += f"💰 TP3 (3.0R)：{tp3:.4f}\n\n"
                    msg += f"💡 *等待價格回踩成交...*"
                    send_tg(msg)
                    updated_trades.append({"instId":instId,"side":setup['side'],"status":"WAITING","entry":setup['entry'],"sl":setup['sl'],"tp1":tp1,"tp2":tp2,"tp3":tp3,"locked":0})
                continue

            # 2. 追蹤現有訂單
            t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
            
            # [通知] 等待成交 -> 已成交
            if t['status'] == "WAITING":
                is_hit = (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry'])
                if is_hit:
                    t['status'] = "ACTIVE"
                    msg = f"🚀 *Alpha Oracle | 成交提醒*\n"
                    msg += f"──────────────────\n"
                    msg += f"✅ #{instId.split('-')[0]} 已觸發進場\n"
                    msg += f"📍 成交價格：{curr_p:.4f}\n"
                    msg += f"🛡️ 止損設定：{t['sl']:.4f}\n"
                    msg += f"📊 當前數據：CVD {m['funding']} | LS {m['ls_ratio']}"
                    send_tg(msg)
                updated_trades.append(t)
            
            # [通知] 持倉中 -> 鎖利/結算
            elif t['status'] == "ACTIVE":
                # 達 2.0R 觸發鎖利 (止損移至 TP1)
                if t['locked'] == 0 and ((t['side']=="LONG" and curr_p >= t['tp2']) or (t['side']=="SHORT" and curr_p <= t['tp2'])):
                    t['locked'], t['sl'] = 1, t['tp1']
                    msg = f"🔒 *Alpha Oracle | 鎖利保護*\n"
                    msg += f"──────────────────\n"
                    msg += f"#{instId.split('-')[0]} 已達 2.0R 目標\n"
                    msg += f"🛡️ 止損已自動移至 TP1：{t['tp1']:.4f}"
                    send_tg(msg)
                
                # 結算判斷
                is_sl = (t['side']=="LONG" and curr_p <= t['sl']) or (t['side']=="SHORT" and curr_p >= t['sl'])
                is_tp3 = (t['side']=="LONG" and curr_p >= t['tp3']) or (t['side']=="SHORT" and curr_p <= t['tp3'])
                
                if is_sl or is_tp3:
                    # 保本算 TP 核心邏輯：如果 locked=1(已鎖利) 或 達到 TP3，皆記為 TP
                    final_res = "TP" if (is_tp3 or t['locked'] == 1) else "SL"
                    
                    msg = f"🏁 *Alpha Oracle | 交易結算*\n"
                    msg += f"──────────────────\n"
                    msg += f"#{instId.split('-')[0]} 結算離場\n"
                    msg += f"🏆 結果：{'💰 強力止盈 (3R)' if is_tp3 else ('🛡️ 保本離場' if t['locked']==1 else '❌ 止損離場')}\n"
                    msg += f"📍 出場價格：{curr_p:.4f}"
                    send_tg(msg)
                    
                    pd.DataFrame([{"instId":instId,"result":final_res}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                    continue
                updated_trades.append(t)

        # 儲存更新後的狀態
        pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)
    except:
        traceback.print_exc()

if __name__ == "__main__":
    main()
