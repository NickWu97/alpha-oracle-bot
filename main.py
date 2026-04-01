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

# --- 2. 核心數據：CVD、資費與多空比 (背離過濾) ---

def get_market_sentiment(instId):
    """
    獲取背離指標：
    1. 現貨 CVD (大戶資金流)
    2. 多空人數持倉比 (散戶情緒)
    3. 資費 (持倉成本)
    """
    try:
        base_ccy = instId.split("-")[0]
        spot_instId = f"{base_ccy}-USDT"
        
        # 抓取現貨成交明細計算 CVD
        trades = requests.get(f"https://www.okx.com/api/v5/market/trades?instId={spot_instId}&limit=100", timeout=5).json()
        cvd_val = sum([float(t['sz']) if t['side'] == 'buy' else -float(t['sz']) for t in trades['data']])
        cvd_trend = "UP" if cvd_val > 0 else "DOWN"

        # 資費
        f_res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5).json()
        funding = float(f_res['data'][0]['fundingRate'])
        
        # 多空人數持倉比
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_ccy}", timeout=5).json()
        ls_ratio = float(ls_res['data'][0]['ratio'])

        return {"cvd": cvd_trend, "funding": funding, "ls_ratio": ls_ratio, "cvd_raw": cvd_val}
    except:
        return None

# --- 3. 長短單分析與槓桿建議 ---

