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

# 監控幣種
ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP", 
    "BCH-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP"
]

LOG_FILE = "active_trades.csv"
STATS_FILE = "daily_stats.csv"

# --- 2. 工具函數 ---

def get_extra_metrics(instId):
    """抓取情緒數據：CVD(由資金費率模擬) 與 LS Ratio"""
    try:
        base_id = instId.replace("-SWAP", "").split("-")[0]
        # 1. 資金費率
        f_res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5).json()
        funding = f"{float(f_res['data'][0]['fundingRate']) * 100:.4f}%"
        # 2. 多空持倉人數比 (使用 Rubik API)
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5).json()
        ls_ratio = ls_res['data'][0]['ratio']
        return {"funding": funding, "ls_ratio": ls_ratio}
    except:
        return {"funding": "N/A", "ls_ratio": "N/A"}

def send_tg(msg):
    if not TG_TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except: pass

def fetch_okx(instId):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=15m&limit=100"
        res = requests.get(url, timeout=10).json()
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        # 只取已收盤的 K 線 (confirm=="1")
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except: return None

def calculate_atr(df):
    """計算平均真實波幅 (ATR) 用於動態止損"""
    high_low = df['h'] - df['l']
    high_close = np.abs(df['h'] - df['c'].shift())
    low_close = np.abs(df['l'] - df['c'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(window=14).mean().iloc[-1]

def find_smc_setup(df):
    """SMC 結構掃描：FVG + 結構突破 (BOS/CHoCH)"""
    if df is None or len(df) < 40: return None
    atr = calculate_atr(df)
    
    # 掃描最近 25 根 K 線尋找進場機會
    for i in range(len(df)-3, len(df)-25, -1):
        k0, k1, k2 = df.iloc[i-1], df.iloc[i], df.iloc[i+1]
        
        # 多頭條件：K2 突破前 15 根高點 且 K2 為陽線 (BOS)
        if k2['c'] > k2['o'] and k2['c'] > df['h'].iloc[i-15:i].max():
            # 進場位取 FVG 缺口或 K 線中點 (較激進)
            entry = (k2['l'] + k0['h']) / 2 if k2['l'] > k0['h'] else (k1['l'] + k1['o']) / 2
            sl = k1['l'] - (0.4 * atr) # 動態 ATR 止損
            return {"side": "LONG", "entry": entry, "sl": sl}
            
        # 空頭條件：K2 跌破前 15 根低點 且 K2 為陰線 (BOS)
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

        # 檔案初始化
        for f, cols in zip([LOG_FILE, STATS_FILE], [log_cols, stats_cols]):
            if not os.path.exists(f) or os.stat(f).st_size == 0:
                pd.DataFrame(columns=cols).to_csv(f, index=False)

        # 🌙 A. 戰績回報 (午夜 00:00 或 手動觸發)
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
                    report_msg += f"──────────────────\n"
                    report_msg += f"✅ 盈：{tp_c} | ❌ 損：{sl_c}\n"
                    report_msg += f"🔥 勝率：*{wr:.1f}%*\n"
                    report_msg += f"🕒 統計時間：{now_tw.strftime('%Y-%m-%d %H:%M')}"
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

            # 1. 發現新機會
            if instId not in active_ids:
                setup = find_smc_setup(df)
                if setup:
                    risk = abs(setup['entry'] - setup['sl'])
                    tp1 = setup['entry'] + risk*1.5 if setup['side']=="LONG" else setup['entry'] - risk*1.5
                    tp2 = setup['entry'] + risk*2.0 if setup['side']=="LONG" else setup['entry'] - risk*2.0
                    tp3 = setup['entry'] + risk*3.0 if setup['side']=="LONG" else setup['entry'] - risk*3.0
                    
                    msg = f"🔍 *Alpha Oracle | 發現機會*\n"
                    msg += f"──────────────────\n"
                    msg += f"💎 #{instId.split('-')[0]} | {'🟢 多' if setup['side']=='LONG' else '🔴 空'}\n"
                    msg += f"📊 數據：CVD {m['funding']} | LS {m['ls_ratio']}\n\n"
                    msg += f"📍 進場位：{setup['entry']:.4f}\n"
                    msg += f"🚫 止損位：{setup['sl']:.4f}\n"
                    msg += f"💰 TP1：{tp1:.4f} | TP3：{tp3:.4f}\n\n"
                    msg += f"💡 *等待回踩成交...*"
                    send_tg(msg)
                    updated_trades.append({"instId":instId,"side":setup['side'],"status":"WAITING","entry":setup['entry'],"sl":setup['sl'],"tp1":tp1,"tp2":tp2,"tp3":tp3,"locked":0})
                continue

            # 2. 追蹤現有單據
            t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
            
            # 檢查是否進場成交
            if t['status'] == "WAITING":
                is_hit = (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry'])
                if is_hit:
                    t['status'] = "ACTIVE"
                    msg = f"🚀 *Alpha Oracle | 已成交*\n"
                    msg += f"──────────────────\n"
                    msg += f"✅ #{instId.split('-')[0]} 已觸發進場\n"
                    msg += f"📍 成交價：{curr_p:.4f} | 🛡️ 止損：{t['sl']:.4f}"
                    send_tg(msg)
                updated_trades.append(t)
            
            # 檢查鎖利與結算
            elif t['status'] == "ACTIVE":
                # 觸發 2.0R 鎖利保護 (止損移至保本 TP1 位)
                if t['locked'] == 0 and ((t['side']=="LONG" and curr_p >= t['tp2']) or (t['side']=="SHORT" and curr_p <= t['tp2'])):
                    t['locked'], t['sl'] = 1, t['tp1']
                    send_tg(f"🔒 *Alpha Oracle | 鎖利保護*\n──────────────────\n#{instId.split('-')[0]} 達 2.0R，止損已移至 TP1: {t['tp1']:.4f}")
                
                # 結算判斷 (TP3 或 止損/保本)
                is_sl = (t['side']=="LONG" and curr_p <= t['sl']) or (t['side']=="SHORT" and curr_p >= t['sl'])
                is_tp3 = (t['side']=="LONG" and curr_p >= t['tp3']) or (t['side']=="SHORT" and curr_p <= t['tp3'])
                
                if is_sl or is_tp3:
                    res = "TP" if is_tp3 else "SL"
                    msg = f"🏁 *Alpha Oracle | 交易結算*\n"
                    msg += f"──────────────────\n"
                    msg += f"#{instId.split('-')[0]} 結算離場\n"
                    msg += f"🏆 結果：{'💰 強力止盈 (3.0R)' if is_tp3 else '🛡️ 保本/止損離場'}\n"
                    msg += f"📍 離場價：{curr_p:.4f}"
                    send_tg(msg)
                    # 寫入統計
                    pd.DataFrame([{"instId":instId,"result":res}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                    continue
                updated_trades.append(t)

        # 儲存當前狀態，供下次 Actions 讀取
        pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)
    except:
        traceback.print_exc()

if __name__ == "__main__":
    main()
