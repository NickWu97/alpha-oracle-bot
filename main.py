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

ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP", 
    "BCH-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP"
]

LOG_FILE = "active_trades.csv"
STATS_FILE = "daily_stats.csv"

# --- 2. 流動性與 SMC 核心邏輯 ---

def get_market_metrics(instId):
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

def fetch_okx(instId):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=15m&limit=100"
        res = requests.get(url, timeout=10).json()
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except: return None

def find_liquidity_setup(df):
    """
    流動性導向邏輯：
    1. 尋找 Swing High/Low (流動性池)
    2. 價格掃過流動性後回撤
    3. FVG (公平價值缺口) 出現作為確認
    """
    if df is None or len(df) < 60: return None
    
    # 定義 HTF 流動性區間 (過去 40 根)
    lookback = 40
    high_liq = df['h'].iloc[-lookback:-5].max()
    low_liq = df['l'].iloc[-lookback:-5].min()
    
    curr_p = df['c'].iloc[-1]
    
    # 檢查最近 8 根 K 線的結構
    for i in range(len(df)-2, len(df)-10, -1):
        k_prev, k_mid, k_next = df.iloc[i-1], df.iloc[i], df.iloc[i+1]
        
        # --- 看漲 W底 (掃低點流動性 + Bullish FVG) ---
        # 條件：最近曾跌破 low_liq 且 k_next 低點高於 k_prev 高點 (FVG 缺口)
        if df['l'].iloc[-10:].min() < low_liq and k_next['l'] > k_prev['h']:
            entry = k_next['l']
            sl = k_mid['l'] # 結構性止損 (OB/FVG 起始)
            tp = df['h'].iloc[-60:].max() # HTF OB 止盈 (前高流動性)
            if entry > sl and tp > entry:
                return {"side": "LONG", "entry": entry, "sl": sl, "tp": tp, "reason": "W-Bottom / Liquidity Sweep"}

        # --- 看跌 M頭 (掃高點流動性 + Bearish FVG) ---
        if df['h'].iloc[-10:].max() > high_liq and k_next['h'] < k_prev['l']:
            entry = k_next['h']
            sl = k_mid['h'] # 結構性止損
            tp = df['l'].iloc[-60:].min() # HTF OB 止盈 (前低流動性)
            if entry < sl and tp < entry:
                return {"side": "SHORT", "entry": entry, "sl": sl, "tp": tp, "reason": "M-Head / Liquidity Sweep"}
                
    return None

# --- 3. 執行與通知系統 ---

def send_tg(msg):
    if not TG_TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try: requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except: pass

