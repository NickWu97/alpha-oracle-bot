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

# 監控清單：5 隻主流 + 5 隻山寨
MAIN_COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP"]
ALT_COINS = ["SUI-USDT-SWAP", "AVAX-USDT-SWAP", "ADA-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP"]
ALL_MONITOR = MAIN_COINS + ALT_COINS

LOG_FILE = "active_trades.csv"
STATS_FILE = "daily_stats.csv"

# --- 2. 專業數據工具 (CVD/多空比/ATR) ---

def get_advanced_metrics(instId):
    """獲取 OKX 實時數據：資費、多空比、大戶傾向"""
    try:
        base_id = instId.replace("-SWAP", "")
        f_res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5).json()
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5).json()
        ls_ratio = ls_res['data'][0]['ratio']
        cvd_status = "🟢 大戶吸籌" if float(ls_ratio) < 0.95 else "🔴 散戶較多"
        return {"funding": f"{float(f_res['data'][0]['fundingRate'])*100:.4f}%", "ls_ratio": ls_ratio, "cvd": cvd_status}
    except: return {"funding": "N/A", "ls_ratio": "N/A", "cvd": "N/A"}

def calculate_atr(df):
    """計算 ATR 用於 0.4 * ATR 動態止損"""
    high_low = df['h'] - df['l']
    high_close = np.abs(df['h'] - df['c'].shift())
    low_close = np.abs(df['l'] - df['c'].shift())
    true_range = np.max(pd.concat([high_low, high_close, low_close], axis=1), axis=1)
    return true_range.rolling(window=14).mean().iloc[-1]

def fetch_okx(instId):
    """獲取 15m K 線數據"""
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=15m&limit=100"
        res = requests.get(url, timeout=10).json()
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except: return None

# --- 3. SMC 進階核心邏輯：結構 + 折價區 + 成交量 ---

def find_smc_setup(df):
    """偵測 Choch/BOS 並在折價區(多)/溢價區(空)定位進場"""
    if df is None or len(df) < 60: return None
    atr = calculate_atr(df)
    vol_sma = df['v'].rolling(5).mean().iloc[-1]
    
    for i in range(len(df)-3, len(df)-20, -1):
        k0, k1, k2 = df.iloc[i-1], df.iloc[i], df.iloc[i+1]
        
        # 1. 多頭 Choch/BOS + 成交量放大確認
        if k2['c'] > df['h'].iloc[i-15:i].max() and k2['v'] > vol_sma:
            swing_low = df['l'].iloc[i-15:i+1].min()
            swing_high = k2['c']
            equilibrium = (swing_high + swing_low) / 2 # 50% 回調平衡線
            
            # 進場點優化：優先找 FVG 中心，但必須在平衡線(折價區)以下才進
            fvg_mid = (k2['l'] + k0['h']) / 2 if k2['l'] > k0['h'] else k1['o']
            entry = min(fvg_mid, equilibrium) 
            
            sl = k1['l'] - (0.4 * atr) # 0.4 ATR 動態止損
            tp = df['h'].iloc[-60:].max()
            r_val = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) != 0 else 0
            
            # 過濾：如果價格已經飛離進場位 > 2.5%，棄單防止接刀
            if (swing_high - entry) / entry > 0.025: continue

            if entry > sl and tp > entry and r_val >= 1.5:
                return {"side": "LONG", "entry": entry, "sl": sl, "tp": tp, "r_ratio": round(r_val, 2)}

        # 2. 空頭 Choch/BOS + 成交量放大確認
        if k2['c'] < df['l'].iloc[i-15:i].min() and k2['v'] > vol_sma:
            swing_high = df['h'].iloc[i-15:i+1].max()
            swing_low = k2['c']
            equilibrium = (swing_high + swing_low) / 2
            
            fvg_mid = (k2['h'] + k0['l']) / 2 if k2['h'] < k0['l'] else k1['o']
            entry = max(fvg_mid, equilibrium) # 溢價區進場
            
            sl = k1['h'] + (0.4 * atr)
            tp = df['l'].iloc[-60:].min()
            r_val = abs(entry - tp) / abs(sl - entry) if abs(sl - entry) != 0 else 0
            
            if (entry - swing_low) / entry > 0.025: continue

            if entry < sl and tp < entry and r_val >= 1.5:
                return {"side": "SHORT", "entry": entry, "sl": sl, "tp": tp, "r_ratio": round(r_val, 2)}
    return None

# --- 4. 自動化通知與報表執行 ---

def send_tg(msg):
    if not TG_TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try: requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except: pass

