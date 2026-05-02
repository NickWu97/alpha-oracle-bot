#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle 回測框架 v1.0
══════════════════════════════════════════════════════════════════════
用法：
  python backtest.py BTC-USDT-SWAP            # 對單一幣種回測
  python backtest.py BTC-USDT-SWAP 200        # 用最近 200 根 K 線
  python backtest.py --all                    # 對所有幣種回測

說明：
  - 從 OKX 抓最近 N 根 15m K 線
  - 對每根 K 線當作「現在」重跑訊號邏輯
  - 模擬 SL/TP/BE/LOCK 觸發（用後續 K 線的 high/low）
  - 輸出：總訊號數、勝率、總 PnL、最大回撤、平均 R 倍數
══════════════════════════════════════════════════════════════════════
"""
import sys
import time
import os
import logging
from typing import Optional

# 重用 main.py 的所有指標與訊號邏輯
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as bot

logging.basicConfig(level=logging.WARNING)


def fetch_history(instId: str, total_bars: int = 500, tf: str = "15m") -> list:
    """抓更深的歷史 K 線（OKX 一次 100，需分頁）"""
    import requests
    out = []
    after = ""
    while len(out) < total_bars:
        try:
            url = f"https://www.okx.com/api/v5/market/history-candles?instId={instId}&bar={tf}&limit=100"
            if after:
                url += f"&after={after}"
            res = requests.get(url, timeout=10).json()
            if res.get("code") != "0":
                break
            data = res.get("data", [])
            if not data:
                break
            out.extend(data)
            after = data[-1][0]
            time.sleep(0.1)
        except Exception as e:
            print(f"⚠️ 抓 {instId} 失敗：{e}")
            break

    candles = []
    for r in out:
        try:
            candles.append({
                "ts": int(r[0]),
                "o": float(r[1]),
                "h": float(r[2]),
                "l": float(r[3]),
                "c": float(r[4]),
                "v": float(r[5]),
            })
        except Exception:
            continue
    candles.sort(key=lambda x: x["ts"])
    return candles[:total_bars]


def simulate_trade(entry_idx: int, candles: list, signal: dict) -> dict:
    """從 entry_idx 開始模擬訊號到結束，回傳結果"""
    side = signal["side"]
    entry = signal["entry"]
    sl = signal["sl"]
    tp1, tp2, tp3 = signal["tp1"], signal["tp2"], signal["tp3"]
    hit_tp1 = hit_tp2 = False

    max_lookahead = min(96, len(candles) - entry_idx - 1)  # 最多看 24 小時
    for j in range(entry_idx + 1, entry_idx + 1 + max_lookahead):
        c = candles[j]
        # 同一根 K：先 TP1 → TP2 → TP3 → SL（用更新後的 SL）
        if side == "LONG":
            if not hit_tp1 and c["h"] >= tp1:
                hit_tp1 = True
                sl = entry  # 移到 BE
            if not hit_tp2 and c["h"] >= tp2:
                hit_tp2 = True
                sl = tp1
            if c["h"] >= tp3:
                return {"close_type": "TP3", "pnl_r": 5.0, "bars": j - entry_idx}
            if c["l"] <= sl:
                if hit_tp2:
                    return {"close_type": "LOCK", "pnl_r": 1.5, "bars": j - entry_idx}
                if hit_tp1:
                    return {"close_type": "BE", "pnl_r": 0.0, "bars": j - entry_idx}
                return {"close_type": "SL", "pnl_r": -1.0, "bars": j - entry_idx}
        else:
            if not hit_tp1 and c["l"] <= tp1:
                hit_tp1 = True
                sl = entry
            if not hit_tp2 and c["l"] <= tp2:
                hit_tp2 = True
                sl = tp1
            if c["l"] <= tp3:
                return {"close_type": "TP3", "pnl_r": 5.0, "bars": j - entry_idx}
            if c["h"] >= sl:
                if hit_tp2:
                    return {"close_type": "LOCK", "pnl_r": 1.5, "bars": j - entry_idx}
                if hit_tp1:
                    return {"close_type": "BE", "pnl_r": 0.0, "bars": j - entry_idx}
                return {"close_type": "SL", "pnl_r": -1.0, "bars": j - entry_idx}
    return {"close_type": "TIMEOUT", "pnl_r": 0.0, "bars": max_lookahead}


def backtest_coin(instId: str, total_bars: int = 500) -> dict:
    """對單一幣種回測"""
    print(f"\n📥 抓取 {instId} 最近 {total_bars} 根 15m K 線...")
    candles = fetch_history(instId, total_bars)
    if len(candles) < 100:
        print(f"  ⚠️ 資料不足（只有 {len(candles)} 根）")
        return {"instId": instId, "n": 0}
    print(f"  ✅ 取得 {len(candles)} 根 K 線")

    # patch fetch_mtf_trend：給回測用，直接用同 K 線推算 1H/4H 趨勢（簡化）
    bot._mtf_cache = {}
    orig_fetch_mtf = bot.fetch_mtf_trend
    bot.fetch_mtf_trend = lambda x: {
        "1H": {"supertrend": 0, "trend": "side", "rsi": 50},
        "4H": {"supertrend": 0, "trend": "side", "rsi": 50},
    }

    trades = []
    last_signal_idx = -10  # 簡單冷卻：8 根 K 線（2 小時）內不重複開
    for i in range(50, len(candles) - 1):
        if i - last_signal_idx < 8:
            continue
        df_so_far = candles[: i + 1]
        cur_price = candles[i]["c"]
        try:
            sig = bot.generate_signal(
                instId, df_so_far, cur_price,
                funding_rate=None,
                score_threshold=68,
                atr_max_pct=0.04,
                signal_expire_hours=24,
            )
        except Exception:
            continue
        if not sig:
            continue

        result = simulate_trade(i, candles, sig)
        trades.append({
            "side": sig["side"], "score": sig["score"],
            "entry": sig["entry"], "sl": sig["sl"],
            "tp1": sig["tp1"], "tp2": sig["tp2"], "tp3": sig["tp3"],
            **result,
        })
        last_signal_idx = i

    bot.fetch_mtf_trend = orig_fetch_mtf

    if not trades:
        print(f"  📭 此區間沒產生訊號")
        return {"instId": instId, "n": 0}

    n = len(trades)
    win = sum(1 for t in trades if t["close_type"] in ("TP1", "TP2", "TP3", "LOCK"))
    loss = sum(1 for t in trades if t["close_type"] == "SL")
    be = sum(1 for t in trades if t["close_type"] == "BE")
    timeout = sum(1 for t in trades if t["close_type"] == "TIMEOUT")
    total_r = sum(t["pnl_r"] for t in trades)

    # 計算最大回撤
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        eq += t["pnl_r"]
        peak = max(peak, eq)
        max_dd = min(max_dd, eq - peak)

    print(f"\n📊 *{instId} 回測結果*")
    print(f"  訊號數：{n}")
    print(f"  勝 / 平 / 敗 / 超時：{win} / {be} / {loss} / {timeout}")
    print(f"  勝率：{win / max(n, 1) * 100:.1f}%")
    print(f"  總 R：{total_r:+.1f}R")
    print(f"  平均：{total_r / max(n, 1):+.2f}R/筆")
    print(f"  最大回撤：{max_dd:.1f}R")

    return {
        "instId": instId, "n": n, "win": win, "loss": loss, "be": be,
        "timeout": timeout, "total_r": total_r, "max_dd": max_dd,
    }


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    if args[0] == "--all":
        all_results = []
        for c in bot.ALL_COINS:
            r = backtest_coin(c, 500)
            all_results.append(r)

        # 彙總
        total_n = sum(r.get("n", 0) for r in all_results)
        total_r = sum(r.get("total_r", 0) for r in all_results)
        total_win = sum(r.get("win", 0) for r in all_results)
        print(f"\n{'='*50}")
        print(f"📈 *彙總（{len(all_results)} 幣種）*")
        print(f"  總訊號：{total_n}")
        print(f"  總勝：{total_win} / 勝率 {total_win / max(total_n, 1) * 100:.1f}%")
        print(f"  總 R：{total_r:+.1f}R")
        print(f"  平均：{total_r / max(total_n, 1):+.2f}R/筆")
    else:
        instId = args[0]
        bars = int(args[1]) if len(args) > 1 else 500
        backtest_coin(instId, bars)


if __name__ == "__main__":
    main()
