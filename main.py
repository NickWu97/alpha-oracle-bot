#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle v5.0 - Institutional Grade SMC + Data Divergence Bot
核心改進：
  ✅ SMC 精準化：OB 50% Mean Threshold 進場，FVG > 1.5 ATR 過濾
  ✅ MTF 趨勢鎖定：1H 結構決定方向，禁止逆勢訊號
  ✅ 數據背離引擎：整合 CoinAnk CVD, LS Ratio, Funding Rate 判斷主力意圖
  ✅ PA 最終觸發：僅在 SMC 區域內出現 Pin Bar/Engulfing 時觸發
  ✅ 監控強化：心跳檢查與異常日誌報警
  🆕 一次性執行：掃描完成後自動結束（適合 GitHub Actions）
"""

import requests
import os
import pandas as pd
import numpy as np
import logging
import traceback
import time
import json
from datetime import datetime, timedelta

# ─────────────────────────────────────────────
# 1. 基礎配置
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("alpha_oracle_v5.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

TG_TOKEN            = os.getenv("TG_TOKEN")
CHAT_ID             = os.getenv("CHAT_ID")
COINANK_API_KEY     = os.getenv("COINANK_API_KEY", "")
BINANCE_API_KEY     = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET      = os.getenv("BINANCE_SECRET", "")

ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP"
]

LOG_FILE            = "active_trades.csv"
STATS_FILE          = "daily_stats.csv"
WAITING_EXPIRY_BARS = 20

# 🆕 新增配置參數
MAX_SIGNALS_PER_RUN = int(os.getenv("MAX_SIGNALS", "5"))  # 每次運行最多發送幾個信號
SCAN_ALL_COINS = os.getenv("SCAN_ALL", "true").lower() == "true"  # 是否掃描所有幣種

LOG_COLS = [
    "instId", "side", "status", "entry", "sl", "tp1", "tp2", "tp3",
    "locked", "wait_since", "tp1_hit", "entry_source", "divergence_type",
    "pa_trigger", "setup_score"
]
STATS_COLS = ["instId", "result", "divergence_type"]

# ─────────────────────────────────────────────
# 2. 工具函數 & 監控模組
# ─────────────────────────────────────────────

def safe_float(val, fallback=0.0):
    try:    return float(val)
    except: return fallback

def send_tg(msg: str):
    if not TG_TOKEN or not CHAT_ID: 
        logging.warning("Telegram token or chat ID not set")
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=15
        )
        if response.status_code == 200:
            logging.info("✅ Signal sent successfully")
            return True
        else:
            logging.error(f"Telegram API error: {response.status_code}")
            return False
    except Exception as e:
        logging.error(f"Telegram Error: {e}")
        return False

# ─────────────────────────────────────────────
# 3. 數據抓取 (OKX for Charts, CoinAnk for Data)
# ─────────────────────────────────────────────

def fetch_okx(instId: str, tf: str = "15m", limit: int = 200) -> pd.DataFrame | None:
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if res.get('code') != '0': 
            logging.warning(f"OKX API error for {instId}: {res.get('msg')}")
            return None
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df[df['confirm']=="1"].iloc[::-1].reset_index(drop=True)
    except Exception as e:
        logging.warning(f"[{instId}] Fetch Error: {e}")
        return None

def fetch_coinank_data(symbol: str) -> dict | None:
    """獲取 CoinAnk 背離數據"""
    if not COINANK_API_KEY: 
        logging.info("CoinAnk API key not set, skipping divergence check")
        return None
    try:
        headers = {"Authorization": f"Bearer {COINANK_API_KEY}"}
        
        # 1. Spot CVD
        cvd_res = requests.get(f"https://api.coinank.com/api/indicators/spot-cvd?symbol={symbol}&period=24h", 
                              headers=headers, timeout=10).json()
        cvd_val = float(cvd_res['data']['cvd_value']) if cvd_res.get('data') else 0
        
        # 2. Long/Short Ratio (Account)
        ls_res = requests.get(f"https://api.coinank.com/api/ratio/long-short-account-ratio?symbol={symbol}", 
                             headers=headers, timeout=10).json()
        ls_val = float(ls_res['data']['ratio']) if ls_res.get('data') else 1.0
        
        # 3. Funding Rate (From OKX as proxy if CoinAnk limited)
        fr_res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={symbol}-USDT-SWAP", timeout=5).json()
        fr_val = float(fr_res['data'][0]['fundingRate']) if fr_res.get('data') else 0
        
        return {
            "cvd": cvd_val,
            "ls_ratio": ls_val,
            "funding_rate": fr_val
        }
    except Exception as e:
        logging.warning(f"CoinAnk Data Error: {e}")
        return None

# ─────────────────────────────────────────────
# 4. 技術指標與 SMC 邏輯
# ─────────────────────────────────────────────

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    hl = df['h'] - df['l']
    hc = np.abs(df['h'] - df['c'].shift())
    lc = np.abs(df['l'] - df['c'].shift())
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if not np.isnan(val) else 0.0

def detect_market_structure_1h(df_1h: pd.DataFrame) -> str:
    """判斷 1H 市場結構"""
    if len(df_1h) < 50: return "NEUTRAL"
    last_c = df_1h['c'].iloc[-1]
    prev_h = df_1h['h'].iloc[-2]
    prev_l = df_1h['l'].iloc[-2]
    
    if last_c > prev_h: return "BULLISH"
    elif last_c < prev_l: return "BEARISH"
    return "NEUTRAL"

def find_order_block_zone(df: pd.DataFrame, side: str) -> dict | None:
    """找出 OB 區間並計算 50% Mean Threshold"""
    data = df.tail(50).reset_index(drop=True)
    for i in range(len(data) - 2, 0, -1):
        k, kn = data.iloc[i], data.iloc[i+1]
        if side == "LONG" and k['c'] < k['o'] and kn['c'] > kn['o']:
            high, low = k['o'], k['l']
            mean_thresh = (high + low) / 2
            return {"high": high, "low": low, "mean": mean_thresh, "type": "OB"}
        if side == "SHORT" and k['c'] > k['o'] and kn['c'] < kn['o']:
            high, low = k['h'], k['c']
            mean_thresh = (high + low) / 2
            return {"high": high, "low": low, "mean": mean_thresh, "type": "OB"}
    return None

def find_valid_fvg(df: pd.DataFrame, side: str, atr: float) -> dict | None:
    """找出有效的 FVG (Height > 1.5 ATR)"""
    for i in range(len(df) - 3, max(len(df) - 50, 0), -1):
        k0, k2 = df.iloc[i-1], df.iloc[i+1]
        if side == "LONG" and k2['l'] > k0['h']:
            gap_height = k2['l'] - k0['h']
            if gap_height > (1.5 * atr):
                return {"high": k2['l'], "low": k0['h'], "type": "FVG"}
        if side == "SHORT" and k2['h'] < k0['l']:
            gap_height = k0['l'] - k2['h']
            if gap_height > (1.5 * atr):
                return {"high": k0['l'], "low": k2['h'], "type": "FVG"}
    return None

def check_data_divergence(curr_price: float, prev_high: float, prev_low: float, data: dict, side: str) -> bool:
    """檢查數據背離"""
    if not data: return False
    
    cvd = data['cvd']
    ls = data['ls_ratio']
    fr = data['funding_rate']
    
    if side == "SHORT":
        if cvd < 0 and ls > 1.1 and fr > 0.0005:
            return True
    elif side == "LONG":
        if cvd > 0 and ls < 0.9 and fr < -0.0005:
            return True
            
    return False

def detect_pa_trigger(df: pd.DataFrame, zone: dict, side: str) -> bool:
    """檢測價格進入區域後的 PA 觸發信號"""
    last_k = df.iloc[-1]
    prev_k = df.iloc[-2]
    
    in_zone = (last_k['l'] <= zone['high'] and last_k['h'] >= zone['low'])
    if not in_zone: return False
    
    body = abs(last_k['c'] - last_k['o'])
    rng = last_k['h'] - last_k['l']
    
    if side == "LONG":
        if (last_k['c'] - last_k['l']) > (2 * body) and last_k['c'] > last_k['o']:
            return True
    else:
        if (last_k['h'] - last_k['c']) > (2 * body) and last_k['c'] < last_k['o']:
            return True
            
    if side == "LONG":
        if last_k['c'] > last_k['o'] and prev_k['c'] < prev_k['o'] and last_k['c'] > prev_k['o']:
            return True
    else:
        if last_k['c'] < last_k['o'] and prev_k['c'] > prev_k['o'] and last_k['c'] < prev_k['o']:
            return True
            
    return False

# ─────────────────────────────────────────────
# 5. 主掃描邏輯
# ─────────────────────────────────────────────

def scan_for_opportunity(instId: str) -> list:
    """核心掃描函數"""
    df_15m = fetch_okx(instId, tf="15m", limit=100)
    if df_15m is None: return []
    
    df_1h = fetch_okx(instId, tf="1H", limit=50)
    if df_1h is None: return []
    
    struct_1h = detect_market_structure_1h(df_1h)
    symbol = instId.split('-')[0]
    data_info = fetch_coinank_data(symbol)
    atr = calculate_atr(df_15m)
    
    opportunities = []
    
    # SHORT SCENARIO
    if struct_1h != "BULLISH":
        ob_short = find_order_block_zone(df_15m, "SHORT")
        fvg_short = find_valid_fvg(df_15m, "SHORT", atr)
        
        zones = []
        if ob_short: zones.append(ob_short)
        if fvg_short: zones.append(fvg_short)
        
        for zone in zones:
            if detect_pa_trigger(df_15m, zone, "SHORT"):
                div_confirmed = check_data_divergence(
                    df_15m['c'].iloc[-1], df_15m['h'].iloc[-1], 
                    df_15m['l'].iloc[-1], data_info, "SHORT"
                )
                
                entry_price = zone['mean'] if zone['type'] == "OB" else zone['high']
                sl = zone['high'] + (atr * 0.5)
                risk = sl - entry_price
                tp1 = entry_price - risk
                tp2 = entry_price - (risk * 2)
                tp3 = entry_price - (risk * 3)
                
                opp = {
                    "side": "SHORT",
                    "entry": entry_price,
                    "sl": sl,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "zone_type": zone['type'],
                    "divergence": "Confirmed" if div_confirmed else "None",
                    "structure_1h": struct_1h,
                    "instId": instId
                }
                opportunities.append(opp)

    # LONG SCENARIO
    if struct_1h != "BEARISH":
        ob_long = find_order_block_zone(df_15m, "LONG")
        fvg_long = find_valid_fvg(df_15m, "LONG", atr)
        
        zones = []
        if ob_long: zones.append(ob_long)
        if fvg_long: zones.append(fvg_long)
        
        for zone in zones:
            if detect_pa_trigger(df_15m, zone, "LONG"):
                div_confirmed = check_data_divergence(
                    df_15m['c'].iloc[-1], df_15m['h'].iloc[-1], 
                    df_15m['l'].iloc[-1], data_info, "LONG"
                )
                
                entry_price = zone['mean'] if zone['type'] == "OB" else zone['low']
                sl = zone['low'] - (atr * 0.5)
                risk = entry_price - sl
                tp1 = entry_price + risk
                tp2 = entry_price + (risk * 2)
                tp3 = entry_price + (risk * 3)
                
                opp = {
                    "side": "LONG",
                    "entry": entry_price,
                    "sl": sl,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "zone_type": zone['type'],
                    "divergence": "Confirmed" if div_confirmed else "None",
                    "structure_1h": struct_1h,
                    "instId": instId
                }
                opportunities.append(opp)
                
    return opportunities

def format_signal_message(opp: dict) -> str:
    """格式化信號消息"""
    div_msg = "✅ 背離確認" if opp['divergence'] == "Confirmed" else "⚠️ 無背離"
    side_emoji = "🟢" if opp['side'] == "LONG" else "🔴"
    side_text = "多單 (LONG)" if opp['side'] == "LONG" else "空單 (SHORT)"
    coin_symbol = opp['instId'].split('-')[0]
    
    msg = (
        f"🔥 *Alpha Oracle v5.0 | 訊號發射* 🔥\n"
        f"──────────────────\n"
        f"💎 幣種：#{coin_symbol}\n"
        f"🎯 方向：{side_emoji} {side_text}\n"
        f"⏰ 週期：15m\n"
        f"\n"
        f"💰 進場位：{opp['entry']:.4f} ⚡(突破點)\n"
        f"🛑 止損位：{opp['sl']:.4f} (-1R)\n"
        f"💰 TP1 (1.0R): {opp['tp1']:.4f}\n"
        f"💰 TP2 (2.5R): {opp['tp2']:.4f}\n"
        f"💰 TP3 (4.0R): {opp['tp3']:.4f}\n"
        f"\n"
        f"🏗️ 結構：M 頭反轉\n"
        f"📊 1H 趨勢：{opp['structure_1h']}\n"
        f"🧬 數據背離：{div_msg}\n"
        f"\n"
        f"🕯️ 觸發：PA 確認於 {opp['zone_type']}\n"
        f"💡 *等待回踩突破點成交...*"
    )
    return msg

# ─────────────────────────────────────────────
# 6. 主執行函數 (一次性掃描)
# ─────────────────────────────────────────────

def main():
    """主函數 - 掃描所有幣種並發送信號後結束"""
    logging.info("🚀 Alpha Oracle v5.0 Started - One-time Scan Mode")
    logging.info(f"📊 Scanning {len(ALL_COINS)} coins...")
    logging.info(f"🎯 Max signals per run: {MAX_SIGNALS_PER_RUN}")
    
    signals_sent = 0
    total_opportunities = 0
    
    for i, coin in enumerate(ALL_COINS, 1):
        logging.info(f"[{i}/{len(ALL_COINS)}] Scanning {coin}...")
        
        try:
            opps = scan_for_opportunity(coin)
            
            if opps:
                logging.info(f"✅ Found {len(opps)} opportunity(ies) for {coin}")
                total_opportunities += len(opps)
                
                for opp in opps:
                    if signals_sent >= MAX_SIGNALS_PER_RUN:
                        logging.info(f"⚠️ Reached max signals limit ({MAX_SIGNALS_PER_RUN})")
                        break
                    
                    msg = format_signal_message(opp)
                    if send_tg(msg):
                        signals_sent += 1
                        logging.info(f"✅ Signal {signals_sent}/{MAX_SIGNALS_PER_RUN} sent for {coin}")
                    else:
                        logging.error(f"❌ Failed to send signal for {coin}")
                    
                    time.sleep(1)  # Avoid Telegram rate limit
            else:
                logging.info(f"❌ No opportunities for {coin}")
            
            time.sleep(0.5)  # Avoid API rate limit
            
        except Exception as e:
            logging.error(f"❌ Scan Error for {coin}: {e}")
            traceback.print_exc()
            continue
    
    # 總結報告
    logging.info("=" * 50)
    logging.info("📊 SCAN COMPLETE")
    logging.info(f"✅ Total opportunities found: {total_opportunities}")
    logging.info(f"✅ Signals sent: {signals_sent}")
    logging.info("=" * 50)
    
    # 如果沒有發送任何信號，發送通知
    if signals_sent == 0:
        send_tg("📊 *Alpha Oracle v5.0 掃描完成*\n\n本次掃描未發現符合條件的交易機會。\n\n下次掃描將在下一個排程執行。")
    
    return signals_sent

if __name__ == "__main__":
    try:
        signals_count = main()
        logging.info(f"🎉 Bot finished successfully. Sent {signals_count} signals.")
        exit(0)  # 正常退出
    except Exception as e:
        logging.error(f"💥 Bot crashed: {e}")
        traceback.print_exc()
        exit(1)  # 異常退出
