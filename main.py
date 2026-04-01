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
# 接收 YAML 傳入的手動觸發指令
MANUAL_REPORT = os.getenv("MANUAL_REPORT", "false").lower() == "true"

# 監控幣種 (OKX 永續合約)
ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP", 
    "BCH-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP"
]

LOG_FILE = "active_trades.csv"
STATS_FILE = "daily_stats.csv"

# --- 2. 數據獲取與情緒分析 (CVD / LS Ratio) ---

def get_market_sentiment(instId):
    """獲取大戶與散戶的背離數據"""
    try:
        base_ccy = instId.split("-")[0]
        spot_instId = f"{base_ccy}-USDT"
        
        # 1. 現貨 CVD (大戶資金流向)
        trades = requests.get(f"https://www.okx.com/api/v5/market/trades?instId={spot_instId}&limit=100", timeout=5).json()
        cvd_val = sum([float(t['sz']) if t['side'] == 'buy' else -float(t['sz']) for t in trades['data']])
        cvd_trend = "UP" if cvd_val > 0 else "DOWN"

        # 2. 資金費率
        f_res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5).json()
        funding = float(f_res['data'][0]['fundingRate'])
        
        # 3. 多空人數持倉比 (散戶情緒)
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_ccy}", timeout=5).json()
        ls_ratio = float(ls_res['data'][0]['ratio'])

        return {"cvd": cvd_trend, "funding": funding, "ls_ratio": ls_ratio}
    except:
        return None