def analyze_trade_type(instId):
    """自動判定長短單並給予槓桿建議"""
    try:
        url_1h = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=1H&limit=50"
        res_1h = requests.get(url_1h).json()
        df_1h = pd.DataFrame(res_1h['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df_1h['c'] = df_1h['c'].astype(float)
        
        ema20 = df_1h['c'].tail(20).mean()
        curr_p = df_1h['c'].iloc[0]
        
        # 趨勢強度判定
        if (curr_p > ema20 * 1.015) or (curr_p < ema20 * 0.985):
            return "📈 長單 (Trend)", "3x - 5x"
        else:
            return "⚡ 短單 (Scalp)", "10x - 15x"
    except:
        return "⚡ 短單 (Scalp)", "10x"

# --- 4. SMC 結構識別核心 ---

def find_smc_setup(df, sentiment):
    if df is None or len(df) < 40 or sentiment is None: return None
    
    swing_h, swing_l = df['h'].tail(40).max(), df['l'].tail(40).min()
    
    for i in range(len(df)-3, len(df)-20, -1):
        k0, k1, k2 = df.iloc[i-1], df.iloc[i], df.iloc[i+1]
        
        # --- 多頭條件：SMC 看多 + 現貨 CVD UP + 散戶看空(LS低) ---
        is_bull_fvg = k2['l'] > k0['h']
        is_choch = k2['c'] > df['h'].iloc[i-12:i].max()
        
        if k2['c'] > k2['o'] and (is_choch or is_bull_fvg):
            if sentiment['cvd'] == "UP" and (sentiment['ls_ratio'] < 1.3 or sentiment['funding'] < 0.0001):
                entry = (k2['l'] + k0['h']) / 2 if is_bull_fvg else k1['l']
                sl = k0['l'] if is_bull_fvg else df['l'].iloc[i-5:i].min()
                risk = entry - sl
                if risk <= 0: continue
                return {"side": "LONG", "entry": entry, "sl": sl, "tp1": entry + risk*1.5, "tp2": swing_h, "tp3": swing_h + risk}

        # --- 空頭條件：SMC 看空 + 現貨 CVD DOWN + 散戶看多(LS高) ---
        is_bear_fvg = k2['h'] < k0['l']
        is_bear_choch = k2['c'] < df['l'].iloc[i-12:i].min()

        if k2['c'] < k2['o'] and (is_bear_choch or is_bear_fvg):
            if sentiment['cvd'] == "DOWN" and (sentiment['ls_ratio'] > 1.3 or sentiment['funding'] > 0.0001):
                entry = (k2['h'] + k0['l']) / 2 if is_bear_fvg else k1['h']
                sl = k0['h'] if is_bear_fvg else df['h'].iloc[i-5:i].max()
                risk = sl - entry
                if risk <= 0: continue
                return {"side": "SHORT", "entry": entry, "sl": sl, "tp1": entry - risk*1.5, "tp2": swing_l, "tp3": swing_l - risk}
    return None

# --- 5. 主程式邏輯 ---

def main():
    try:
        now_tw = datetime.utcnow() + timedelta(hours=8)
        manual_report = os.getenv("MANUAL_REPORT", "false").lower() == "true"
        log_cols = ["instId","side","status","entry","sl","tp1","tp2","tp3","locked"]
        
        # 檔案初始化
        for f, cols in zip([LOG_FILE, STATS_FILE], [log_cols, ["instId","result"]]):
            if not os.path.exists(f) or os.stat(f).st_size == 0:
                pd.DataFrame(columns=cols).to_csv(f, index=False)

        # A. 報表回報邏輯
        if (now_tw.hour == 0 and 0 <= now_tw.minute < 15) or manual_report:
            if not os.path.exists("midnight.ok") or manual_report:
                df_s = pd.read_csv(STATS_FILE)
                if not df_s.empty:
                    tp_c, sl_c = len(df_s[df_s['result'] == 'TP']), len(df_s[df_s['result'] == 'SL'])
                    wr = (tp_c / (tp_c + sl_c) * 100) if (tp_c + sl_c) > 0 else 0
                    msg = f"📊 *Alpha Oracle 戰績報表*\n──────────────────\n✅ 獲利/保本：{tp_c}\n❌ 虧損離場：{sl_c}\n🔥 昨日勝率：*{wr:.1f}%*"
                    send_tg(msg)
                    if not manual_report:
                        pd.DataFrame(columns=["instId","result"]).to_csv(STATS_FILE, index=False)
                        with open("midnight.ok", "w") as f: f.write("ok")
        elif now_tw.hour != 0 and os.path.exists("midnight.ok"):
            os.remove("midnight.ok")

        # B. 監控與掃描
        try: trades_df = pd.read_csv(LOG_FILE)
        except: trades_df = pd.DataFrame(columns=log_cols)
        
        active_ids = trades_df['instId'].tolist()
        updated_trades = []

        for instId in ALL_COINS:
            df = fetch_okx(instId)
            if df is None or df.empty: continue
            
            sentiment = get_market_sentiment(instId)
            curr_p = df['c'].iloc[-1]

            # 1. 發現機會
            if instId not in active_ids:
                setup = find_smc_setup(df, sentiment)
                if setup:
                    trade_mode, lev_suggest = analyze_trade_type(instId)
                    msg = (f"🤖 *Alpha Oracle | 終極融合分析*\n"
                           f"──────────────────\n"
                           f"💎 幣種：#{instId.split('-')[0]}\n"
                           f"🏷️ 模式：{trade_mode}\n"
                           f"⚖️ 建議槓桿：`{lev_suggest}`\n"
                           f"🎯 動作：{'🟢 多 (LONG)' if setup['side']=='LONG' else '🔴 空 (SHORT)'}\n"
                           f"🔥 數據：CVD {sentiment['cvd']} | LS {sentiment['ls_ratio']}\n\n"
                           f"📍 進場參考：{setup['entry']:.4f}\n"
                           f"🛡️ 結構止損：{setup['sl']:.4f}\n"
                           f"💰 TP1 (保本): {setup['tp1']:.4f}\n"
                           f"💰 TP2 (目標): {setup['tp2']:.4f}\n"
                           f"💰 TP3 (擴展): {setup['tp3']:.4f}\n\n"
                           f"⚠️ *注意：達 TP1 後將自動移至保本位*")
                    send_tg(msg)
                    setup.update({"instId": instId, "status": "WAITING", "locked": 0})
                    updated_trades.append(setup)
                continue

            # 2. 追蹤訂單
            t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
            if t['status'] == "WAITING":
                is_hit = (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry'])
                if is_hit:
                    t['status'] = "ACTIVE"
                    send_tg(f"🚀 *成交提醒*: #{instId.split('-')[0]} 已觸發成交點位。")
                updated_trades.append(t)
            elif t['status'] == "ACTIVE":
                # TP1 保本鎖利
                if t['locked'] == 0 and ((t['side']=="LONG" and curr_p >= t['tp1']) or (t['side']=="SHORT" and curr_p <= t['tp1'])):
                    t['locked'], t['sl'] = 1, t['entry']
                    send_tg(f"🔒 *鎖利*: #{instId.split('-')[0]} 達 TP1，止損已移至進場位保本。")
                
                is_sl = (t['side']=="LONG" and curr_p <= t['sl']) or (t['side']=="SHORT" and curr_p >= t['sl'])
                is_tp3 = (t['side']=="LONG" and curr_p >= t['tp3']) or (t['side']=="SHORT" and curr_p <= t['tp3'])
                
                if is_sl or is_tp3:
                    res = "TP" if (is_tp3 or t['locked'] == 1) else "SL"
                    msg = f"🏁 *結算*: #{instId.split('-')[0]} | 結果: {res}\n離場價格: {curr_p:.4f}"
                    send_tg(msg)
                    pd.DataFrame([{"instId":instId,"result":res}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                    continue
                updated_trades.append(t)

        pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)
    except: traceback.print_exc()

def fetch_okx(instId):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=15m&limit=100"
        res = requests.get(url, timeout=10).json()
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except: return None

def send_tg(msg):
    if not TG_TOKEN or not CHAT_ID: return
    try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                      json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except: pass

if __name__ == "__main__":
    main()
