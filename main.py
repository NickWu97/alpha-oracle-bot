import requests
import os
import random
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# 1. 系統設定 (請確保 GitHub Secrets 已設定)
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 2. 監控池
WATCHLIST = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "ORDI-USDT-SWAP", "PEPE-USDT-SWAP", "WIF-USDT-SWAP", "FET-USDT-SWAP"
]

def fetch_smc_analysis(instId):
    try:
        base = instId.split('-')[0]
        # --- 抓取 15m K線 (SMC 核心分析) ---
        c_url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=15m&limit=100"
        raw = requests.get(c_url, timeout=10).json()['data']
        df = pd.DataFrame(raw, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'volCcy', 'volCcyQuote', 'confirm'])
        df[['o', 'h', 'l', 'c', 'v']] = df[['o', 'h', 'l', 'c', 'v']].astype(float)
        df = df.iloc[::-1].reset_index(drop=True)
        
        curr_p = df['c'].iloc[-1]

        # --- SMC 結構計算 ---
        # 尋找局部高低點
        df['hi_max'] = df['h'].rolling(window=5, center=True).max()
        df['lo_min'] = df['l'].rolling(window=5, center=True).min()
        
        # 市場結構轉變 (CHoCH)
        last_hi = df[df['h'] == df['hi_max']]['h'].iloc[-2]
        last_lo = df[df['l'] == df['lo_min']]['l'].iloc[-2]
        
        is_choch_bull = curr_p > last_hi
        is_choch_bear = curr_p < last_lo

        # 尋找 FVG (Fair Value Gap)
        fvg_bull = None
        if df['l'].iloc[-1] > df['h'].iloc[-3]:
            fvg_bull = (df['h'].iloc[-3], df['l'].iloc[-1])
            
        fvg_bear = None
        if df['h'].iloc[-1] < df['l'].iloc[-3]:
            fvg_bear = (df['l'].iloc[-3], df['h'].iloc[-1])

        # --- 籌碼面數據 ---
        ls_url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base}-USDT"
        ls_res = requests.get(ls_url).json()['data']
        ls_now, ls_prev = float(ls_res[0]['ratio']), float(ls_res[1]['ratio'])
        
        f_url = f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}"
        funding = float(requests.get(f_url).json()['data'][0]['fundingRate'])

        # --- 融合決策邏輯 ---
        side, entry, sl, tp, signal_type = None, None, None, None, ""

        # 看多：結構轉多 + CVD(模擬) + 人數比降 + 資費低
        if is_choch_bull and ls_now < ls_prev and funding < 0.0002:
            side = "做多 LONG 🟢"
            # 進場點選 FVG 回採
            entry = fvg_bull[1] if fvg_bull else curr_p * 0.998
            sl = last_lo * 0.995
            tp = entry + (entry - sl) * 2.5 # 1:2.5 盈虧比
            signal_type = "SMC CHoCH + 大戶接盤"

        # 看空：結構轉空 + 人數比升 + 資費高
        elif is_choch_bear and ls_now > ls_prev and funding > 0.0001:
            side = "做空 SHORT 🔴"
            entry = fvg_bear[1] if fvg_bear else curr_p * 1.002
            sl = last_hi * 1.005
            tp = entry - (sl - entry) * 2.5
            signal_type = "SMC CHoCH + 大戶派發"

        if side:
            return {
                "ticker": base, "side": side, "p": curr_p, "entry": entry,
                "sl": sl, "tp": tp, "type": signal_type, "ls": ls_now, "fund": funding
            }
        return None
    except:
        return None

def format_msg(s):
    return (f"🔮 *Alpha Oracle | SMC 進場預警*\n"
            f"═" * 15 + "\n"
            f"💎 幣種：*{s['ticker']}*\n"
            f"🎯 動作：*{s['side']}*\n"
            f"🔍 邏輯：`{s['type']}`\n\n"
            f"📍 *建議進場(回採)*：`{s['entry']:.4f}`\n"
            f"🎯 *止盈 (TP)*：`{s['tp']:.4f}`\n"
            f"🛡️ *止損 (SL)*：`{s['sl']:.4f}`\n\n"
            f"📊 LS: `{s['ls']}` | 資費: `{s['fund']*100:.3f}%`\n"
            f"⏰ 時間：{(datetime.utcnow()+timedelta(hours=8)).strftime('%H:%M')}\n")

def main():
    for instId in WATCHLIST:
        res = fetch_smc_analysis(instId)
        if res:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                         json={"chat_id": CHAT_ID, "text": format_msg(res), "parse_mode": "Markdown"})

if __name__ == "__main__":
    main()
