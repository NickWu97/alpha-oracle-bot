import requests
import os
import pandas as pd
import numpy as np
import logging
import traceback
import time
from datetime import datetime

# --- 0. 環境修正：設定台北時區 ---
os.environ['TZ'] = 'Asia/Taipei'
try:
    time.tzset() 
except:
    pass

# --- 1. 基礎配置與 30 幣種清單 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
LOG_FILE = "active_trades.csv"

# 監控清單
KING_COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP"]
MAJOR_ALTS = ["SUI-USDT-SWAP", "AVAX-USDT-SWAP", "XRP-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP", "OP-USDT-SWAP", "ARB-USDT-SWAP", "NEAR-USDT-SWAP"]
HOT_ALTS = ["PEPE-USDT-SWAP", "WIF-USDT-SWAP", "ORDI-USDT-SWAP", "TON-USDT-SWAP", "FET-USDT-SWAP", "TIA-USDT-SWAP", "PENDLE-USDT-SWAP", "RNDR-USDT-SWAP"]
ALL_MONITOR = KING_COINS + MAJOR_ALTS + HOT_ALTS

# --- 2. 數據獲取與籌碼分析 ---

def fetch_okx_kline(instId, bar='15m', limit='100'):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if 'data' not in res or not res['data']: return None
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        # 過濾未確認的 K 線並轉為正序
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except:
        return None

def get_market_metrics(instId):
    """抓取多空比、資費、CVD 傾向"""
    base_id = instId.replace("-SWAP", "")
    metrics = {"ls_ratio": 1.0, "funding": 0.0, "cvd_bias": "中性"}
    try:
        # 1. 多空持倉人數比
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5).json()
        if 'data' in ls_res and ls_res['data']:
            metrics['ls_ratio'] = float(ls_res['data'][0]['ratio'])
        
        # 2. 資金費率
        fr_res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5).json()
        if 'data' in fr_res and fr_res['data']:
            metrics['funding'] = float(fr_res['data'][0]['fundingRate'])
        
        # 3. CVD 傾向判斷
        if metrics['ls_ratio'] < 0.95: metrics['cvd_bias'] = "🟢 大戶吸籌 (CVD+)"
        elif metrics['ls_ratio'] > 1.20: metrics['cvd_bias'] = "🔴 散戶派發 (CVD-)"
        return metrics
    except:
        return metrics

def calculate_atr(df):
    if len(df) < 15: return 0
    hl, hc, lc = df['h']-df['l'], np.abs(df['h']-df['c'].shift()), np.abs(df['l']-df['c'].shift())
    tr = np.max(pd.concat([hl, hc, lc], axis=1), axis=1)
    return tr.rolling(window=14).mean().iloc[-1]

# --- 3. 核心過濾大腦 ---

def is_high_probability(setup, instId, df_1h):
    score = 0
    now = datetime.now()
    met = get_market_metrics(instId)
    
    # 1. 交易時段過濾 (台北 15-18, 20-24)
    if (15 <= now.hour <= 18) or (20 <= now.hour <= 23): score += 1
    
    # 2. 籌碼面與趨勢過濾
    ema50 = df_1h['c'].ewm(span=50).mean().iloc[-1]
    curr_p = df_1h['c'].iloc[-1]

    if setup['side'] == 'LONG':
        if met['ls_ratio'] < 1.05 and met['funding'] < 0.0003: score += 1 # 散戶不多且資費不貴
        if curr_p > ema50: score += 1 # 順勢
    else:
        if met['ls_ratio'] > 1.10 and met['funding'] > -0.0001: score += 1 # 散戶在多且資費沒負太兇
        if curr_p < ema50: score += 1 # 順勢
       
    return score >= 2, score, met

# --- 4. SMC 策略核心 ---

