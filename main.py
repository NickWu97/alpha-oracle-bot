import requests
import os
import random
from datetime import datetime, timedelta

# 1. 系統設定
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 2. 監控池 (5大5小)
WATCHLIST = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "ORDI-USDT-SWAP", "PEPE-USDT-SWAP", "WIF-USDT-SWAP", "FET-USDT-SWAP"
]

def fetch_analysis(instId):
    try:
        base = instId.split('-')[0]
        # 抓取 12H K線數據
        c_url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=12H&limit=2"
        data = requests.get(c_url, timeout=10).json()['data']
        curr_p, o0 = float(data[0][4]), float(data[0][1])
        h1, l1 = float(data[1][2]), float(data[1][3])
        
        # 抓取多空比與資費
        ls_url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base}-USDT"
        ls_ratio = float(requests.get(ls_url).json()['data'][0]['ratio'])
        f_url = f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}"
        funding = float(requests.get(f_url).json()['data'][0]['fundingRate'])

        # --- 勝率評分邏輯 (機會較多版) ---
        win_rate = 58.0
        side = "LONG" if curr_p > o0 else "SHORT"
        
        if side == "LONG":
            if ls_ratio < 1.18: win_rate += 22.0
            if funding < 0.0002: win_rate += 10.0
        else:
            if ls_ratio > 1.12: win_rate += 22.0
            if funding > 0.0001: win_rate += 10.0
            
        if (side == "LONG" and curr_p > h1) or (side == "SHORT" and curr_p < l1):
            win_rate += 8.0

        win_rate = min(max(win_rate + random.uniform(-1, 1), 40.0), 98.0)

        # --- 核心點位計算 (1:2 盈虧比) ---
        # 止損設在前一根 12H K線的低點/高點外加一點緩衝
        if side == "LONG":
            sl = l1 * 0.992  # 止損設在低點下方 0.8%
            risk = curr_p - sl
            tp = curr_p + (risk * 2) # 1:2 盈虧比
        else:
            sl = h1 * 1.008  # 止損設在高點上方 0.8%
            risk = sl - curr_p
            tp = curr_p - (risk * 2) # 1:2 盈虧比
        
        return {
            "ticker": base, "side": side, "p": curr_p, 
            "tp": tp, "sl": sl, "win": win_rate, 
            "ls": ls_ratio, "fund": funding
        }
    except:
        return None

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    # 早報時間 08:30 - 09:15
    is_morning = (now_tw.hour == 8 and now_tw.minute >= 30) or (now_tw.hour == 9 and now_tw.minute <= 15)

    results = [fetch_analysis(c) for c in WATCHLIST]
    results = [r for r in results if r]
    
    msg = ""
    if is_morning:
        # 早報模式：發送前 5 名
        top_5 = sorted(results, key=lambda x: x['win'], reverse=True)[:5]
        msg = f"🌅 *Alpha Oracle | 每日精選早報*\n📅 日期：{now_tw.strftime('%m/%d')}\n"
        msg += "═" * 18 + "\n\n"
        for s in top_5:
            msg += format_signal(s)
    else:
        # 即時模式：勝率 > 72% 發報
        entry_signals = [s for s in results if s['win'] >= 72.0]
        if entry_signals:
            msg = f"🔔 *Alpha Oracle | 即時進場訊號*\n"
            msg += "═" * 18 + "\n\n"
            for s in entry_signals:
                msg += format_signal(s)

    if msg:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                      json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})

def format_signal(s):
    emoji = "🚀" if s['side'] == "LONG" else "📉"
    tag = "🏛️ 大幣" if s['ticker'] in ["BTC", "ETH", "SOL", "BNB", "XRP"] else "🧪 小幣"
    # 格式化價格顯示位數
    p_fmt = f"{s['p']:.4f}" if s['p'] < 10 else f"{s['p']:.2f}"
    tp_fmt = f"{s['tp']:.4f}" if s['tp'] < 10 else f"{s['tp']:.2f}"
    sl_fmt = f"{s['sl']:.4f}" if s['sl'] < 10 else f"{s['sl']:.2f}"
    
    return (f"{tag} *{s['ticker']}* | 勝率 `{s['win']:.1f}%`\n"
            f"方向：`{s['side']}` {emoji}\n"
            f"📍 *進場價格*：`{p_fmt}`\n"
            f"🎯 *止盈 (TP)*：`{tp_fmt}`\n"
            f"🛡️ *止損 (SL)*：`{sl_fmt}`\n"
            f"📊 LS: `{s['ls']}` | 資費: `{s['fund']*100:.3f}%`\n"
            f"─" * 12 + "\n\n")

if __name__ == "__main__":
    main()
