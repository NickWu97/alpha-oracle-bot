import requests
import os
import random
from datetime import datetime, timedelta

# 1. 抓取 GitHub Secrets
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 2. 測試監控池 (5大5小)
WATCHLIST = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "ORDI-USDT-SWAP", "PEPE-USDT-SWAP", "WIF-USDT-SWAP", "FET-USDT-SWAP"
]

def fetch_test_data(instId):
    try:
        base = instId.split('-')[0]
        # 抓取 OKX 數據
        c_url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=12H&limit=2"
        data = requests.get(c_url, timeout=10).json()['data']
        curr_p, o0 = float(data[0][4]), float(data[0][1])
        
        ls_url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base}-USDT"
        ls_ratio = float(requests.get(ls_url).json()['data'][0]['ratio'])
        
        f_url = f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}"
        funding = float(requests.get(f_url).json()['data'][0]['fundingRate'])

        side = "LONG" if curr_p > o0 else "SHORT"
        # 測試用偽隨機勝率 (確保排版好看)
        test_win = 65.0 + random.uniform(5, 25)
        
        return {"ticker": base, "side": side, "p": curr_p, "win": test_win, "ls": ls_ratio, "fund": funding}
    except:
        return None

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    results = [fetch_test_data(c) for c in WATCHLIST]
    results = [r for r in results if r]

    msg = f"🛰️ *Alpha Oracle | 全系統連線測試*\n"
    msg += f"⏰ 測試時間：{now_tw.strftime('%m/%d %H:%M:%S')}\n"
    msg += "═" * 18 + "\n\n"

    # 直接列出前 8 名 (測試排版)
    top_list = sorted(results, key=lambda x: x['win'], reverse=True)[:8]

    for s in top_list:
        emoji = "🚀" if s['side'] == "LONG" else "📉"
        tag = "🏛️ 大幣" if s['ticker'] in ["BTC", "ETH", "SOL", "BNB", "XRP"] else "🧪 小幣"
        msg += (f"{tag} *{s['ticker']}* | `{s['win']:.1f}%` {emoji}\n"
                f"├ 現價：`{s['p']}`\n"
                f"├ LS比：`{s['ls']}`\n"
                f"└ 資費：`{s['fund']*100:.3f}%`\n"
                f"─" * 12 + "\n\n")

    msg += "✅ *連線測試成功！*\n💡 提示：若收到此訊息，請換回正式版代碼以啟動 08:30 早報功能。"

    # 發送到 Telegram
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                  json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})

if __name__ == "__main__":
    main()
