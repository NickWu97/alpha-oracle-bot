import requests
import os
import random
from datetime import datetime, timedelta

TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 主流幣監控
MAINSTREAM = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
# 潛力小幣榜 (波動大、易拉跌)
ALTCOINS = ["SUI-USDT-SWAP", "ORDI-USDT-SWAP", "PEPE-USDT-SWAP", "WIF-USDT-SWAP", "TIA-USDT-SWAP", "BLUR-USDT-SWAP", "METIS-USDT-SWAP", "APT-USDT-SWAP", "OP-USDT-SWAP", "ARB-USDT-SWAP"]

def get_market_data(instId, is_alt=False):
    try:
        base = instId.split('-')[0]
        # 1. 抓取 K 線 (12H) 與 成交量
        c_url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=12H&limit=2"
        data = requests.get(c_url, timeout=10).json()['data']
        curr_p, o0, v0 = float(data[0][4]), float(data[0][1]), float(data[0][7]) # 收盤, 開盤, 成交量(幣)
        h1, l1, v1 = float(data[1][2]), float(data[1][3]), float(data[1][7]) # 前根高, 低, 量
        
        # 2. 抓取持倉量 (OI)
        oi_url = f"https://www.okx.com/api/v5/public/open-interest?instId={instId}"
        oi = float(requests.get(oi_url).json()['data'][0]['oi'])

        # 3. 抓取多空比與資費
        ls_url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base}-USDT"
        ls_ratio = float(requests.get(ls_url).json()['data'][0]['ratio'])
        f_url = f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}"
        funding = float(requests.get(f_url).json()['data'][0]['fundingRate'])

        vol_increase = (v0 - v1) / v1 if v1 > 0 else 0
        is_up = curr_p > o0

        # --- 小幣爆發邏輯：量增 + 持倉增 + 散戶反向 ---
        if is_alt:
            # 爆發評分：成交量增加 + 持倉量大 + LS比極端
            score = (vol_increase * 50) + (1/ls_ratio * 20 if is_up else ls_ratio * 20)
            return {"ticker": base, "score": score, "p": curr_p, "ls": ls_ratio, "vol_up": vol_increase}
        
        # --- 主流幣背離報單邏輯 ---
        else:
            side = "LONG" if (is_up and ls_ratio < 1.1 and funding < 0.0001) else "SHORT" if (not is_up and ls_ratio > 1.2 and funding > 0.0002) else None
            if side:
                sl = l1 * 0.99 if side == "LONG" else h1 * 1.01
                tp = curr_p + (curr_p - sl) * 2 if side == "LONG" else curr_p - (sl - curr_p) * 2
                return {"ticker": base, "side": side, "p": curr_p, "tp": tp, "sl": sl, "ls": ls_ratio, "fund": funding}
        return None
    except:
        return None

def main():
    # 執行主流幣報單
    main_signals = [get_market_data(c, False) for c in MAINSTREAM]
    main_signals = [s for s in main_signals if s and 'side' in s]

    # 執行小幣爆發榜篩選
    alt_results = [get_market_data(c, True) for c in ALTCOINS]
    alt_results = [a for a in alt_results if a]
    top_alts = sorted(alt_results, key=lambda x: x['score'], reverse=True)[:3]

    now_tw = datetime.utcnow() + timedelta(hours=8)
    msg = f"🛰️ *Alpha Oracle | 全方位掃描*\n⏰ {now_tw.strftime('%H:%M')}\n"
    msg += "═" * 18 + "\n\n"

    if main_signals:
        msg += "🔥 *精選進場點位*\n"
        for s in main_signals:
            msg += f"💎 *{s['ticker']}* | `{s['side']}`\n📍 進：`{s['p']}` | 🎯 盈：`{s['tp']:.2f}`\n🛡️ 損：`{s['sl']:.2f}`\n\n"
    
    msg += "🚀 *小幣爆發潛力榜 (熱度)*\n"
    for a in top_alts:
        trend = "📈 強勢" if a['vol_up'] > 0 else "📉 走弱"
        msg += f"• *{a['ticker']}*：熱度 `{a['score']:.1f}` | {trend}\n  (LS比: `{a['ls']}` | 量增: `{a['vol_up']*100:.1%}`)\n"

    msg += "\n⚠️ *小幣波動極大，請嚴格縮小倉位。*"

    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                  json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})

if __name__ == "__main__":
    main()
