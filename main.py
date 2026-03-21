import requests
import os
from datetime import datetime, timedelta

# 1. 系統設定
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 監控池
COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "SUI-USDT-SWAP", "ORDI-USDT-SWAP"]

def get_signal(instId):
    try:
        base = instId.split('-')[0]
        # 抓取 12H K線
        c_url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=12H&limit=2"
        c_raw = requests.get(c_url, timeout=10).json()['data']
        curr_p, o0 = float(c_raw[0][4]), float(c_raw[0][1])
        h1, l1 = float(c_raw[1][2]), float(c_raw[1][3])
        
        # 抓取多空比與資費 (CoinAnk 同款大數據)
        ls_url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base}-USDT"
        ls_ratio = float(requests.get(ls_url).json()['data'][0]['ratio'])
        
        f_url = f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}"
        funding = float(requests.get(f_url).json()['data'][0]['fundingRate'])

        # --- 進場判斷邏輯 (背離條件) ---
        side = None
        # 多頭背離：現貨漲(CVD+) + 散戶看空(LS低) + 低資費
        if curr_p > o0 and ls_ratio < 1.05 and funding < 0.0001:
            side = "LONG"
        # 空頭背離：現貨跌(CVD-) + 散戶看多(LS高) + 高資費
        elif curr_p < o0 and ls_ratio > 1.20 and funding > 0.0002:
            side = "SHORT"

        if side:
            # 計算 1:2 盈虧比點位
            if side == "LONG":
                sl = l1 * 0.993 # 止損設在前低點稍下方
                tp = curr_p + (curr_p - sl) * 2
            else:
                sl = h1 * 1.007 # 止損設在前高點稍上方
                tp = curr_p - (sl - curr_p) * 2

            return {
                "ticker": base, "side": side, "price": curr_p, 
                "tp": tp, "sl": sl, "ls": ls_ratio, "fund": funding
            }
        return None
    except:
        return None

def main():
    signals = []
    for coin in COINS:
        res = get_signal(coin)
        if res:
            signals.append(res)
    
    if not signals:
        print("掃描完成：目前無符合背離條件之進場訊號。")
        return

    # 組合訊息
    now_tw = datetime.utcnow() + timedelta(hours=8)
    msg = f"🔔 *Alpha Oracle | 即時進場預警*\n"
    msg += f"⏰ 觸發時間：{now_tw.strftime('%H:%M')}\n"
    msg += "═" * 18 + "\n\n"

    for s in signals:
        msg += (f"💎 *{s['ticker']} 交易指令*\n"
                f"方向：`{s['side']}` {'🚀' if s['side']=='LONG' else '📉'}\n"
                f"📍 進場：`{s['price']}`\n"
                f"🎯 止盈：`{s['tp']:.4f}`\n"
                f"🛡️ 止損：`{s['sl']:.4f}`\n"
                f"📊 數據：LS `{s['ls']}` | 資費 `{s['fund']*100:.3f}%`\n"
                f"─" * 12 + "\n\n")

    # 發送至 TG
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                  json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})

if __name__ == "__main__":
    main()
