import requests
import os
from datetime import datetime, timedelta

# 1. 抓取你的 GitHub Secrets
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 2. 測試清單
ALT_WATCHLIST = ["SUI-USDT-SWAP", "ORDI-USDT-SWAP", "PEPE-USDT-SWAP", "SOL-USDT-SWAP", "WIF-USDT-SWAP"]

def fetch_test_data(instId):
    try:
        base = instId.split('-')[0]
        # 抓取 12H 數據
        c_url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=12H&limit=2"
        data = requests.get(c_url, timeout=10).json()['data']
        curr_p = float(data[0][4])
        
        # 抓取多空比與資費 (核心數據)
        ls_url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base}-USDT"
        ls_ratio = float(requests.get(ls_url).json()['data'][0]['ratio'])
        f_url = f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}"
        funding = float(requests.get(f_url).json()['data'][0]['fundingRate'])

        # 簡單計算一個測試勝率 (僅供測試排版)
        test_win = 60.0 + (ls_ratio * 10)
        
        return {
            "ticker": base, "p": curr_p, "win": test_win, 
            "ls": ls_ratio, "fund": funding
        }
    except Exception as e:
        print(f"Error fetching {instId}: {e}")
        return None

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    results = [fetch_test_data(c) for c in ALT_WATCHLIST]
    results = [r for r in results if r]

    msg = f"🧪 *Alpha Oracle | 機器人連線測試*\n"
    msg += f"⏰ 測試時間：{now_tw.strftime('%H:%M:%S')}\n"
    msg += "═" * 18 + "\n\n"

    for r in results:
        msg += (f"🔹 *{r['ticker']}*：現價 `{r['p']}`\n"
                f"├ 預測勝率：`{r['win']:.1f}%`\n"
                f"├ 多空持倉比：`{r['ls']}`\n"
                f"└ 當前資費：`{r['fund']*100:.4f}%`\n"
                f"─" * 12 + "\n\n")

    msg += "✅ *如果收到此訊息，代表你的 GitHub 與 Telegram 連線完全正常！*"

    # 發送到 Telegram
    res = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                        json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})
    
    if res.status_code == 200:
        print("測試訊息發送成功！")
    else:
        print(f"發送失敗，錯誤碼：{res.status_code}, 內容：{res.text}")

if __name__ == "__main__":
    main()
