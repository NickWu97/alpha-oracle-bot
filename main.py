import requests
import os
import pandas as pd
import numpy as np
import logging
import traceback
import time
from datetime import datetime, timedelta

# --- 1. 基礎配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 環境變數調用
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
MANUAL_REPORT = os.getenv("MANUAL_REPORT", "false").lower() == "true"

# 監控幣種 (OKX 永續合約)
ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP", 
    "BCH-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP"
]

LOG_FILE = "active_trades.csv"
STATS_FILE = "daily_stats.csv"

# --- 2. 工具函數 ---

def send_tg(msg):
    """發送 Telegram 訊息"""
    if not TG_TOKEN or not CHAT_ID:
        logging.warning("TG Token 或 Chat ID 未設置，跳過發送。")
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        logging.error(f"TG 發送失敗: {e}")

def fetch_okx_candles(instId, bar='15m', limit=100):
    """獲取 K 線數據"""
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if res.get('code') != '0': return None
        
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        # 轉為時間正序 (舊 -> 新)
        return df.iloc[::-1].reset_index(drop=True)
    except:
        return None

def get_market_sentiment(instId):
    """獲取 CVD 與 LS Ratio 情緒數據"""
    try:
        base_ccy = instId.split("-")[0]
        spot_instId = f"{base_ccy}-USDT"
        
        # 1. 現貨近期成交流向 (模擬 CVD)
        trades = requests.get(f"https://www.okx.com/api/v5/market/trades?instId={spot_instId}&limit=100", timeout=5).json()
        cvd_val = sum([float(t['sz']) if t['side'] == 'buy' else -float(t['sz']) for t in trades['data']])
        cvd_trend = "UP" if cvd_val > 0 else "DOWN"

        # 2. 資金費率
        f_res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5).json()
        funding = float(f_res['data'][0]['fundingRate'])
        
        # 3. 多空人數持倉比
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_ccy}", timeout=5).json()
        ls_ratio = float(ls_res['data'][0]['ratio'])

        return {"cvd": cvd_trend, "funding": funding, "ls_ratio": ls_ratio}
    except:
        return None

def analyze_trade_type(df_15m):
    """基於波動率與均線判斷交易性質"""
    try:
        ema20 = df_15m['c'].tail(20).mean()
        curr_p = df_15m['c'].iloc[-1]
        
        # 判斷是否遠離均線 (偏向趨勢單)
        if abs(curr_p - ema20) / ema20 > 0.015:
            return "📈 長單 (Trend)", "3x - 5x"
        return "⚡ 短單 (Scalp)", "10x - 15x"
    except:
        return "⚡ 短單 (Scalp)", "10x"

# --- 3. 核心策略邏輯 ---

def find_smc_setup(df, sentiment):
    """SMC 結構識別 + FVG 檢測"""
    if df is None or len(df) < 40 or sentiment is None: return None
    
    # 最近 40 根 K 線的高低點
    swing_h, swing_l = df['h'].max(), df['l'].min()
    
    # 檢查倒數第 2 根 K 線是否有 FVG (Fair Value Gap)
    # 多頭 FVG: K(-3)的高點 < K(-1)的低點
    k0, k1, k2 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    
    # 多頭進場條件
    if k2['l'] > k0['h'] and k2['c'] > k2['o']:
        if sentiment['cvd'] == "UP" and sentiment['ls_ratio'] < 1.4:
            entry = (k2['l'] + k0['h']) / 2
            sl = min(k0['l'], k1['l'])
            risk = entry - sl
            if risk > 0:
                return {"side": "LONG", "entry": entry, "sl": sl, "tp1": entry + risk*1.5, "tp2": swing_h, "tp3": swing_h + risk}

    # 空頭進場條件
    if k2['h'] < k0['l'] and k2['c'] < k2['o']:
        if sentiment['cvd'] == "DOWN" and sentiment['ls_ratio'] > 0.8:
            entry = (k2['h'] + k0['l']) / 2
            sl = max(k0['h'], k1['h'])
            risk = sl - entry
            if risk > 0:
                return {"side": "SHORT", "entry": entry, "sl": sl, "tp1": entry - risk*1.5, "tp2": swing_l, "tp3": swing_l - risk}
                
    return None

# --- 4. 戰績匯報 ---

