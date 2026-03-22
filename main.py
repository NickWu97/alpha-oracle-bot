import requests
import os
import random
from datetime import datetime, timedelta

# 1. 安全抓取環境變數
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 2. 核心監控池 (5 大 5 小)
WATCHLIST = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP", # 大幣
    "SUI-USDT-SWAP", "ORDI-USDT-SWAP", "PEPE-USDT-SWAP", "WIF-USDT-SWAP", "FET-USDT-SWAP" # 小幣
]

def fetch_analysis(instId):
    try:
        base = instId.split('-')[0]
        # A. 抓取 12H K線
        c_url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=12H&limit=2"
        c_data = requests.get(c_url, timeout=10).json()['data']
        curr_p, o0 = float(c_data[0][4]), float(c_data[0][1])
        h1, l1 = float(c_data[1][2]), float(c_data[1][3])
        
        # B. 抓取多空人數持倉比 (散戶情緒)
        ls_url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base}-USDT"
        ls_ratio = float(requests.get(ls_url).json()['data'][0]['ratio'])
        
        # C. 抓取資費 (持倉成本)
        f_url = f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}"
        funding = float(requests.get(f_url).json()['data'][0]['fundingRate'])

        # --- 技術分析：勝率加權邏輯 (背離與擠壓) ---
        win_rate = 55.0  # 基礎分
        is_up = curr_p > o0
        side = "LONG" if is_up else "SHORT"
        
        # 1. 核心背離邏輯 (現貨動能 vs 散戶情緒)
        if side == "LONG":
            if ls_ratio < 1.15: win_rate += 22.0 # 價格漲 + 散戶不敢追多 = 高機率續漲
            if funding < 0.00015: win_rate += 10.0
        else:
            if ls_ratio > 1.15: win_rate += 22.0 # 價格跌 + 散戶死命接多 = 高機率續跌
            if funding > 0.00015: win_rate += 10.0

        # 2. 突破動能 (12H 擠壓後突破)
        if (side == "LONG" and curr_p > h1) or (side == "SHORT" and curr_p < l1):
            win_rate += 8.5

        # 微調與限制
        win_rate = min(max(win_rate + random.uniform(-1, 1), 40.0), 98.0)

        # 3. 計算 1:2 盈虧比點位
        sl = l1 * 0.988 if side == "LONG" else h1 * 1.012
        tp = curr_p + (curr_p - sl) * 2 if side == "LONG" else curr_p - (sl - curr_p) * 2
        
        return {
            "ticker": base, "side": side, "p": curr_p, "tp": tp, "sl": sl, 
            "win": win_rate, "ls": ls_ratio, "fund": funding
        }
    except:
        return None

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    # 早報判定：08:30 - 09:15 之間執行則觸發早報
    is_morning = (now_tw.hour == 8 and now_tw.minute >= 30) or (now_tw.hour == 9 and now_tw.minute <= 15)

    results = [fetch_analysis(c) for c in WATCHLIST]
    results = [r for r in results if r]
    
    msg = ""
    if is_morning:
        # --- 模式 A: 每日精選早報 (前 5 名) ---
        top_5 = sorted(results, key=lambda x: x['win'], reverse=True)[:5]
        msg = f"🌅 *Alpha Oracle | 每日精選早報*\n📅 日期：{now_tw.strftime('%m/%d')}\n"
        msg += "═" * 18 + "\n\n"
        for s in top_5:
            msg += format_report(s)
        msg += "⚠️ *早報為當日趨勢參考，建議分批進場。*"
    else:
        # --- 模式 B: 即時進場訊號 (勝率需 > 72.0) ---
        signals = [s for s in results if s['win'] >= 72.0]
        if signals:
            msg = f"🔔 *Alpha Oracle | 即時進場訊號*\n"
            msg += "═" * 18 + "\n\n"
            for s in signals:
                msg += format_report(s)

    # 發送至 Telegram
    if msg:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                      json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})

def format_report(s):
    emoji = "🚀" if s['side'] == "LONG" else "📉"
    tag = "🏛️ 大幣" if s['ticker'] in ["BTC", "ETH", "SOL", "BNB", "XRP"] else "🧪 小幣"
    return (f"{tag} *{s['ticker']}* | 勝率 `{s['win']:.1f}%`\n"
            f"建議：`{s['side']}` {emoji}\n"
            f"📍 進場：`{s['p']}`\n"
            f"🎯 止盈：`{s['tp']:.4f}`\n"
            f"🛡️ 止損：`{s['sl']:.4f}`\n"
            f"📊 LS 比：`{s['ls']}` | 資費：`{s['fund']*100:.3f}%`\n"
            f"─" * 12 + "\n\n")

if __name__ == "__main__":
    main()
