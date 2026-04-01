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

# 5 隻主流幣 + 5 隻山寨幣 (早報與巡邏清單)
MAIN_COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP"]
ALT_COINS = ["SUI-USDT-SWAP", "AVAX-USDT-SWAP", "ADA-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP"]
ALL_MONITOR = MAIN_COINS + ALT_COINS

LOG_FILE = "active_trades.csv"
STATS_FILE = "daily_stats.csv"

# --- 2. 專業數據工具 (CVD/多空比/ATR) ---

def get_advanced_metrics(instId):
    """獲取 OKX 實時數據：資費、多空人數比、CVD 傾向"""
    try:
        base_id = instId.replace("-SWAP", "")
        # 資費
        f_res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5).json()
        funding = f"{float(f_res['data'][0]['fundingRate']) * 100:.4f}%"
        # 多空人數持倉比
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5).json()
        ls_ratio = ls_res['data'][0]['ratio']
        # CVD 簡單判定：LS Ratio 低於 0.95 通常暗示大戶在吸籌
        cvd_status = "🟢 大戶收貨" if float(ls_ratio) < 0.95 else "🔴 散戶持倉高"
        return {"funding": funding, "ls_ratio": ls_ratio, "cvd": cvd_status}
    except:
        return {"funding": "N/A", "ls_ratio": "N/A", "cvd": "N/A"}

