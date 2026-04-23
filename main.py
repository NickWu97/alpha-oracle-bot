#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import requests
import os
import json
import sys
import argparse
import pandas as pd
import numpy as np
import logging
import time
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────────────────
# 1. 基礎配置
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("alpha_oracle.log", encoding="utf-8", mode="a"),
        logging.StreamHandler()
    ]
)

TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
EMERGENCY_MODE = os.getenv("EMERGENCY_MODE", "false").lower() == "true"

ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP"
]

# ─────────────────────────────────────────────────────────
# 2. 工具 & 通知
# ─────────────────────────────────────────────────────────
def utc_now() -> datetime: return datetime.now(timezone.utc)
def tw_now() -> datetime: return utc_now() + timedelta(hours=8)

def send_tg(msg: str, parse_mode: str = "Markdown", max_retries: int = 3) -> bool:
    if not TG_TOKEN or not CHAT_ID:
        logging.error("❌ TG_TOKEN 或 CHAT_ID 缺失")
        return False
    
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": parse_mode}
    
    for attempt in range(max_retries):
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 200:
                logging.info("✅ Telegram 訊息發送成功")
                return True
            time.sleep(2)
        except Exception as e:
            logging.error(f"❌ 發送異常: {e}")
    return False

# ─────────────────────────────────────────────────────────
# 3. 技術分析模組 (SMC & 趨勢)
# ─────────────────────────────────────────────────────────
def fetch_okx(instId: str, tf: str = "15m", limit: int = 150):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if res.get("code") != "0": return None
        df = pd.DataFrame(res["data"], columns=["ts","o","h","l","c","v","volCcy","volCcyQuote","confirm"])
        df[["o","h","l","c","v"]] = df[["o","h","l","c","v"]].astype(float)
        df = df[df["confirm"] == "1"].iloc[::-1].reset_index(drop=True)
        return df if len(df) >= 30 else None
    except: return None

def calculate_atr(df: pd.DataFrame, period: int = 14):
    df = df.copy()
    df['tr'] = np.maximum(df['h'] - df['l'], 
                          np.maximum(abs(df['h'] - df['c'].shift(1)), 
                                     abs(df['l'] - df['c'].shift(1))))
    return df['tr'].rolling(period).mean().iloc[-1]

def find_swing_points(df: pd.DataFrame, n: int = 3):
    sh = []; sl = []
    for i in range(n, len(df)-n):
        if df['h'].iloc[i] == df['h'].iloc[i-n:i+n+1].max():
            sh.append({'price': df['h'].iloc[i], 'index': i})
        if df['l'].iloc[i] == df['l'].iloc[i-n:i+n+1].min():
            sl.append({'price': df['l'].iloc[i], 'index': i})
    return sh, sl

def check_ob_fvg_entry(df: pd.DataFrame, side: str):
    price = df['c'].iloc[-1]
    atr = calculate_atr(df)
    sh_points, sl_points = find_swing_points(df)
    
    at_ob = at_fvg = False
    ob_desc = "無 OB"; fvg_desc = "無 FVG"
    score = 0
    
    # 判斷 Order Block (OB)
    if side == "LONG" and sl_points:
        last_sl = sl_points[-1]['price']
        if abs(price - last_sl) < atr * 0.4:
            at_ob = True
            ob_desc = f"觸碰支撐 OB ({last_sl:.4f})"
            score += 45
    elif side == "SHORT" and sh_points:
        last_sh = sh_points[-1]['price']
        if abs(price - last_sh) < atr * 0.4:
            at_ob = True
            ob_desc = f"觸碰壓力 OB ({last_sh:.4f})"
            score += 45

    # 判斷 Fair Value Gap (FVG)
    if side == "LONG":
        if df['l'].iloc[-1] > df['h'].iloc[-3]: # 簡單 FVG 判定
            at_fvg = True
            fvg_desc = "FVG 缺口看漲"
            score += 25
    else:
        if df['h'].iloc[-1] < df['l'].iloc[-3]:
            at_fvg = True
            fvg_desc = "FVG 缺口看跌"
            score += 25
            
    return score, f"{ob_desc} | {fvg_desc}"

# ─────────────────────────────────────────────────────────
# 4. 主流程控制
# ─────────────────────────────────────────────────────────
def run_scan():
    logging.info("🔎 開始市場掃描...")
    for coin in ALL_COINS:
        df = fetch_okx(coin)
        if df is None: continue
        
        # 同時掃描多空
        for side in ["LONG", "SHORT"]:
            score, logic = check_ob_fvg_entry(df, side)
            if score >= 60:
                msg = (f"🎯 *Alpha Oracle 訊號發佈*\n"
                       f"━━━━━━━━━━━━━━\n"
                       f"代幣: `{coin}`\n"
                       f"方向: *{'做多' if side == 'LONG' else '做空'}*\n"
                       f"評分: {score}\n"
                       f"邏輯: {logic}\n"
                       f"時間: {tw_now().strftime('%Y-%m-%d %H:%M')}")
                send_tg(msg)
        time.sleep(1) # 避免 API 頻率過快

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', default='scan', help='執行模式 (scan/daily_report)')
    args = parser.parse_args()

    if args.mode == 'scan':
        run_scan()
    elif args.mode == 'daily_report':
        send_tg(f"📊 *Alpha Oracle 每日報告*\n系統當前運行中，數據庫已更新。")
    else:
        logging.info(f"模式 {args.mode} 未定義，執行預設掃描。")
        run_scan()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.critical(f"系統崩潰: {e}")
        send_tg(f"🚨 *系統異常報告*\n錯誤內容: `{str(e)[:100]}`")