def main():
    try:
        now_tw = datetime.utcnow() + timedelta(hours=8)
        manual_report = os.getenv("MANUAL_REPORT", "false").lower() == "true"
        
        # 檔案初始化
        log_cols = ["instId","side","status","entry","sl","tp","locked"]
        for f, cols in [(LOG_FILE, log_cols), (STATS_FILE, ["instId","result"])]:
            if not os.path.exists(f) or os.stat(f).st_size == 0:
                pd.DataFrame(columns=cols).to_csv(f, index=False)

        # 📊 午夜戰績回報
        if (now_tw.hour == 0 and 0 <= now_tw.minute < 15) or manual_report:
            if not os.path.exists("midnight.ok") or manual_report:
                df_s = pd.read_csv(STATS_FILE)
                if not df_s.empty:
                    tp_c, sl_c = len(df_s[df_s['result'] == 'TP']), len(df_s[df_s['result'] == 'SL'])
                    wr = (tp_c / (tp_c + sl_c) * 100) if (tp_c + sl_c) > 0 else 0
                    msg = f"📊 *Alpha Oracle 戰績回報*\n──────────────────\n\n✅ 獲利/保本：{tp_c} 單\n❌ 虧損離場：{sl_c} 單\n🔥 昨日勝率：*{wr:.1f}%*\n\n🕒 統計時間：{now_tw.strftime('%Y-%m-%d')}"
                    send_tg(msg)
                    if not manual_report:
                        pd.DataFrame(columns=["instId","result"]).to_csv(STATS_FILE, index=False)
                        with open("midnight.ok", "w") as f: f.write("ok")
        elif now_tw.hour != 0 and os.path.exists("midnight.ok"): os.remove("midnight.ok")

        # 核心監控邏輯
        try: trades_df = pd.read_csv(LOG_FILE)
        except: trades_df = pd.DataFrame(columns=log_cols)
        
        active_ids = trades_df['instId'].tolist()
        updated_trades = []

        for instId in ALL_COINS:
            df = fetch_okx(instId)
            if df is None or df.empty: continue
            curr_p, m = df['c'].iloc[-1], get_market_metrics(instId)

            if instId not in active_ids:
                setup = find_liquidity_setup(df)
                if setup:
                    msg = f"🔍 *Alpha Oracle | 流動性偵測*\n"
                    msg += f"──────────────────\n\n"
                    msg += f"💎 幣種：#{instId.split('-')[0]}\n"
                    msg += f"💡 邏輯：{setup['reason']}\n"
                    msg += f"🎯 動作：{'🟢 多' if setup['side']=='LONG' else '🔴 空'}\n"
                    msg += f"📊 數據：CVD {m['funding']} | LS {m['ls_ratio']}\n\n"
                    msg += f"📍 進場(FVG)：{setup['entry']:.4f}\n"
                    msg += f"🚫 結構止損：{setup['sl']:.4f}\n"
                    msg += f"💰 目標流動性：{setup['tp']:.4f}\n\n"
                    msg += f"⚠️ *市場正在往高時間級別 OB 移動...*"
                    send_tg(msg)
                    updated_trades.append({"instId":instId,"side":setup['side'],"status":"WAITING","entry":setup['entry'],"sl":setup['sl'],"tp":setup['tp'],"locked":0})
                continue

            # 訂單追蹤邏輯
            t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
            
            # 狀態：等待成交 -> 已成交
            if t['status'] == "WAITING":
                is_hit = (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry'])
                if is_hit:
                    t['status'] = "ACTIVE"
                    send_tg(f"🚀 *Alpha Oracle | 成交提醒*\n──────────────────\n✅ #{instId.split('-')[0]} 已觸發進場\n📍 當前價格：{curr_p:.4f}\n🛡️ 結構止損：{t['sl']:.4f}")
                updated_trades.append(t)
            
            # 狀態：持倉中 -> 鎖利/結算
            elif t['status'] == "ACTIVE":
                # 達 50% 空間自動鎖利保本
                mid_point = (t['entry'] + t['tp']) / 2
                if t['locked'] == 0 and ((t['side']=="LONG" and curr_p >= mid_point) or (t['side']=="SHORT" and curr_p <= mid_point)):
                    t['locked'], t['sl'] = 1, t['entry']
                    send_tg(f"🔒 *Alpha Oracle | 鎖利保護*\n──────────────────\n#{instId.split('-')[0]} 已達 50% 目標\n🛡️ 止損已移至進場價(保本)")
                
                is_sl = (t['side']=="LONG" and curr_p <= t['sl']) or (t['side']=="SHORT" and curr_p >= t['sl'])
                is_tp = (t['side']=="LONG" and curr_p >= t['tp']) or (t['side']=="SHORT" and curr_p <= t['tp'])
                
                if is_sl or is_tp:
                    # 保本或鎖利過後的結算一律計入 TP
                    final_res = "TP" if (is_tp or t['locked'] == 1) else "SL"
                    msg = f"🏁 *Alpha Oracle | 交易結算*\n──────────────────\n"
                    msg += f"#{instId.split('-')[0]} 結算離場\n"
                    msg += f"🏆 結果：{'💰 獲利離場' if is_tp else ('🛡️ 保本離場' if t['locked']==1 else '❌ 止損離場')}\n"
                    msg += f"📍 出場價格：{curr_p:.4f}"
                    send_tg(msg)
                    pd.DataFrame([{"instId":instId,"result":final_res}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                    continue
                updated_trades.append(t)

        pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)
    except: traceback.print_exc()

if __name__ == "__main__": main()
