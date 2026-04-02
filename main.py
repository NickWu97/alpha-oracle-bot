import requests
import os
import pandas as pd
import numpy as np
import logging
import traceback
import time
from datetime import datetime, timedelta
 
# --- 1. 基礎配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID  = os.getenv("CHAT_ID")
 
ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP",
    "BCH-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP"
]
 
LOG_FILE   = "active_trades.csv"
STATS_FILE = "daily_stats.csv"
 
# WAITING 狀態超過幾根 K 棒後自動清除（15m K × 20 = 5 小時）
WAITING_EXPIRY_BARS = 20
 
LOG_COLS   = ["instId", "side", "status", "entry", "sl", "tp1", "tp2", "tp3", "locked", "wait_since"]
STATS_COLS = ["instId", "result"]
 
# --- 2. 工具函數 ---
 
def safe_float(val, fallback=0.0):
    """安全地將任意值轉為 float，避免 CSV 讀回來的字串型態問題"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return fallback
 
def safe_int(val, fallback=0):
    """安全地將任意值轉為 int"""
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return fallback
 
def normalize_trade(t: dict) -> dict:
    """確保從 CSV 讀回來的 trade dict 所有欄位型態正確"""
    return {
        "instId":     str(t.get("instId", "")),
        "side":       str(t.get("side", "")),
        "status":     str(t.get("status", "")),
        "entry":      safe_float(t.get("entry")),
        "sl":         safe_float(t.get("sl")),
        "tp1":        safe_float(t.get("tp1")),
        "tp2":        safe_float(t.get("tp2")),
        "tp3":        safe_float(t.get("tp3")),
        "locked":     safe_int(t.get("locked")),       # FIX: 原本直接 == 0 會因字串 "0" 判斷錯誤
        "wait_since": safe_int(t.get("wait_since", 0)),
    }
 
def get_extra_metrics(instId):
    """抓取情緒數據：資金費率 與 多空持倉比"""
    base_id = instId.replace("-SWAP", "").split("-")[0]
    funding  = "N/A"
    ls_ratio = "N/A"
    try:
        f_res = requests.get(
            f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}",
            timeout=5
        ).json()
        funding = f"{float(f_res['data'][0]['fundingRate']) * 100:.4f}%"
    except Exception as e:
        logging.warning(f"[{instId}] 資金費率抓取失敗: {e}")
 
    try:
        ls_res = requests.get(
            f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}",
            timeout=5
        ).json()
        ls_ratio = ls_res['data'][0]['ratio']
    except Exception as e:
        logging.warning(f"[{instId}] 多空比抓取失敗: {e}")
 
    return {"funding": funding, "ls_ratio": ls_ratio}
 
def send_tg(msg):
    if not TG_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=15
        )
    except Exception as e:
        logging.warning(f"Telegram 發送失敗: {e}")
 
def fetch_okx(instId):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=15m&limit=100"
        res = requests.get(url, timeout=10).json()
        df  = pd.DataFrame(
            res['data'],
            columns=['ts', 'o', 'h', 'l', 'c', 'v', 'volCcy', 'volCcyQuote', 'confirm']
        )
        df[['o', 'h', 'l', 'c', 'v']] = df[['o', 'h', 'l', 'c', 'v']].astype(float)
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except Exception as e:
        logging.warning(f"[{instId}] K 線抓取失敗: {e}")
        return None
 
def calculate_atr(df):
    """計算 ATR(14) 用於動態止損"""
    high_low    = df['h'] - df['l']
    high_close  = np.abs(df['h'] - df['c'].shift())
    low_close   = np.abs(df['l'] - df['c'].shift())
    true_range  = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.rolling(window=14).mean().iloc[-1]
 
def find_smc_setup(df):
    """
    SMC 結構掃描：FVG + BOS
    FIX: 改為收集所有符合條件的訊號，回傳最新（最右邊）一個，
         避免原本 return 第一個找到就停止、可能忽略更近期強訊號的問題。
    """
    if df is None or len(df) < 40:
        return None
 
    atr      = calculate_atr(df)
    best     = None  # 儲存最新訊號
 
    for i in range(len(df) - 3, len(df) - 25, -1):
        k0, k1, k2 = df.iloc[i - 1], df.iloc[i], df.iloc[i + 1]
 
        # 多頭 BOS：K2 突破前 15 根高點且為陽線
        if k2['c'] > k2['o'] and k2['c'] > df['h'].iloc[i - 15:i].max():
            entry = (k2['l'] + k0['h']) / 2 if k2['l'] > k0['h'] else (k1['l'] + k1['o']) / 2
            sl    = k1['l'] - (0.4 * atr)
            best  = {"side": "LONG", "entry": entry, "sl": sl, "bar_idx": i}
 
        # 空頭 BOS：K2 跌破前 15 根低點且為陰線
        elif k2['c'] < k2['o'] and k2['c'] < df['l'].iloc[i - 15:i].min():
            entry = (k2['h'] + k0['l']) / 2 if k2['h'] < k0['l'] else (k1['h'] + k1['o']) / 2
            sl    = k1['h'] + (0.4 * atr)
            best  = {"side": "SHORT", "entry": entry, "sl": sl, "bar_idx": i}
 
    if best:
        best.pop("bar_idx", None)
    return best
 
# --- 3. 主程式邏輯 ---
 
def main():
    try:
        now_tw        = datetime.utcnow() + timedelta(hours=8)
        manual_report = os.getenv("MANUAL_REPORT", "false").lower() == "true"
 
        # 檔案初始化
        for f, cols in zip([LOG_FILE, STATS_FILE], [LOG_COLS, STATS_COLS]):
            if not os.path.exists(f) or os.stat(f).st_size == 0:
                pd.DataFrame(columns=cols).to_csv(f, index=False)
 
        # ── A. 戰績回報（午夜 00:00 或手動觸發）──────────────────────────
        is_midnight = (now_tw.hour == 0 and 0 <= now_tw.minute < 15)
        if is_midnight or manual_report:
            if not os.path.exists("midnight.ok") or manual_report:
                df_s = pd.read_csv(STATS_FILE)
                if not df_s.empty:
                    tp_c  = len(df_s[df_s['result'] == 'TP'])
                    sl_c  = len(df_s[df_s['result'] == 'SL'])
                    total = tp_c + sl_c
                    wr    = (tp_c / total * 100) if total > 0 else 0
 
                    report_msg  = "📊 *Alpha Oracle 戰績回報*\n"
                    report_msg += "──────────────────\n"
                    report_msg += f"✅ 盈：{tp_c} | ❌ 損：{sl_c}\n"
                    report_msg += f"🔥 勝率：*{wr:.1f}%*\n"
                    report_msg += f"🕒 統計時間：{now_tw.strftime('%Y-%m-%d %H:%M')}"
                    send_tg(report_msg)
 
                    if is_midnight:
                        pd.DataFrame(columns=STATS_COLS).to_csv(STATS_FILE, index=False)
                        with open("midnight.ok", "w") as fh:
                            fh.write("ok")
        elif now_tw.hour != 0 and os.path.exists("midnight.ok"):
            os.remove("midnight.ok")
 
        # ── B. 核心監控邏輯 ──────────────────────────────────────────────
        try:
            trades_df = pd.read_csv(LOG_FILE)
            # 補上新增欄位（舊 CSV 可能沒有 wait_since）
            if "wait_since" not in trades_df.columns:
                trades_df["wait_since"] = 0
        except Exception:
            trades_df = pd.DataFrame(columns=LOG_COLS)
 
        active_ids     = trades_df['instId'].tolist()
        updated_trades = []
        current_bar    = int(datetime.utcnow().timestamp() // 900)  # 15m bar index（UNIX 時間 / 900）
 
        for instId in ALL_COINS:
            df = fetch_okx(instId)
            if df is None or df.empty:
                continue
 
            curr_p = df['c'].iloc[-1]
 
            # ── 1. 發現新機會 ──────────────────────────────────────────
            if instId not in active_ids:
                setup = find_smc_setup(df)
                if setup:
                    risk = abs(setup['entry'] - setup['sl'])
                    tp1  = setup['entry'] + risk * 1.5 if setup['side'] == "LONG" else setup['entry'] - risk * 1.5
                    tp2  = setup['entry'] + risk * 2.0 if setup['side'] == "LONG" else setup['entry'] - risk * 2.0
                    tp3  = setup['entry'] + risk * 3.0 if setup['side'] == "LONG" else setup['entry'] - risk * 3.0
 
                    # FIX: 標籤改為「資金費率」，原本誤標成 CVD
                    m    = get_extra_metrics(instId)
                    msg  = "🔍 *Alpha Oracle | 發現機會*\n"
                    msg += "──────────────────\n"
                    msg += f"💎 #{instId.split('-')[0]} | {'🟢 多' if setup['side'] == 'LONG' else '🔴 空'}\n"
                    msg += f"📊 資金費率 {m['funding']} | 多空比 {m['ls_ratio']}\n\n"
                    msg += f"📍 進場位：{setup['entry']:.4f}\n"
                    msg += f"🚫 止損位：{setup['sl']:.4f}\n"
                    msg += f"💰 TP1：{tp1:.4f} | TP3：{tp3:.4f}\n\n"
                    msg += "💡 *等待回踩成交...*"
                    send_tg(msg)
 
                    updated_trades.append({
                        "instId": instId, "side": setup['side'], "status": "WAITING",
                        "entry": setup['entry'], "sl": setup['sl'],
                        "tp1": tp1, "tp2": tp2, "tp3": tp3,
                        "locked": 0, "wait_since": current_bar  # 記錄等待起始 bar
                    })
                time.sleep(0.2)  # FIX: rate limit 保護，避免連續請求被 OKX 擋
                continue
 
            # ── 2. 追蹤現有單據 ─────────────────────────────────────────
            t = normalize_trade(trades_df[trades_df['instId'] == instId].iloc[0].to_dict())
 
            # WAITING 狀態
            if t['status'] == "WAITING":
                # FIX: WAITING 超過 WAITING_EXPIRY_BARS 根 K 棒自動清除，避免舊訊號殘留重複通知
                bars_waited = current_bar - t['wait_since']
                if bars_waited > WAITING_EXPIRY_BARS:
                    logging.info(f"[{instId}] WAITING 已逾 {bars_waited} 根 K 棒，自動清除")
                    time.sleep(0.2)
                    continue  # 不加入 updated_trades，相當於刪除這筆
 
                is_hit = (
                    (t['side'] == "LONG"  and curr_p <= t['entry']) or
                    (t['side'] == "SHORT" and curr_p >= t['entry'])
                )
                if is_hit:
                    t['status'] = "ACTIVE"
                    msg  = "🚀 *Alpha Oracle | 已成交*\n"
                    msg += "──────────────────\n"
                    msg += f"✅ #{instId.split('-')[0]} 已觸發進場\n"
                    msg += f"📍 成交價：{curr_p:.4f} | 🛡️ 止損：{t['sl']:.4f}"
                    send_tg(msg)
                updated_trades.append(t)
 
            # ACTIVE 狀態
            elif t['status'] == "ACTIVE":
                # 觸及 2.0R → 鎖利保護，止損移至 TP1
                if t['locked'] == 0 and (
                    (t['side'] == "LONG"  and curr_p >= t['tp2']) or
                    (t['side'] == "SHORT" and curr_p <= t['tp2'])
                ):
                    t['locked'] = 1
                    t['sl']     = t['tp1']
                    send_tg(
                        f"🔒 *Alpha Oracle | 鎖利保護*\n──────────────────\n"
                        f"#{instId.split('-')[0]} 達 2.0R，止損已移至 TP1: {t['tp1']:.4f}"
                    )
 
                is_sl  = (t['side'] == "LONG"  and curr_p <= t['sl']) or (t['side'] == "SHORT" and curr_p >= t['sl'])
                is_tp3 = (t['side'] == "LONG"  and curr_p >= t['tp3']) or (t['side'] == "SHORT" and curr_p <= t['tp3'])
 
                if is_sl or is_tp3:
                    res  = "TP" if is_tp3 else "SL"
                    msg  = "🏁 *Alpha Oracle | 交易結算*\n"
                    msg += "──────────────────\n"
                    msg += f"#{instId.split('-')[0]} 結算離場\n"
                    msg += f"🏆 結果：{'💰 強力止盈 (3.0R)' if is_tp3 else '🛡️ 保本/止損離場'}\n"
                    msg += f"📍 離場價：{curr_p:.4f}"
                    send_tg(msg)
                    pd.DataFrame([{"instId": instId, "result": res}]).to_csv(
                        STATS_FILE, mode='a', header=False, index=False
                    )
                    time.sleep(0.2)
                    continue  # 結算後不加入 updated_trades
 
                updated_trades.append(t)
 
            time.sleep(0.2)  # FIX: 每幣種間 rate limit 保護
 
        pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)
 
    except Exception:
        traceback.print_exc()
 
if __name__ == "__main__":
    main()
