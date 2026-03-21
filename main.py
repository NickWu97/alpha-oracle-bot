import requests
import os
from datetime import datetime, timedelta

TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 監控池 (保持 5 隻主要標的)
COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "SUI-USDT-SWAP", "ORDI-USDT-SWAP"]

def get_signal(instId):
    try:
        base = instId.split('-')[0]
        # 抓取 OKX 數據
        c_url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=12H&limit=2"
        c_raw = requests.get(c_url, timeout=10).json()['data']
        curr_p, o0 = float(c_raw[0][4]), float(c_raw[0][1])
        h1, l1 = float(c_raw[1][2]), float(c_raw[1][3])
        
        ls_url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base}-USDT"
        ls_ratio = float(requests.get(ls_url).json()['data'][0]['ratio'])
        
        f_url = f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}"
        funding = float(requests.get(f_url).json()['data'][0]['fundingRate'])

        # --- 判斷邏輯 ---
        side = None
        # 做多：現貨漲 + 散戶空 (LS < 1.1) + 低資費
        if curr_p > o0 and ls_ratio < 1.1 and funding < 0.0001:
            side = "LONG"
        # 做空：現貨跌 + 散戶多 (LS > 1.2) + 高資費
        elif curr_p < o0 and ls_ratio > 1.2 and funding > 0.0002:
            side = "SHORT"

        if side:
            # 計算 1:2 點位
            sl = l1 * 0.99 if side == "LONG" else h1 * 1.01
            tp = curr_p + (curr_p - sl) * 2 if side == "LONG" else curr_p - (sl - curr_p) * 2
            return {"ticker": base, "side": side, "price": curr_p, "tp": tp, "sl": sl, "ls": ls_ratio, "fund": funding}
        
        # 沒訊號時返回基本數據供診斷
        return {"ticker": base, "side": None, "ls": ls_ratio, "fund": funding}
    except:
        return None

def main():
    results = [get_signal(c) for c in COINS if get_signal(c)]
    signals = [r for r in results if r['side'] is not None]
    
    now_tw = datetime.utcnow() + timedelta(hours=8)
    msg = f"🛰️ *Alpha Oracle 診斷報告*\n⏰ 時間：{now_tw.strftime('%H:%M')}\n"
    
    if signals:
        msg += "🔥 *發現進場機會！*\n\n"
        for s in signals:
            msg += (f"💎 *{s['ticker']}* | 方向：`{s['side']}`\n"
                    f"📍 進場：`{s['price']}`\n"
                    f"🎯 止盈：`{s['tp']:.4f}`\n"
                    f"🛡️ 止損：`{s['sl']:.4f}`\n\n")
    else:
        # 如果沒訊號，發送目前的數據狀態，讓你知道為什麼沒報單
        msg += "💤 *目前無背離訊號*\n指標監控中：\n"
        for r in results:
            msg += f"• {r['ticker']}: LS `{r['ls']}` | 資費 `{r['fund']*100:.3f}%`\n"

    # 發送至 TG
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                  json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})

if __name__ == "__main__":
    main()
