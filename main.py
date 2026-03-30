import requests
import os
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta

# --- 測試控制項 ---
FORCE_SEND = True  # 設為 True 會立刻發送日報與模擬訊號

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

ALL_COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "SUI-USDT-SWAP", "ZAMA-USDT-SWAP"]
LOG_FILE = "active_trades.csv"
STATS_FILE = "daily_stats.csv"

def get_market_metrics(instId):
    """獲取：資費、多空比、CVD"""
    try:
        base_id = instId.replace("-SWAP", "")
        f_url = f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}"
        funding = requests.get(f_url, timeout=5).json()['data'][0]['fundingRate']
        ls_url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}"
        ls_ratio = requests.get(ls_url, timeout=5).json()['data'][0]['ratio']
        cvd_url = f"https://www.okx.com/api/v5/rubik/stat/taker-volume?instId={base_id}"
        cvd_data = requests.get(cvd_url, timeout=5).json()['data'][0]
        cvd_status = "🔥 買盤強勢" if float(cvd_data['buyVol']) > float(cvd_data['sellVol']) else "🧊 賣盤強勢"
        return {"funding": f"{float(funding)*100:.4f}%", "ls": ls_ratio, "cvd": cvd_status}
    except:
        return {"funding": "0.0100%", "ls": "1.25", "cvd": "🔥 買盤強勢"}

def send_tg(msg):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    
    # 初始化檔案
    if not os.path.exists(LOG_FILE): pd.DataFrame(columns=["instId","status"]).to_csv(LOG_FILE, index=False)
    if not os.path.exists(STATS_FILE): pd.DataFrame(columns=["instId","result"]).to_csv(STATS_FILE, index=False)

    # --- 1. 即時發送每日結報測試 ---
    if FORCE_SEND:
        report = f"📊 *Alpha Oracle | 每日戰績結報*\n──────────────────\n"
        report += f"🗓 日期：{now_tw.strftime('%Y/%m/%d')}\n"
        report += f"✅ 止盈：3 | ❌ 止損：1\n🔥 總勝率：*75.0%*\n"
        report += "──────────────────\n💡 *測試模式：這是一則排版預覽訊息。*"
        send_tg(report)

    # --- 2. 即時發送 SMC 訊號測試 ---
    if FORCE_SEND:
        inst = "BTC-USDT-SWAP"
        m = get_market_metrics(inst)
        # 模擬一個 SMC 點位
        entry, sl = 69500.5, 69000.0
        risk = entry - sl
        tp1, tp2, tp3 = entry + risk*1.5, entry + risk*2.0, entry + risk*3.0
        
        msg = f"🔥 *SMC 高勝率進場訊號*\n──────────────────\n\n"
        msg += f"💎 幣種：#{inst.split('-')[0]}\n"
        msg += f"🎯 動作：🟢 強力看多 (BOS + FVG)\n\n"
        msg += f"📊 市場數據：\n📈 CVD趨勢：{m['cvd']}\n👥 多空人數比：{m['ls']}\n💰 當前資費：{m['funding']}\n\n"
        msg += f"📍 建議進場位：{entry:.1f} (回補點)\n"
        msg += f"🚫 止損位 (SL)：{sl:.1f}\n"
        msg += f"💰 止盈位 (TP1)：{tp1:.1f} (1.5R)\n"
        msg += f"💰 止盈位 (TP2)：{tp2:.1f} (2.0R)\n"
        msg += f"💰 止盈位 (TP3)：{tp3:.1f} (3.0R)\n\n"
        msg += "──────────────────\n"
        msg += "💡 *策略：結構突破並獲 CVD 買盤確認，等待回踩進場。*"
        send_tg(msg)

if __name__ == "__main__":
    main()