def analyze_trade_type(instId):
    """自動分析長短單與槓桿"""
    try:
        url_1h = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=1H&limit=50"
        res_1h = requests.get(url_1h).json()
        df_1h = pd.DataFrame(res_1h['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df_1h['c'] = df_1h['c'].astype(float)
        ema20 = df_1h['c'].tail(20).mean()
        curr_p = df_1h['c'].iloc[0]
        
        if (curr_p > ema20 * 1.015) or (curr_p < ema20 * 0.985):
            return "📈 長單 (Trend)", "3x - 5x"
        return "⚡ 短單 (Scalp)", "10x - 15x"
    except:
        return "⚡ 短單 (Scalp)", "10x"

# --- 3. SMC 核心策略 ---

def find_smc_setup(df, sentiment):
    if df is None or len(df) < 40 or sentiment is None: return None
    swing_h, swing_l = df['h'].tail(40).max(), df['l'].tail(40).min()
    
    for i in range(len(df)-3, len(df)-20, -1):
        k0, k1, k2 = df.iloc[i-1], df.iloc[i], df.iloc[i+1]
        
        # 多頭條件：SMC 結構 + 現貨 CVD 買入 + 散戶看空(LS低)
        if (k2['l'] > k0['h'] or k2['c'] > df['h'].iloc[i-12:i].max()) and k2['c'] > k2['o']:
            if sentiment['cvd'] == "UP" and (sentiment['ls_ratio'] < 1.3 or sentiment['funding'] < 0.0001):
                entry, sl = (k2['l'] + k0['h']) / 2, k0['l']
                risk = entry - sl
                if risk <= 0: continue
                return {"side": "LONG", "entry": entry, "sl": sl, "tp1": entry + risk*1.5, "tp2": swing_h, "tp3": swing_h + risk}

        # 空頭條件：SMC 結構 + 現貨 CVD 賣出 + 散戶看多(LS高)
        if (k2['h'] < k0['l'] or k2['c'] < df['l'].iloc[i-12:i].min()) and k2['c'] < k2['o']:
            if sentiment['cvd'] == "DOWN" and (sentiment['ls_ratio'] > 1.3 or sentiment['funding'] > 0.0001):
                entry, sl = (k2['h'] + k0['l']) / 2, k1['h']
                risk = sl - entry
                if risk <= 0: continue
                return {"side": "SHORT", "entry": entry, "sl": sl, "tp1": entry - risk*1.5, "tp2": swing_l, "tp3": swing_l - risk}
    return None

# --- 4. 戰績回報邏輯 ---

def send_win_rate_report(is_manual=False):
    if not os.path.exists(STATS_FILE): return
    df_s = pd.read_csv(STATS_FILE)
    if df_s.empty:
        if is_manual: send_tg("📭 目前尚無結算交易紀錄。")
        return

    tp_c = len(df_s[df_s['result'] == 'TP'])
    sl_c = len(df_s[df_s['result'] == 'SL'])
    wr = (tp_c / (tp_c + sl_c) * 100) if (tp_c + sl_c) > 0 else 0
    
    title = "📊 *即時戰報 (手動查詢)*" if is_manual else "🌙 *午夜戰績自動匯報*"
    msg = (f"{title}\n──────────────────\n"
           f"✅ 獲利/保本 (TP)：{tp_c}\n"
           f"❌ 虧損離場 (SL)：{sl_c}\n"
           f"🔥 勝率：*{wr:.1f}%*\n"
           f"──────────────────\n"
           f"💡 *註：只要達到 TP1 觸發保本皆計入 TP*")
    send_tg(msg)
    if not is_manual:
        pd.DataFrame(columns=["instId","result"]).to_csv(STATS_FILE, index=False)

# --- 5. 主程式流程 ---

def main():
    try:
        now_tw = datetime.utcnow() + timedelta(hours=8)
        
        # 初始化 CSV
        for f, c in zip([LOG_FILE, STATS_FILE], [["instId","side","status","entry","sl","tp1","tp2","tp3","locked"], ["instId","result"]]):
            if not os.path.exists(f) or os.stat(f).st_size == 0:
                pd.DataFrame(columns=c).to_csv(f, index=False)

        # 報表處理
        if (now_tw.hour == 0 and 0 <= now_tw.minute < 15):
            if not os.path.exists("midnight.ok"):
                send_win_rate_report(is_manual=False)
                with open("midnight.ok", "w") as f: f.write("ok")
        elif MANUAL_REPORT:
            send_win_rate_report(is_manual=True)
        
        if now_tw.hour != 0 and os.path.exists("midnight.ok"):
            os.remove("midnight.ok")

        # 監控交易
        try: trades_df = pd.read_csv(LOG_FILE)
        except: trades_df = pd.DataFrame(columns=["instId","side","status","entry","sl","tp1","tp2","tp3","locked"])
        
        active_ids = trades_df['instId'].tolist()
        updated_trades = []

        for instId in ALL_COINS:
            df = fetch_okx(instId)
            if df is None or df.empty: continue
            sentiment = get_market_sentiment(instId)
            curr_p = df['c'].iloc[-1]

            if instId not in active_ids:
                setup = find_smc_setup(df, sentiment)
                if setup:
                    mode, lev = analyze_trade_type(instId)
                    msg = (f"🤖 *Alpha Oracle | 策略報單*\n──────────────────\n"
                           f"💎 幣種：#{instId.split('-')[0]} | {mode}\n"
                           f"⚖️ 槓桿：`{lev}` | 動作：{'🟢 多' if setup['side']=='LONG' else '🔴 空'}\n"
                           f"🔥 數據：CVD {sentiment['cvd']} | LS {sentiment['ls_ratio']}\n\n"
                           f"📍 進場點：{setup['entry']:.4f}\n"
                           f"🛡️ 止損點：{setup['sl']:.4f}\n"
                           f"💰 TP1 (保本)：{setup['tp1']:.4f}\n"
                           f"💰 TP2 (目標)：{setup['tp2']:.4f}\n"
                           f"💰 TP3 (極限)：{setup['tp3']:.4f}")
                    send_tg(msg)
                    setup.update({"instId": instId, "status": "WAITING", "locked": 0})
                    updated_trades.append(setup)
                continue

            t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
            if t['status'] == "WAITING":
                if (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry']):
                    t['status'] = "ACTIVE"
                    send_tg(f"🚀 *成交*: #{instId.split('-')[0]} 已觸發進場位。")
                updated_trades.append(t)
            elif t['status'] == "ACTIVE":
                if t['locked'] == 0 and ((t['side']=="LONG" and curr_p >= t['tp1']) or (t['side']=="SHORT" and curr_p <= t['tp1'])):
                    t['locked'], t['sl'] = 1, t['entry']
                    send_tg(f"🔒 *鎖利*: #{instId.split('-')[0]} 達 TP1，止損移至進場位。")
                
                is_sl = (t['side']=="LONG" and curr_p <= t['sl']) or (t['side']=="SHORT" and curr_p >= t['sl'])
                is_tp3 = (t['side']=="LONG" and curr_p >= t['tp3']) or (t['side']=="SHORT" and curr_p <= t['tp3'])
                if is_sl or is_tp3:
                    res = "TP" if (is_tp3 or t['locked'] == 1) else "SL"
                    send_tg(f"🏁 *結算*: #{instId.split('-')[0]} 結果: {res}")
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
    try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except: pass

if __name__ == "__main__":
    main()
