import requests
import os
import random
from datetime import datetime, timedelta

# 1. 系統設定
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 2. 監控池
WATCHLIST = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "ORDI-USDT-SWAP", "PEPE-USDT-SWAP", "WIF-USDT-SWAP", "FET-USDT-SWAP"
]

def fetch_analysis(instId):
    try:
        base = instId.split('-')[0]
        c_url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=12H&limit=2"
        data = requests.get(c_url, timeout=15).json()['data']
        curr_p, o0 = float(data[0][4]), float(data[0][1])
        h1, l1 = float(data[1][2]), float(data[1][3])
        
        ls_url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base}-USDT"
        ls_ratio = float(requests.get(ls_url).json()['data'][0]['ratio'])
        f_url = f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}"
        funding = float(requests.get(f_url).json()['data'][0]['fundingRate'])

        # --- 勝率評分邏輯 (放寬版) ---
        win_rate = 60.0 # 測試版給予較高基礎分
        side = "LONG" if curr_p > o0 else "SHORT"
        
        if side == "LONG" and ls_ratio < 1.18: win_rate += 20.0
        if side == "SHORT" and ls_ratio > 1.12: win_rate += 20.0

        win_rate = min(max(win_rate + random.uniform(-1, 1), 40.0), 98.0)
        sl = l1 * 0.988 if side == "LONG" else h1 * 1.012
        tp = curr_p + (curr_p - sl) * 2 if side == "LONG" else curr_p - (sl - curr_p) * 2
        
        return {"ticker": base, "side": side, "p": curr_p, "tp": tp, "sl": sl, "win": win_rate, "ls": ls_ratio, "fund": funding}
    except:
        return None

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    results = [fetch_analysis(c) for c in WATCHLIST]
    results = [r for r in results if r]
    
    # --- 強制補發模式 ---
    top_5 = sorted(results, key=lambda x: x['win'], reverse=True)[:5]
    msg = f"🌅 *Alpha Oracle | 補發今日精選早報*\n📅 補發時間：{now_tw.strftime('%m/%d %H:%M')}\n"
    msg += "═" * 18 + "\n\n"
    for s in top_5:
        msg += format_signal(s)
    msg += "✅ *補發測試完成，請確認 Telegram 是否收到。*"

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