def find_smc_setup(df, regime_tf):
    if df is None or len(df) < 40: return None
    atr = calculate_atr(df)
    min_r = 2.5 if regime_tf == '1H' else 1.8
    vol_sma = df['v'].rolling(10).mean().iloc[-1]
    
    # 掃描最近 10 根 K 線尋找 BOS 結構
    for i in range(len(df)-2, len(df)-12, -1):
        k0, k1, k2 = df.iloc[i-1], df.iloc[i], df.iloc[i+1]
        
        # 多頭 BOS (價格突破前高 + 成交量放大)
        if k2['c'] > df['h'].iloc[i-15:i].max() and k2['v'] > vol_sma:
            entry = (k2['l'] + k0['h']) / 2 if k2['l'] > k0['h'] else k1['o']
            sl = k1['l'] - (0.5 * atr)
            tp = entry + (abs(entry - sl) * min_r)
            return {"side": "LONG", "entry": entry, "sl": sl, "tp": tp, "r_ratio": round(min_r, 2)}
            
        # 空頭 BOS (價格跌破前低 + 成交量放大)
        if k2['c'] < df['l'].iloc[i-15:i].min() and k2['v'] > vol_sma:
            entry = (k2['h'] + k0['l']) / 2 if k2['h'] < k0['l'] else k1['o']
            sl = k1['h'] + (0.5 * atr)
            tp = entry - (abs(sl - entry) * min_r)
            return {"side": "SHORT", "entry": entry, "sl": sl, "tp": tp, "r_ratio": round(min_r, 2)}
    return None

# --- 5. 主程序與發送 ---

def send_tg(msg):
    if not TG_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                      json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except:
        pass

def run_oracle():
    # 強制初始化 CSV 格式
    cols = ["instId","side","status","entry","sl","tp","r_ratio","locked"]
    if not os.path.exists(LOG_FILE):
        pd.DataFrame(columns=cols).to_csv(LOG_FILE, index=False)
    
    try:
        trades_df = pd.read_csv(LOG_FILE)
        # 確保舊檔案也有新欄位
        for c in cols:
            if c not in trades_df.columns: trades_df[c] = None
    except:
        trades_df = pd.DataFrame(columns=cols)
    
    active_ids = trades_df['instId'].tolist() if not trades_df.empty else []
    updated_trades = []

    print(f"[{datetime.now()}] 啟動 30 幣種深度掃描...")
    
    for instId in ALL_MONITOR:
        df_1h = fetch_okx_kline(instId, bar='1H')
        if df_1h is None or len(df_1h) < 50: continue # 數據不足跳過
        
        # 判斷市場環境 (1H EMA 乖離)
        ema50 = df_1h['c'].ewm(span=50).mean().iloc[-1]
        bias = abs(df_1h['c'].iloc[-1] - ema50) / ema50
        regime_tf = '1H' if bias > 0.025 else '15m'
        
        df = fetch_okx_kline(instId, bar=regime_tf)
        if df is None: continue
        
        # A. 新機會尋找
        if instId not in active_ids:
            setup = find_smc_setup(df, regime_tf)
            if setup:
                is_good, score, met = is_high_probability(setup, instId, df_1h)
                if is_good:
                    msg = f"🔍 *Alpha Oracle | SMC 訊號*\n──────────────────\n"
                    msg += f"#{instId.split('-')[0]} [{regime_tf}]\n"
                    msg += f"📊 數據面：\n"
                    msg += f"├ 多空比：{met['ls_ratio']}\n"
                    msg += f"├ 資費：{met['funding']:.4%}\n"
                    msg += f"└ CVD：{met['cvd_bias']}\n\n"
                    msg += f"📍 進場：{setup['entry']:.4f}\n🚫 止損：{setup['sl']:.4f}\n💰 盈虧比：{setup['r_ratio']}R"
                    send_tg(msg)
                    setup.update({"instId": instId, "status": "WAITING", "locked": 0})
                    updated_trades.append(setup)
        else:
            # B. 現有持倉追蹤
            t_rows = trades_df[trades_df['instId'] == instId]
            if t_rows.empty: continue
            t = t_rows.iloc[0].to_dict()
            curr_p = df['c'].iloc[-1]
            
            if t['status'] == "WAITING":
                # 判定成交
                if (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry']):
                    t['status'] = "ACTIVE"
                    send_tg(f"🔔 *成交通知*：#{instId.split('-')[0]} 已觸發進場位！")
                updated_trades.append(t)
            elif t['status'] == "ACTIVE":
                # 判定結算
                is_sl = (curr_p <= t['sl'] if t['side']=="LONG" else curr_p >= t['sl'])
                is_tp = (curr_p >= t['tp'] if t['side']=="LONG" else curr_p <= t['tp'])
                if is_sl or is_tp:
                    res = "💰 獲利 (TP)" if is_tp else "❌ 止損 (SL)"
                    send_tg(f"🏁 *結算通知*：#{instId.split('-')[0]} 結果：{res}")
                else:
                    updated_trades.append(t)

    # 存回更新後的 CSV
    pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)

if __name__ == "__main__":
    try:
        run_oracle()
    except Exception as e:
        print(f"程式崩潰: {e}")
        traceback.print_exc()
    print("掃描任務結束。")
