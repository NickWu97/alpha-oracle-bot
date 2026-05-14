#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro v15.5 — Backtest
══════════════════════════════════════════════════════════════════════
🔬 回測 generate_signal 在歷史 K 線上的表現

用法：
    python3 backtest.py BTC-USDT-SWAP 7    # 回測 BTC 過去 7 天
    python3 backtest.py ALL 30              # 全部幣回測 30 天

注意：
- 用 OKX history-candles API（最多 100 根 × 多次分頁）
- 不模擬 MTF（會用當時 mtf snapshot 來算）
- 用 ⅓ 分批的 PnL 模型，跟 main.py 對齊
══════════════════════════════════════════════════════════════════════
"""
import os
import sys
import time
import logging
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from main import (  # noqa: E402
    ALL_COINS, load_config, _okx_get,
    generate_signal, generate_reversal_signal,
    calc_atr, calc_realized_r, calc_realized_usd,
    DEFAULT_CONFIG,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [BT] %(message)s")


# ═════════════════════════════════════════════════════════
# 抓歷史 K 線（分頁）
# ═════════════════════════════════════════════════════════
def fetch_history(instId: str, days: int, tf: str = "15m") -> list:
    """抓 N 天的 15m K 線，回傳由舊到新"""
    bars_per_day = {"1m": 1440, "5m": 288, "15m": 96, "1H": 24, "4H": 6}.get(tf, 96)
    target_bars = days * bars_per_day
    candles = []
    before_ts = ""  # 第一次留空抓最新
    while len(candles) < target_bars:
        url = (f"https://www.okx.com/api/v5/market/history-candles"
               f"?instId={instId}&bar={tf}&limit=100")
        if before_ts:
            url += f"&after={before_ts}"
        data = _okx_get(url, timeout=10)
        if not data or data.get("code") != "0":
            break
        rows = data.get("data", [])
        if not rows:
            break
        batch = []
        for row in rows:
            try:
                batch.append({
                    "ts": int(row[0]), "o": float(row[1]),
                    "h": float(row[2]), "l": float(row[3]),
                    "c": float(row[4]), "v": float(row[5]),
                    "confirmed": True,
                })
            except Exception:
                continue
        if not batch:
            break
        candles.extend(batch)
        before_ts = str(batch[-1]["ts"])  # 用最舊的 ts 翻頁
        time.sleep(0.2)  # rate limit friendly
    candles.sort(key=lambda x: x["ts"])
    # 去重
    seen = set()
    uniq = []
    for c in candles:
        if c["ts"] not in seen:
            seen.add(c["ts"])
            uniq.append(c)
    return uniq[:target_bars]


# ═════════════════════════════════════════════════════════
# 單筆訊號的回測模擬
# ═════════════════════════════════════════════════════════
def simulate_trade(sig: dict, future_candles: list, cfg: dict) -> dict | None:
    """從 sig['entry'] 開始往後跑，看打到 SL 還是 TP3"""
    side = sig["side"]
    entry = sig["entry"]
    sl = sig["sl"]
    tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
    max_hold_h = cfg.get("max_hold_hours", 48)
    max_bars = int(max_hold_h * 4)  # 15m × 4 = 1h
    conservative = cfg.get("conservative_sl_first", True)

    hit_tp1 = hit_tp2 = hit_tp3 = False
    current_sl = sl
    for i, c in enumerate(future_candles[:max_bars]):
        ch, cl = c["h"], c["l"]
        if side == "LONG":
            against = cl <= current_sl
            tp1_hit = ch >= tp1
            tp2_hit = ch >= tp2
            tp3_hit = ch >= tp3
        else:
            against = ch >= current_sl
            tp1_hit = cl <= tp1
            tp2_hit = cl <= tp2
            tp3_hit = cl <= tp3

        if conservative and against:
            return {"close_type": "SL" if not hit_tp1 else ("BE" if not hit_tp2 else "LOCK"),
                    "bars": i + 1}
        if not hit_tp1 and tp1_hit:
            hit_tp1 = True
            current_sl = entry
        if not hit_tp2 and tp2_hit:
            hit_tp2 = True
            current_sl = tp1
        if not hit_tp3 and tp3_hit:
            return {"close_type": "TP3", "bars": i + 1}
        if not conservative and against:
            return {"close_type": "SL" if not hit_tp1 else ("BE" if not hit_tp2 else "LOCK"),
                    "bars": i + 1}
    # 超時
    if hit_tp2: return {"close_type": "LOCK", "bars": max_bars}
    if hit_tp1: return {"close_type": "BE", "bars": max_bars}
    return {"close_type": "TIMEOUT", "bars": max_bars}


# ═════════════════════════════════════════════════════════
# 回測單一幣種
# ═════════════════════════════════════════════════════════
def backtest_coin(instId: str, days: int, cfg: dict) -> dict:
    candles = fetch_history(instId, days + 10)  # 多抓 10 天作為 lookback
    if len(candles) < 200:
        logging.warning(f"{instId} 資料不足（{len(candles)} 根）")
        return {"trades": [], "n": 0}

    logging.info(f"📊 {instId} 抓到 {len(candles)} 根 K 線")
    trades = []
    cooldown = {"LONG": 0, "SHORT": 0}
    cooldown_bars = int(cfg.get("cooldown_hours", 2) * 4)

    # 從第 200 根開始（讓 EMA200 有數據）
    for i in range(200, len(candles) - 4):
        df = candles[: i + 1]
        current_price = df[-1]["c"]
        ts = df[-1]["ts"]

        # cooldown
        if cooldown["LONG"] > i and cooldown["SHORT"] > i:
            continue

        sig = generate_signal(instId, df, current_price, None, cfg)
        if not sig:
            sig = generate_reversal_signal(instId, df, current_price, None, cfg)
        if not sig:
            continue

        side = sig["side"]
        if cooldown[side] > i:
            continue

        future = candles[i + 1:]
        result = simulate_trade(sig, future, cfg)
        if result is None:
            continue

        close_type = result["close_type"]
        if close_type == "TIMEOUT":
            close_type = "BE" if (current_price > sig["entry"] if side == "LONG"
                                  else current_price < sig["entry"]) else "SL"

        tp_r = sig.get("tp_r_ratios", [1.5, 3.0, 5.0])
        realized_r = calc_realized_r(close_type, tp_r)
        sig["sl_original"] = sig["sl"]
        realized_usd = calc_realized_usd(sig, close_type)

        trades.append({
            "ts": ts,
            "time": datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M"),
            "side": side,
            "close_type": close_type,
            "realized_r": realized_r,
            "realized_usd": realized_usd,
            "score": sig["score"],
            "mode": sig.get("mode", "trend"),
            "bars": result["bars"],
        })

        cooldown[side] = i + cooldown_bars

    return {"trades": trades, "n": len(trades)}


def summarize(trades: list) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0}
    wins = sum(1 for t in trades if t["close_type"] in ("TP3", "LOCK"))
    losses = sum(1 for t in trades if t["close_type"] == "SL")
    bes = sum(1 for t in trades if t["close_type"] == "BE")
    total_r = sum(t["realized_r"] for t in trades)
    total_usd = sum(t["realized_usd"] for t in trades)
    return {
        "n": n, "wins": wins, "losses": losses, "bes": bes,
        "wr": wins / n * 100,
        "total_r": total_r,
        "avg_r": total_r / n,
        "total_usd": total_usd,
        "max_win_r": max((t["realized_r"] for t in trades), default=0),
        "max_loss_r": min((t["realized_r"] for t in trades), default=0),
    }


def main():
    if len(sys.argv) < 2:
        print("用法：python3 backtest.py <COIN|ALL> [DAYS]")
        print("範例：python3 backtest.py BTC-USDT-SWAP 7")
        print("範例：python3 backtest.py ALL 30")
        sys.exit(1)

    target = sys.argv[1]
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 7

    cfg = load_config()
    coins = ALL_COINS if target == "ALL" else [target]

    all_trades = []
    by_coin = {}

    for c in coins:
        result = backtest_coin(c, days, cfg)
        by_coin[c] = result
        all_trades.extend([dict(t, coin=c.split("-")[0]) for t in result["trades"]])

    print("\n" + "=" * 60)
    print(f"📊 回測結果（過去 {days} 天）")
    print("=" * 60)

    for c, result in by_coin.items():
        s = summarize(result["trades"])
        coin = c.split("-")[0]
        if s["n"] == 0:
            print(f"  {coin:6s}：無訊號")
            continue
        print(f"  {coin:6s}：{s['n']:3d} 筆 / 勝 {s['wins']:2d} 平 {s['bes']:2d} 敗 {s['losses']:2d}"
              f" / 勝率 {s['wr']:5.1f}% / 總 R {s['total_r']:+6.1f} / 總 ${s['total_usd']:+7.0f}")

    print()
    overall = summarize(all_trades)
    if overall["n"] > 0:
        print(f"  整體：{overall['n']} 筆 / 勝率 {overall['wr']:.1f}% / "
              f"總 R {overall['total_r']:+.1f} / 平均 R {overall['avg_r']:+.2f} / "
              f"總 ${overall['total_usd']:+.0f}")
        print(f"  最大獲利 R：{overall['max_win_r']:+.2f}")
        print(f"  最大虧損 R：{overall['max_loss_r']:+.2f}")

    print("=" * 60)


if __name__ == "__main__":
    main()
