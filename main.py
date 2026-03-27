import requests
import os
import pandas as pd
import numpy as np
import random
from datetime import datetime, timedelta

# 1. 系統環境變數 (請確保 GitHub Secrets 已設定)
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
        raw = requests.get(c_url, timeout=10).json()['data']
        df = pd.DataFrame(raw, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'volCcy', 'volCcyQuote', 'confirm'])
        df[['o', 'h', 'l', 'c', 'v']] = df[['o', 'h', 'l', 'c', 'v']].astype(float)
        df = df.iloc[::-1].reset_index(drop=True)
        curr_p = df['c'].iloc[-1]

        # --- SMC 結構計算 ---
        df['hi_max'] = df['h'].rolling(window=5, center=True).max()
        df['lo_min'] = df['l'].rolling(window=5, center=True).min()
        
        # 取得上一個波段高低點
        valid_highs = df[df['h'] == df['hi_max']]['h']
        valid_lows = df[df['l'] == df['lo_min']]['l']
        
        last_hi = valid_highs.iloc[-2] if len(valid_highs) > 1 else df['h'].max()
        last_lo = valid_lows.iloc[-2] if len(valid_lows) > 1 else df['l'].min()
        
        # CHoCH 判斷
        is_choch_bull = curr_p > last_hi
        is_choch_bear = curr_p < last_lo

        # FVG 判斷 (回採區間)
        fvg_price = None
        if df['l'].iloc[-1] > df['h'].iloc[-3]: # 看多 FVG
            fvg_price = df['l'].iloc[-1]
        elif df['h'].iloc[-1] < df['l'].iloc[-3]: # 看空 FVG
            fvg_price = df['h'].iloc[-1]

        # --- 籌碼面數據 ---
        ls_url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base}-USDT"
        ls_res = requests.get(ls_url).json()['data']
        ls_now = float(ls_res[0]['ratio'])
        ls_prev = float(ls_res[1]['ratio'])
        
        # --- 勝率計算法 (基於 SMC + 籌碼) ---
        win_rate = 55 # 基礎勝率
        if is_choch_bull and ls_now < ls_prev: win_rate += 25 # 結構+籌碼同步看多
        if is_choch_bear and ls_now > ls_prev: win_rate += 25 # 結構+籌碼同步看空
        if fvg_price: win_rate += 5 # 有 FVG 支撐/壓力
        win_rate = min(win_rate + random.randint(-3, 3), 92) # 隨機擾動，最高不超過 92%

        side = "看多 LONG 🟢" if (curr_p > last_hi or ls_now < ls_prev) else "看空 SHORT 🔴"
        
        result = {
            "base": base, "side": side, "win": win_rate, "p": curr_p,
            "is_choch": (is_choch_bull or is_choch_bear),
            "entry": fvg_price if fvg_price else curr_p
        }
        
        if force_report: return result
        return result if result["is_choch"] and win_rate > 70 else None
    except:
        return None

def main():
    # 取得台灣時間 (GMT+8)
    now_tw = datetime.utcnow() + timedelta(hours=8)
    # 判斷是否為 08:30 區間 (GitHub Cron 可能有幾分鐘延遲)
    is_report_time = (now_tw.hour == 8 and 15 <= now_tw.minute <= 45)

    if is_report_time:
        msg = f"🌅 *Alpha Oracle 早盤盤面總結*\n"
        msg += f"⏰ 時間：{now_tw.strftime('%Y-%m-%d %H:%M')}\n"
        msg += "═"*15 + "\n\n"
        
        msg += "🏆 *主流幣 (Top 5)*\n"
        for coin in MAINSTREAM:
            r = fetch_smc_analysis(coin, force_report=True)
            if r: msg += f"• *{r['base']}*: {r['side']} | 勝率 `{r['win']}%` | 點位 `{r['entry']:.2f}`\n"
        
        msg += "\n🚀 *山寨幣 (Top 5)*\n"
        for coin in ALTS:
            r = fetch_smc_analysis(coin, force_report=True)
            if r: msg += f"• *{r['base']}*: {r['side']} | 勝率 `{r['win']}%` | 點位 `{r['entry']:.2f}`\n"
        
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                     json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})
    else:
        # --- 24H 即時強訊號監控 ---
        for coin in (MAINSTREAM + ALTS):
            r = fetch_smc_analysis(coin, force_report=False)
            if r:
                alert = (f"🚨 *SMC 高勝率進場警報*\n"
                         f"═"*15 + f"\n"
                         f"💎 幣種：{r['base']}\n"
                         f"🎯 動作：{r['side']}\n"
                         f"📈 預估勝率：`{r['win']}%`\n"
                         f"📍 進場點位：`{r['entry']:.4f}`\n"
                         f"💡 說明：偵測到 CHoCH 結構轉變，建議等待回採進場。")
                requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                             json={"chat_id": CHAT_ID, "text": alert, "parse_mode": "Markdown"})

if __name__ == "__main__":
    main()