def send_win_rate_report(is_manual=False):
    if not os.path.exists(STATS_FILE): return
    try:
        df_s = pd.read_csv(STATS_FILE)
        if df_s.empty:
            if is_manual: send_tg("📭 目前尚無結算紀錄。")
            return

        tp_c = len(df_s[df_s['result'] == 'TP'])
        sl_c = len(df_s[df_s['result'] == 'SL'])
        total = tp_c + sl_c
        wr = (tp_c / total * 100) if total > 0 else 0
        
        title = "📊 *即時戰報 (手動)*" if is_manual else "🌙 *午夜戰績匯報*"
        msg = (f"{title}\n──────────────────\n"
               f"✅ 獲利/保本 (TP)：{tp_c}\n"
               f"❌ 虧損離場 (SL)：{sl_c}\n"
               f"🔥 總計勝率：*{wr:.1f}%*\n"
               f"──────────────────")
        send_tg(msg)
        if not is_manual: # 清除舊紀錄重新開始
            pd.DataFrame(columns=["instId","result"]).to_csv(STATS_FILE, index=False)
    except: pass

# --- 5. 主循環 ---

def main():
    # 初始化文件
    for f, cols in [(LOG_FILE, ["instId","side","status","entry","sl","tp1","tp2","tp3","locked"]), 
                    (STATS_FILE, ["instId","result"])]:
        if not os.path.exists(f):
            pd.DataFrame(columns=cols).to_csv(f, index=False)

    # 時間判斷 (台北時間 UTC+8)
    now_tw = datetime.utcnow() + timedelta(hours=8)
    
    # 每日凌晨回報
    if now_tw.hour == 0 and 0 <= now_tw.minute < 15:
        if not os.path.exists("midnight.ok"):
            send_win_rate_report(is_manual=False)
            with open("midnight.ok", "w") as f: f.write("ok")
    elif now_tw.hour != 0 and os.path.exists("midnight.ok"):
        os.remove("midnight.ok")

    if MANUAL_REPORT:
        send_win_rate_report(is_manual=True)

    # 讀取現有訂單
    try:
        trades_df = pd.read_csv(LOG_FILE)
    except:
        trades_df = pd.DataFrame(columns=["instId","side","status","entry","sl","tp1","tp2","tp3","locked"])

    active_ids = trades_df['instId'].tolist()
    new_trades = []

    for instId in ALL_COINS:
        df = fetch_okx_candles(instId)
        if df is None or df.empty: continue
        
        curr_p = df['c'].iloc[-1]
        
        # A. 如果該幣種不在監控中 -> 尋找信號
        if instId not in active_ids:
            sentiment = get_market_sentiment(instId)
            setup = find_smc_setup(df, sentiment)
            if setup:
                mode, lev = analyze_trade_type(df)
                msg = (f"🤖 *Alpha Oracle | 策略信號*\n──────────────────\n"
                       f"💎 幣種：#{instId.split('-')[0]} | {mode}\n"
                       f"⚖️ 槓桿：`{lev}` | 方向：{'🟢 多' if setup['side']=='LONG' else '🔴 空'}\n"
                       f"📍 進場：{setup['entry']:.4f}\n"
                       f"🛡️ 止損：{setup['sl']:.4f}\n"
                       f"🎯 TP1：{setup['tp1']:.4f} (保本)\n"
                       f"🎯 TP2：{setup['tp2']:.4f}")
                send_tg(msg)
                setup.update({"instId": instId, "status": "WAITING", "locked": 0})
                new_trades.append(setup)
            continue

        # B. 如果該幣種已在監控中 -> 更新狀態
        t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
        
        if t['status'] == "WAITING":
            # 檢查是否成交
            if (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry']):
                t['status'] = "ACTIVE"
                send_tg(f"🚀 *成交通知*: #{instId.split('-')[0]} 已觸發進場。")
            new_trades.append(t)
            
        elif t['status'] == "ACTIVE":
            # 1. 檢查是否觸發 TP1 移動保本
            if t['locked'] == 0:
                if (t['side']=="LONG" and curr_p >= t['tp1']) or (t['side']=="SHORT" and curr_p <= t['tp1']):
                    t['locked'], t['sl'] = 1, t['entry']
                    send_tg(f"🔒 *保本觸發*: #{instId.split('-')[0]} 已達 TP1，止損移至成本。")
            
            # 2. 檢查結算 (止損或全盈)
            is_sl = (t['side']=="LONG" and curr_p <= t['sl']) or (t['side']=="SHORT" and curr_p >= t['sl'])
            is_tp3 = (t['side']=="LONG" and curr_p >= t['tp3']) or (t['side']=="SHORT" and curr_p <= t['tp3'])
            
            if is_sl or is_tp3:
                res = "TP" if (is_tp3 or t['locked'] == 1) else "SL"
                send_tg(f"🏁 *結算通知*: #{instId.split('-')[0]} 結算結果: {res}")
                pd.DataFrame([{"instId":instId,"result":res}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                # 不加入 new_trades，即從監控名單移除
                continue
            new_trades.append(t)

    # 保存更新後的名單
    pd.DataFrame(new_trades).to_csv(LOG_FILE, index=False)

if __name__ == "__main__":
    main()
