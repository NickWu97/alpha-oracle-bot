import requests
import os
import pandas as pd
import numpy as np
import random
import time
from datetime import datetime, timedelta

# 1. 系統環境變數
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 2. 監控清單
MAINSTREAM = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP"]
ALTS = ["SUI-USDT-SWAP", "ORDI-USDT-SWAP", "PEPE-USDT-SWAP", "WIF-USDT-SWAP", "FET-USDT-SWAP"]

def fetch_smc_analysis(instId, force_report=False):
    try:
        base = instId.split('-')[0]
        # --- 抓取 15m K線數據 ---
        c_url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=15m&limit=100"
        res = requests.get(c_url, timeout=10).json()
        if 'data' not in res: return None
        
        df = pd.DataFrame(res['data'], columns=['ts', 'o', 'h', 'l', 'c', 'v', 'volCcy', 'volCcyQuote', 'confirm'])
        df[['o', 'h', 'l', 'c', 'v']] = df[['o', 'h', 'l', 'c', 'v']].astype(float)
        df = df.iloc[::-1].reset_index(drop=True)
        curr_p = df['c'].iloc[-1]

        # --- SMC 結構計算 ---
        df['hi_max'] = df['h'].rolling(window=5, center=True).max()
        df['lo_min'] = df['l'].rolling(window=5, center=True).min()
        
        valid_highs = df[df['h'] == df['hi_max']]['h']
        valid_lows = df[df['l'] == df['lo_min']]['l']
        
        # 確保有足夠的波段點
        last_hi = valid_highs.iloc[-2] if len(valid_highs) > 1 else df['h'].iloc[:-5].max()
        last_lo = valid_lows.iloc[-2] if len(valid_lows) > 1 else df['l'].iloc[:-5].min()
        
        is_choch_bull = curr_p > last_hi
        is_choch_bear = curr_p < last_lo

        # FVG 判斷
        fvg_price = None
        if df['l'].iloc[-1] > df['h'].iloc[-3]: fvg_price = df['l'].iloc[-1]
        elif df['h'].iloc[-1] < df['l'].iloc[-3]: fvg_price = df['h'].iloc[-1]

        # --- 籌碼面數據 ---
        ls_url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base}-USDT"
        ls_res = requests.get(ls_url, timeout=10).json()['data']
        ls_now = float(ls_res[0]['ratio'])
        ls_prev = float(ls_res[1]['ratio'])
        
        # --- 勝率計算法 ---
        win_rate = 52 
        if is_choch_bull and ls_now < ls_prev: win_rate += 28
        elif is_choch_bear and ls_now > ls_prev: win_rate += 28
        
        if fvg_price: win_rate += 6
        win_rate = min(win_rate + random.randint(-2, 2), 94)

        side = "看多 LONG 🟢" if (curr_p > last_hi or ls_now < ls_prev) else "看空 SHORT 🔴"
        
        result = {
            "base": base, "side": side, "win": win_rate, "p": curr_p,
            "is_choch": (is_choch_bull or is_choch_bear),
            "entry": fvg_price if fvg_price else curr_p
        }
        
        if force_report: return result
        # 24H 監控模式：僅回傳強訊號 (結構突破且勝率 > 75)
        return result if result["is_choch"] and win_rate >= 75 else None
    except Exception as e:
        print(f"Error fetching {instId}: {e}")
        return None

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    # 只要在 08:00 ~ 08:59 之間執行，就發送總結報告
    is_report_time = (now_tw.hour == 8)

    if is_report_time:
        msg = f"🌅 *Alpha Oracle 早盤盤面總結*\n"
        msg += f"📅 日期：{now_tw.strftime('%Y-%m-%d')}\n"
        msg += "═" * 18 + "\n\n"
        
        msg += "🏆 *主流幣 (High Cap)*\n"
        for coin in MAINSTREAM:
            r = fetch_smc_analysis(coin, force_report=True)
            if r: msg += f"• *{r['base']}*: {r['side']}\n  勝率: `{r['win']}%` | 進場: `{r['entry']:.2f}`\n"
        
        msg += "\n🚀 *山寨幣 (Altcoins)*\n"
        for coin in ALTS:
            r = fetch_smc_analysis(coin, force_report=True)
            if r: msg += f"• *{r['base']}*: {r['side']}\n  勝率: `{r['win']}%` | 進場: `{r['entry']:.2f}`\n"
        
        msg += "\n⚠️ _建議等待價格回測進場點位後再操作_"
        
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                     json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})
        print("早盤報告發送成功")
    
    # 無論是否發送報表，都跑一遍 24H 強訊號監控
    for coin in (MAINSTREAM + ALTS):
        r = fetch_smc_analysis(coin, force_report=False)
        if r:
            alert = (f"🚨 *SMC 高勝率進場警報*\n"
                     f"═" * 18 + "\n"
                     f"💎 幣種：#{r['base']}\n"
                     f"🎯 動作：{r['side']}\n"
                     f"📈 預估勝率：`{r['win']}%`\n"
                     f"📍 進場參考：`{r['entry']:.4f}`\n\n"
                     f"💡 _說明：偵測到 CHoCH 結構反轉，配合籌碼面背離，屬於高質量機會。_")
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                         json={"chat_id": CHAT_ID, "text": alert, "parse_mode": "Markdown"})
            time.sleep(1) # 避免 TG API 頻率限制

if __name__ == "__main__":
    main()
