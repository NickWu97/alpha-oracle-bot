import requests
import os

TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]

def get_data(instId):
    url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=12H&limit=2"
    res = requests.get(url).json()['data']
    c0, o0, h1, l1 = float(res[0][4]), float(res[0][1]), float(res[1][2]), float(res[1][3])
    
    # 5% 擠壓邏輯
    is_long = c0 > o0 and (h1 - c0) / h1 < 0.05
    is_short = c0 < o0 and (c0 - l1) / l1 < 0.05
    
    status = "🚀 多頭擠壓" if is_long else "📉 空頭擠壓" if is_short else "☁️ 盤整"
    return f"• {instId.split('-')[0]}: {status}"

def main():
    reports = [get_data(c) for c in COINS]
    msg = "📢 Alpha Oracle 早報\n\n" + "\n".join(reports)
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": msg})

if __name__ == "__main__":
    main()
