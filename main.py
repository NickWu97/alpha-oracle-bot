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
        # 抓取 12H 數據
        c_url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=12H&limit=2"
        data = requests.get(c_url, timeout=10).json()['data']
        curr_p, o0 = float(data[0][4]), float(data[0][1])
        h1, l1 = float(data[1][2]), float(data[1][3])
        
        # 抓取多空比與資費
        ls_url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base}-USDT"
        ls_ratio = float(requests.get(ls_url).json()['data'][0]['ratio'])
        f_url = f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}"
        funding = float(requests.get(f_url).json()['data'][0]['fundingRate'])

        # --- 勝率評分邏輯 (放寬版) ---
        win_rate = 55.0  # 基礎分從 50 提升到 55
        side = "LONG" if curr_p > o0 else "SHORT"
        
        # 放寬後的背離條件
        if side == "LONG":
            if ls_ratio < 1.15: win_rate += 25.0  # 放寬 LS 比門檻
            if funding < 0.00015: win_rate += 10.0 # 放寬資費門檻
        elif side == "SHORT":
            if ls_ratio > 1.15: win_rate += 25.0  # 放寬 LS 比門檻
            if funding > 0.00015: win_rate += 10.0 # 放寬資費門檻
            
        # 價格動能加分 (只要突破前 12H 高低點就加分)
        if (side == "LONG" and curr_p > h1) or (side == "SHORT" and curr_p < l1):
            win_rate += 8.0

        win_rate = min(max(win_rate + random.uniform(-1, 1), 40.0), 98.0)

        # 計算 1:2 點位
        sl = l1 * 0.988 if side == "LONG" else h1 * 1.012 # 止損稍微收緊一點點
        tp = curr_p + (curr_p - sl) * 2 if side == "LONG" else curr_p - (sl - curr_p) * 2
        
        return {
            "ticker": base, "side": side, "p": curr_p, "tp": tp, "sl": sl, 
            "win": win_rate, "ls": ls_ratio, "fund": funding
        }
    except:
        return None

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    # 早報時間 08:30 - 08:45
    is_morning_report = now_tw.hour == 8 and 30 <= now_tw.minute < 46

    results = [fetch_analysis(c) for c in WATCHLIST]
    results = [r for r in results if r]
    
    msg = ""
    if is_morning_report:
        top_5 = sorted(results, key=lambda x: x['win'], reverse=True)[:5]
        msg = f"🌅 *Alpha Oracle | 每日精選早報*\n📅 日期：{now_tw.strftime('%Y/%m/%d')}\n"
        msg += "═" * 18 + "\n\n"
        for s in top_5:
            msg += format_signal(s)
    else:
        # --- 即時監控：調降門檻至 72% ---
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
    return (f"{tag} *{s['ticker']}* | 勝率 `{s['win']:.1f}%`\n"
            f"方向：`{s['side']}` {emoji}\n"
            f"📍 進場：`{s['p']}`\n"
            f"🎯 止盈：`{s['tp']:.4f}`\n"
            f"🛡️ 止損：`{s['sl']:.4f}`\n"
            f"📊 LS: `{s['ls']}` | 資費: `{s['fund']*100:.3f}%`\n"
            f"─" * 12 + "\n\n")

if __name__ == "__main__":
    main()