def calculate_atr(df):
    """計算 ATR 用於 0.4 * ATR 動態止損"""
    high_low = df['h'] - df['l']
    high_close = np.abs(df['h'] - df['c'].shift())
    low_close = np.abs(df['l'] - df['c'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
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

# --- 3. SMC 策略核心：Choch/Bos 偵測與回踩掛單 ---

def find_smc_setup(df):
    """尋找結構破壞 (Choch/BOS) 並定位回踩進場區間 (FVG/OB)"""
    if df is None or len(df) < 60: return None
    atr = calculate_atr(df)
    
    # 掃描 K 線尋找最近的結構突破
    for i in range(len(df)-3, len(df)-20, -1):
        k0, k1, k2 = df.iloc[i-1], df.iloc[i], df.iloc[i+1] # 前、中(突破起始)、後(確認突破)
        
        # --- 多頭結構破壞 (Choch / BOS) ---
        # 條件：收盤強勢突破前 15 根 K 線的高點
        if k2['c'] > df['h'].iloc[i-15:i].max() and k2['c'] > k2['o']:
            # 進場位定位在 FVG (公平價值缺口) 中軸或 OB (訂單塊) 邊緣
            entry = (k2['l'] + k0['h']) / 2 if k2['l'] > k0['h'] else k1['o']
            sl = k1['l'] - (0.4 * atr) # 止損設在結構下方並加上 0.4 ATR 緩衝
            tp = df['h'].iloc[-60:].max() # 目標定位在前高流動性池
            r_val = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) != 0 else 0
            
            # 只有 R 值大於 1.5 且方向正確才觸發
            if entry > sl and tp > entry and r_val >= 1.5:
                return {"side": "LONG", "entry": entry, "sl": sl, "tp": tp, "r_ratio": round(r_val, 2)}

        # --- 空頭結構破壞 (Choch / BOS) ---
        # 條件：收盤強勢跌破前 15 根 K 線的低點
        if k2['c'] < df['l'].iloc[i-15:i].min() and k2['c'] < k2['o']:
            entry = (k2['h'] + k0['l']) / 2 if k2['h'] < k0['l'] else k1['o']
            sl = k1['h'] + (0.4 * atr)
            tp = df['l'].iloc[-60:].min() # 目標定位在前低流動性池
            r_val = abs(entry - tp) / abs(sl - entry) if abs(sl - entry) != 0 else 0
            
            if entry < sl and tp < entry and r_val >= 1.5:
                return {"side": "SHORT", "entry": entry, "sl": sl, "tp": tp, "r_ratio": round(r_val, 2)}
    return None

# --- 4. 報表、通知與執行系統 ---

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

        # 🌙 [00:00 戰報] 統計昨日勝率 (保本結算算作 TP)
        if (now_tw.hour == 0 and 0 <= now_tw.minute < 15) or manual_mode == "midnight":
            df_s = pd.read_csv(STATS_FILE)
            if not df_s.empty:
                tp_c, sl_c = len(df_s[df_s['result'] == 'TP']), len(df_s[df_s['result'] == 'SL'])
                wr = (tp_c / (tp_c + sl_c) * 100) if (tp_c + sl_c) > 0 else 0
                msg = f"📊 *Alpha Oracle 戰績回報*\n──────────────────\n✅ 獲利/保本：{tp_c}\n❌ 虧損離場：{sl_c}\n🔥 昨日勝率：*{wr:.1f}%*"
                send_tg(msg)
                if now_tw.hour == 0: pd.DataFrame(columns=["instId","result"]).to_csv(STATS_FILE, index=False)

        # ☕ [08:00 早報] 5 主流 + 5 山寨 (含 CVD 與 LS)
        if (now_tw.hour == 8 and 0 <= now_tw.minute < 15) or manual_mode == "morning":
            m_msg = f"☕ *Alpha Oracle 晨間報報*\n──────────────────\n"
            for label, coins in [("💎 主流強勢", MAIN_COINS), ("🚀 山寨潛力", ALT_COINS)]:
                m_msg += f"\n【{label}】\n"
                for inst in coins:
                    df = fetch_okx(inst)
                    if df is not None:
                        curr_p, metrics = df['c'].iloc[-1], get_advanced_metrics(inst)
                        m_msg += f"• #{inst.split('-')[0]}: {curr_p}\n  (LS: {metrics['ls_ratio']} | {metrics['cvd']})\n"
            send_tg(m_msg + "\n💡 *SMC 提醒：尋找結構破壞後的回踩，不追高買入。*")

        # 5. 核心交易巡邏
        try: trades_df = pd.read_csv(LOG_FILE)
        except: trades_df = pd.DataFrame(columns=log_cols)
        active_ids, updated_trades = trades_df['instId'].tolist(), []

        for instId in ALL_MONITOR:
            df = fetch_okx(instId)
            if df is None: continue
            curr_p, metrics = df['c'].iloc[-1], get_advanced_metrics(instId)

            # A. 發現結構並「預備掛單」
            if instId not in active_ids:
                setup = find_smc_setup(df)
                if setup:
                    msg = f"🔍 *Alpha Oracle | SMC 訊號*\n──────────────────\n💎 幣種：#{instId.split('-')[0]}\n🎯 動作：{'🟢 多' if setup['side']=='LONG' else '🔴 空'}\n"
                    msg += f"📊 數據：{metrics['cvd']} | LS {metrics['ls_ratio']}\n\n"
                    msg += f"📍 掛單區(OB/FVG)：{setup['entry']:.4f}\n🚫 止損：{setup['sl']:.4f}\n💰 止盈：{setup['tp']:.4f}\n📈 R值：*{setup['r_ratio']}R*\n\n💡 *提示：結構已破壞，等待價格回踩區域成交...*"
                    send_tg(msg)
                    setup.update({"instId": instId, "status": "WAITING", "locked": 0})
                    updated_trades.append(setup)
                continue

            # B. 持倉追蹤 (回踩確認與自動鎖利)
            t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
            
            if t['status'] == "WAITING":
                # 判斷是否回踩成功 (多單價格跌回 entry, 空單價格漲回 entry)
                is_hit = (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry'])
                if is_hit:
                    t['status'] = "ACTIVE"
                    send_tg(f"🚀 *Alpha Oracle | 成交提醒*\n#{instId.split('-')[0]} 已回踩成交！\n當前 R 值：{t['r_ratio']}")
                updated_trades.append(t)
                
            elif t['status'] == "ACTIVE":
                # 鎖利保本點 (路程完成 50%)
                mid_p = (t['entry'] + t['tp']) / 2
                if t['locked'] == 0 and ((t['side']=="LONG" and curr_p >= mid_p) or (t['side']=="SHORT" and curr_p <= mid_p)):
                    t['locked'], t['sl'] = 1, t['entry']
                    send_tg(f"🔒 *Alpha Oracle | 鎖利保護*\n#{instId.split('-')[0]} 已保本，風險歸零。")
                
                # 結算判斷
                is_sl = (curr_p <= t['sl'] if t['side']=="LONG" else curr_p >= t['sl'])
                is_tp = (curr_p >= t['tp'] if t['side']=="LONG" else curr_p <= t['tp'])
                
                if is_sl or is_tp:
                    final_res = "TP" if (is_tp or t['locked'] == 1) else "SL"
                    msg = f"🏁 *Alpha Oracle | 交易結算*\n──────────────────\n#{instId.split('-')[0]} 離場\n🏆 結果：{'💰 獲利/保本離場' if final_res=='TP' else '❌ 止損離場'}"
                    send_tg(msg)
                    pd.DataFrame([{"instId":instId,"result":final_res}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                    continue
                updated_trades.append(t)

        pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)
    except: traceback.print_exc()

if __name__ == "__main__": main()