def main():
    try:
        now_tw = datetime.utcnow() + timedelta(hours=8)
        manual_mode = os.getenv("REPORT_TYPE", "none")
        
        # 初始化 CSV 資料庫
        log_cols = ["instId","side","status","entry","sl","tp","r_ratio","locked"]
        for f, cols in [(LOG_FILE, log_cols), (STATS_FILE, ["instId","result"])]:
            if not os.path.exists(f) or os.stat(f).st_size == 0:
                pd.DataFrame(columns=cols).to_csv(f, index=False)

        # 🌙 [00:00 戰報] 統計昨日勝率
        if (now_tw.hour == 0 and 0 <= now_tw.minute < 15) or manual_mode == "midnight":
            df_s = pd.read_csv(STATS_FILE)
            if not df_s.empty:
                tp_c, sl_c = len(df_s[df_s['result'] == 'TP']), len(df_s[df_s['result'] == 'SL'])
                wr = (tp_c / (tp_c + sl_c) * 100) if (tp_c + sl_c) > 0 else 0
                msg = f"📊 *Alpha Oracle 戰績回報*\n──────────────────\n✅ 獲利/保本：{tp_c}\n❌ 虧損離場：{sl_c}\n🔥 昨日勝率：*{wr:.1f}%*"
                send_tg(msg)
                if now_tw.hour == 0: pd.DataFrame(columns=["instId","result"]).to_csv(STATS_FILE, index=False)

        # ☕ [08:00 早報] 10 幣巡檢 (含 CVD 與 LS)
        if (now_tw.hour == 8 and 0 <= now_tw.minute < 15) or manual_mode == "morning":
            m_msg = f"☕ *Alpha Oracle 晨間報報*\n──────────────────\n"
            for label, coins in [("💎 主流強勢", MAIN_COINS), ("🚀 山寨潛力", ALT_COINS)]:
                m_msg += f"\n【{label}】\n"
                for inst in coins:
                    df = fetch_okx(inst)
                    if df is not None:
                        curr_p, metrics = df['c'].iloc[-1], get_advanced_metrics(inst)
                        m_msg += f"• #{inst.split('-')[0]}: {curr_p}\n  (LS: {metrics['ls_ratio']} | {metrics['cvd']})\n"
            send_tg(m_msg + "\n💡 *SMC 提醒：只在折價區回調時出手，不追高。*")

        # --- 5. 核心巡邏監控邏輯 ---
        try: trades_df = pd.read_csv(LOG_FILE)
        except: trades_df = pd.DataFrame(columns=log_cols)
        active_ids, updated_trades = trades_df['instId'].tolist(), []

        for instId in ALL_MONITOR:
            df = fetch_okx(instId)
            if df is None: continue
            curr_p, metrics = df['c'].iloc[-1], get_advanced_metrics(instId)

            # A. 發現結構 (掛單等待模式)
            if instId not in active_ids:
                setup = find_smc_setup(df)
                if setup:
                    msg = f"🔍 *Alpha Oracle | SMC 預警*\n──────────────────\n#{instId.split('-')[0]} 結構確認破壞！\n數據：{metrics['cvd']} | LS {metrics['ls_ratio']}\n\n📍 進場區(折價/OB)：{setup['entry']:.4f}\n🚫 止損：{setup['sl']:.4f}\n💰 止盈：{setup['tp']:.4f}\n📈 盈虧比：*{setup['r_ratio']}R*\n\n💡 *提示：結構已完成，等待價格回補區域...*"
                    send_tg(msg)
                    setup.update({"instId": instId, "status": "WAITING", "locked": 0})
                    updated_trades.append(setup)
                continue

            # B. 持倉追蹤：失效判定、回補成交、保本、結算
            t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
            
            if t['status'] == "WAITING":
                # 失效判定：成交前就跌破止損位，代表結構瓦解，撤單
                is_invalid = (t['side']=="LONG" and curr_p <= t['sl']) or (t['side']=="SHORT" and curr_p >= t['sl'])
                if is_invalid:
                    send_tg(f"⚠️ *Alpha Oracle | 撤單*\n#{instId.split('-')[0]} 價格已破壞原結構，掛單失效撤回。")
                    continue 
                
                # 成交判定 (回踩進場位)
                is_hit = (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry'])
                if is_hit:
                    t['status'] = "ACTIVE"
                    send_tg(f"🚀 *Alpha Oracle | 成交提醒*\n#{instId.split('-')[0]} 已成功回補成交！")
                updated_trades.append(t)
                
            elif t['status'] == "ACTIVE":
                # 自動移動止損至保本 (利潤完成 50%)
                mid_p = (t['entry'] + t['tp']) / 2
                if t['locked'] == 0 and ((t['side']=="LONG" and curr_p >= mid_p) or (t['side']=="SHORT" and curr_p <= mid_p)):
                    t['locked'], t['sl'] = 1, t['entry']
                    send_tg(f"🔒 *Alpha Oracle | 鎖利保本*\n#{instId.split('-')[0]} 已達 50% 目標，止損移至開倉價。")
                
                # 結算判斷
                is_sl = (curr_p <= t['sl'] if t['side']=="LONG" else curr_p >= t['sl'])
                is_tp = (curr_p >= t['tp'] if t['side']=="LONG" else curr_p <= t['tp'])
                
                if is_sl or is_tp:
                    final_res = "TP" if (is_tp or t['locked'] == 1) else "SL"
                    send_tg(f"🏁 *Alpha Oracle | 交易結算*\n#{instId.split('-')[0]} 離場，結果：{'💰 獲利/保本' if final_res=='TP' else '❌ 止損'}")
                    pd.DataFrame([{"instId":instId,"result":final_res}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                    continue
                updated_trades.append(t)

        pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)
    except: traceback.print_exc()

if __name__ == "__main__": main()
