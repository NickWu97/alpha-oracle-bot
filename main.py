#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro v11.0 — 專業版 (Production-Grade)
══════════════════════════════════════════════════════════════════════
🔧 v11.0 重大修正 (相對 v10.8)：
  ❌ v10.8 Bug: `_dev(tp3) > 0.003` 永遠為 True，導致開倉瞬間就假觸發 TP3
  ✅ v11.0: 嚴格價格比較，杜絕假訊號
  ✅ 階梯式觸發: TP1 → TP2 → TP3 依序，每達成一個發一則訊息
  ✅ 觸發訊息顯示「實際當下價」而非 TP 目標值
  ✅ 訊息精簡化 (專業交易員風格)
  ✅ 每次判斷都拉即時價 (禁用價格快取)，避免延遲假訊號
  ✅ 日誌含完整觸發軌跡，便於事後稽核

專業版本原則：複雜運算在後台，前台只傳簡潔通知。
══════════════════════════════════════════════════════════════════════
"""
import requests
import os
import json
import logging
import time
import sys
import uuid
from datetime import datetime, timezone, timedelta


# ─────────────────────────────────────────────────────────
# 🔧 環境變數
# ─────────────────────────────────────────────────────────
def _get_env(key, default=""):
    val = os.getenv(key)
    return val.strip() if val and val.strip() else default


def _get_env_int(key, default):
    val = os.getenv(key)
    try:
        return int(val.strip()) if val and val.strip() else default
    except Exception:
        return default


# ─────────────────────────────────────────────────────────
# 1. 基礎配置
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)

TG_TOKEN = _get_env("TG_TOKEN")
CHAT_ID = _get_env("CHAT_ID")

ALL_COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
MAX_SIGNALS = _get_env_int("MAX_SIGNALS", 3)
SCORE_THRESHOLD = _get_env_int("SETUP_SCORE_THRESHOLD", 68)
SIGNAL_EXPIRE_HOURS = 24

ACTIVE_SIGNALS_FILE = "active_signals.json"
TRADE_HISTORY_FILE = "trade_history.json"

# 🔹 進場區間容差 (0.6% 內視為進場)
ENTRY_ZONE_TOLERANCE = 0.006

_signal_cooldown = {}


# ─────────────────────────────────────────────────────────
# 2. 台灣時間工具
# ─────────────────────────────────────────────────────────
def get_tw_time() -> str:
    """🕐 台灣時間字串 (UTC+8)"""
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S TW")


def get_tw_date() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────
# 3. 通知系統 (線層回覆 + 按鈕)
# ─────────────────────────────────────────────────────────
def send_tg(msg: str, parse_mode: str = "Markdown", reply_to_id: int = None, buttons: list = None) -> int:
    """📤 發送 Telegram 訊息，回傳 message_id"""
    if not TG_TOKEN or not CHAT_ID:
        logging.warning("⚠️ TG_TOKEN 或 CHAT_ID 未設定")
        return None

    payload = {
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_to_id:
        payload["reply_to_message_id"] = reply_to_id
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": [buttons]})

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json=payload,
            timeout=5,
        )
        if r.status_code == 200:
            return r.json().get("result", {}).get("message_id")
        logging.warning(f"⚠️ TG API 回應 {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logging.error(f"❌ TG 發送失敗: {e}")
    return None


def _get_order_button(order_id: str) -> list:
    return [{
        "text": f"🔍 訂單 {order_id[-8:]}",
        "callback_data": f"order_{order_id}",
    }]


# ─────────────────────────────────────────────────────────
# 4. 訊息模板 (專業簡潔版)
# ─────────────────────────────────────────────────────────
def _fmt_entry(coin, side, order_id, price, entry, sl, tp1, tp2, tp3, score):
    """📌 進場通知"""
    direction = "做多 LONG" if side == "LONG" else "做空 SHORT"
    grade = "🔥" if score >= 80 else "⭐" if score >= 70 else "✅"

    tp1_pct = (tp1 - entry) / entry * 100
    tp2_pct = (tp2 - entry) / entry * 100
    tp3_pct = (tp3 - entry) / entry * 100
    sl_pct = (sl - entry) / entry * 100

    return (
        f"{grade} *{coin} · 進場 {direction}*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🆔 `{order_id}`\n"
        f"⏰ {get_tw_time()}\n"
        f"\n"
        f"進場 `{entry:.4f}`  |  現價 `{price:.4f}`  |  評分 *{score}*\n"
        f"\n"
        f"🎯 TP1 `{tp1:.4f}` ({tp1_pct:+.2f}%)\n"
        f"🎯 TP2 `{tp2:.4f}` ({tp2_pct:+.2f}%)\n"
        f"🎯 TP3 `{tp3:.4f}` ({tp3_pct:+.2f}%)\n"
        f"🛑 SL  `{sl:.4f}` ({sl_pct:+.2f}%)\n"
        f"\n"
        f"_TP1 自動保本 · TP2 自動鎖利_"
    )


def _fmt_tp(coin, side, order_id, tp_level, trigger_price, entry, pnl_pct, r_mult, advice):
    """🎯 止盈通知 (顯示實際觸發當下價格)"""
    direction = "做多" if side == "LONG" else "做空"
    medal = {"TP1": "🥇", "TP2": "🥈", "TP3": "🏆"}.get(tp_level, "🎯")

    return (
        f"{medal} *{coin} · {tp_level} 達標*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🆔 `{order_id}`\n"
        f"⏰ {get_tw_time()}\n"
        f"\n"
        f"方向 {direction}  |  現價 `{trigger_price:.4f}`\n"
        f"獲利 *+{pnl_pct:.2f}%* (`+{r_mult:.1f}R`)\n"
        f"\n"
        f"💡 {advice}"
    )


def _fmt_sl(coin, side, order_id, trigger_price, entry, pnl_pct, is_be):
    """🛑 止損 / 保本出場"""
    direction = "做多" if side == "LONG" else "做空"
    if is_be:
        label, tag, advice = "🔒 保本出場", "0.0R", "資金安全，等待下一次機會"
    else:
        label, tag, advice = "❌ 止損出場", "-1.0R", "遵守風控，勿加碼攤平"

    return (
        f"{label} *{coin}*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🆔 `{order_id}`\n"
        f"⏰ {get_tw_time()}\n"
        f"\n"
        f"方向 {direction}  |  現價 `{trigger_price:.4f}`\n"
        f"結果 *{pnl_pct:+.2f}%* (`{tag}`)\n"
        f"\n"
        f"💡 {advice}"
    )


def _fmt_expire(coin, order_id, entry, price):
    return (
        f"⏰ *{coin} · 訊號過期*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🆔 `{order_id}`\n"
        f"進場 `{entry:.4f}` 24h 內未觸發\n"
        f"當前 `{price:.4f}`"
    )


# ─────────────────────────────────────────────────────────
# 5. 行情 API (專業版：禁用快取)
# ─────────────────────────────────────────────────────────
def fetch_price(instId: str, retries: int = 2) -> float:
    """
    🔍 即時價格 (不使用快取，避免觸發判斷延遲)
    retries: 失敗重試次數
    """
    for attempt in range(retries + 1):
        try:
            res = requests.get(
                f"https://www.okx.com/api/v5/market/ticker?instId={instId}",
                timeout=3,
            ).json()
            if res.get("code") == "0" and res.get("data"):
                price = float(res["data"][0]["last"])
                if price > 0:
                    return price
        except Exception as e:
            if attempt < retries:
                time.sleep(0.3)
                continue
            logging.warning(f"⚠️ fetch_price({instId}) 失敗: {e}")
    return 0.0


def fetch_candles(instId: str, tf: str = "15m", limit: int = 100):
    """📊 取 K 線 (只取 confirmed candle)"""
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}",
            timeout=3,
        ).json()
        if res.get("code") != "0":
            return None
        data = res.get("data", [])
        if len(data) < 30:
            return None
        confirmed = [row for row in data if row[8] == "1"][::-1]
        return [
            {"ts": row[0], "o": float(row[1]), "h": float(row[2]),
             "l": float(row[3]), "c": float(row[4]), "v": float(row[5])}
            for row in confirmed
        ]
    except Exception as e:
        logging.warning(f"⚠️ fetch_candles({instId}) 失敗: {e}")
        return None


# ─────────────────────────────────────────────────────────
# 6. 技術指標
# ─────────────────────────────────────────────────────────
def calc_atr(df, period: int = 14) -> float:
    if len(df) < period + 1:
        return 0.001
    trs = []
    for i in range(1, len(df)):
        hl = df[i]["h"] - df[i]["l"]
        hc = abs(df[i]["h"] - df[i - 1]["c"])
        lc = abs(df[i]["l"] - df[i - 1]["c"])
        trs.append(max(hl, hc, lc))
    if len(trs) < period:
        return 0.001
    atr = sum(trs[-period:]) / period
    return atr if atr > 0 else 0.001


def calc_supertrend(df, period: int = 10) -> int:
    if len(df) < period + 2:
        return 0
    atr = calc_atr(df, period)
    mid_price = sum(row["c"] for row in df[-20:]) / 20
    current_price = df[-1]["c"]
    if current_price > mid_price + atr * 0.5:
        return 1
    if current_price < mid_price - atr * 0.5:
        return -1
    return 0


def calc_rsi(df, period: int = 14) -> float:
    if len(df) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(df)):
        change = df[i]["c"] - df[i - 1]["c"]
        gains.append(change if change > 0 else 0)
        losses.append(-change if change < 0 else 0)
    if len(gains) < period:
        return 50.0
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_score(df, side: str) -> tuple:
    score = 0
    st = calc_supertrend(df)
    if (side == "LONG" and st == 1) or (side == "SHORT" and st == -1):
        score += 60
    elif st == 0:
        score += 30
    rsi = calc_rsi(df)
    if side == "LONG":
        if 30 <= rsi <= 50:
            score += 40
        elif 50 < rsi < 70:
            score += 20
    else:
        if 50 <= rsi <= 70:
            score += 40
        elif 30 < rsi < 50:
            score += 20
    grade = "A+ 極強" if score >= 85 else "A 強力" if score >= 70 else "B+ 觀望"
    return score, grade


# ─────────────────────────────────────────────────────────
# 7. 訊號生成
# ─────────────────────────────────────────────────────────
def generate_signal(instId: str, df, current_price: float):
    if df is None or len(df) < 50:
        return None
    price = current_price
    atr = calc_atr(df)
    if atr / price > 0.04:
        return None

    signals = []
    for side in ["LONG", "SHORT"]:
        score, grade = calc_score(df, side)
        if score < SCORE_THRESHOLD:
            continue

        entry = price
        sl_dist = atr * 1.5
        sl = entry - sl_dist if side == "LONG" else entry + sl_dist
        risk = abs(entry - sl)

        signals.append({
            "instId": instId,
            "side": side,
            "tf": "15m",
            "entry": round(entry, 4),
            "sl": round(sl, 4),
            "tp1": round(entry + risk if side == "LONG" else entry - risk, 4),
            "tp2": round(entry + risk * 2.5 if side == "LONG" else entry - risk * 2.5, 4),
            "tp3": round(entry + risk * 4.0 if side == "LONG" else entry - risk * 4.0, 4),
            "score": score,
            "grade": grade,
            "created": time.time(),
            "expires": time.time() + SIGNAL_EXPIRE_HOURS * 3600,
        })
    return max(signals, key=lambda x: x["score"]) if signals else None


# ─────────────────────────────────────────────────────────
# 8. SignalTracker (專業版 — 嚴格觸發 + 階梯式 TP)
# ─────────────────────────────────────────────────────────
class SignalTracker:
    def __init__(self, filepath: str = ACTIVE_SIGNALS_FILE):
        self.filepath = filepath
        self.signals = self._load()

    def _load(self) -> dict:
        try:
            if os.path.exists(self.filepath):
                with open(self.filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logging.warning(f"⚠️ load 失敗: {e}")
        return {}

    def _save(self):
        try:
            temp = self.filepath + ".tmp"
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(self.signals, f, indent=2, ensure_ascii=False)
            os.replace(temp, self.filepath)
        except Exception as e:
            logging.error(f"❌ save 失敗: {e}")

    def add(self, signal: dict, active: bool = False, entry_msg_id: int = None) -> tuple:
        order_id = f"{int(time.time())}-{uuid.uuid4().hex[:8].upper()}"
        key = f"{signal['instId']}_{signal['side']}_{order_id}"
        self.signals[key] = {
            **signal,
            "order_id": order_id,
            "status": "ACTIVE" if active else "PENDING",
            "hit_tp1": False,
            "hit_tp2": False,
            "hit_tp3": False,
            "entry_msg_id": entry_msg_id,
            "activated_at": time.time() if active else None,
        }
        self._save()
        logging.info(f"📌 新增訂單 {order_id} {signal['instId']} {signal['side']}")
        return key, order_id

    def update_signal(self, key: str, **kwargs):
        if key in self.signals:
            self.signals[key].update(kwargs)
            self._save()

    def check_all(self):
        """🔄 檢查所有訊號"""
        to_remove = []
        for key, sig in list(self.signals.items()):
            try:
                closed = self._check_one(key, sig)
                if closed:
                    to_remove.append(key)
            except Exception as e:
                logging.error(f"❌ check_one [{key}] 錯誤: {e}")
        for key in to_remove:
            if key in self.signals:
                del self.signals[key]
        self._save()

    def _check_one(self, key: str, sig: dict) -> bool:
        """
        🔍 專業版嚴格觸發邏輯:
           - 不使用任何容差 _dev
           - TP1 → TP2 → TP3 階梯式觸發
           - 每觸發一次就傳一次訊息，顯示真實當下價
           - SL 跟隨: TP1 後移到保本, TP2 後移到 TP1
        """
        price = fetch_price(sig["instId"])  # 每次拉即時價，不走快取
        if price <= 0:
            logging.warning(f"[{sig['instId']}] 拿不到價格，略過")
            return False

        coin = sig["instId"].split("-")[0]
        order_id = sig.get("order_id", "N/A")
        side = sig["side"]
        status = sig["status"]
        entry = sig["entry"]
        sl = sig["sl"]
        tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
        reply_id = sig.get("entry_msg_id")

        # ─────────── PENDING → ACTIVE ───────────
        if status == "PENDING":
            if time.time() > sig["expires"]:
                send_tg(_fmt_expire(coin, order_id, entry, price), reply_to_id=reply_id)
                logging.info(f"⏰ {order_id} 過期")
                return True

            in_zone = (
                (side == "LONG" and entry * (1 - ENTRY_ZONE_TOLERANCE) <= price <= entry * (1 + 0.002))
                or (side == "SHORT" and entry * (1 - 0.002) <= price <= entry * (1 + ENTRY_ZONE_TOLERANCE))
            )
            if in_zone:
                sig["status"] = "ACTIVE"
                sig["activated_at"] = time.time()
                msg = _fmt_entry(coin, side, order_id, price, entry, sl, tp1, tp2, tp3, sig["score"])
                new_msg_id = send_tg(msg, reply_to_id=reply_id, buttons=_get_order_button(order_id))
                if new_msg_id:
                    sig["entry_msg_id"] = new_msg_id
                self._save()
                logging.info(f"🟢 {order_id} 進場 @ {price:.4f}")
            return False

        if status not in ("ACTIVE", "BE", "TRAIL"):
            return False

        # ─────────── SL 嚴格觸發 ───────────
        if (side == "LONG" and price <= sl) or (side == "SHORT" and price >= sl):
            is_be = status in ("BE", "TRAIL") and abs(sl - entry) < entry * 0.0001
            pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
            send_tg(
                _fmt_sl(coin, side, order_id, price, entry, pnl, is_be),
                reply_to_id=reply_id,
                buttons=_get_order_button(order_id),
            )
            _record_trade(coin, side, order_id, entry, price, "BE" if is_be else "SL", sig["score"])
            logging.info(f"🔴 {order_id} {'BE' if is_be else 'SL'} @ {price:.4f}")
            return True

        # ─────────── TP1 嚴格觸發 ───────────
        if not sig["hit_tp1"]:
            if (side == "LONG" and price >= tp1) or (side == "SHORT" and price <= tp1):
                sig["hit_tp1"] = True
                sig["sl"] = entry          # 保本
                sig["status"] = "BE"
                pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
                send_tg(
                    _fmt_tp(coin, side, order_id, "TP1", price, entry, pnl, 1.0, "建議平倉 ⅓，SL 上移保本"),
                    reply_to_id=reply_id,
                    buttons=_get_order_button(order_id),
                )
                _record_trade(coin, side, order_id, entry, price, "TP1", sig["score"])
                self._save()
                logging.info(f"🥇 {order_id} TP1 @ {price:.4f}")
                # ⚠️ 不 return，繼續檢查是否同時跨越 TP2/TP3

        # ─────────── TP2 嚴格觸發 (必須先 TP1) ───────────
        if sig["hit_tp1"] and not sig["hit_tp2"]:
            if (side == "LONG" and price >= tp2) or (side == "SHORT" and price <= tp2):
                sig["hit_tp2"] = True
                sig["sl"] = tp1            # 鎖利到 TP1
                sig["status"] = "TRAIL"
                pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
                send_tg(
                    _fmt_tp(coin, side, order_id, "TP2", price, entry, pnl, 2.5, "建議平倉 ⅓，SL 鎖利到 TP1"),
                    reply_to_id=reply_id,
                    buttons=_get_order_button(order_id),
                )
                _record_trade(coin, side, order_id, entry, price, "TP2", sig["score"])
                self._save()
                logging.info(f"🥈 {order_id} TP2 @ {price:.4f}")

        # ─────────── TP3 嚴格觸發 (必須先 TP2) ───────────
        if sig["hit_tp2"] and not sig["hit_tp3"]:
            if (side == "LONG" and price >= tp3) or (side == "SHORT" and price <= tp3):
                sig["hit_tp3"] = True
                pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
                send_tg(
                    _fmt_tp(coin, side, order_id, "TP3", price, entry, pnl, 4.0, "建議全部平倉，完美收割"),
                    reply_to_id=reply_id,
                    buttons=_get_order_button(order_id),
                )
                _record_trade(coin, side, order_id, entry, price, "TP3", sig["score"])
                logging.info(f"🏆 {order_id} TP3 @ {price:.4f} — 訂單結束")
                return True    # TP3 達成才關閉訂單

        return False

    def get_position_stats(self) -> str:
        positions = []
        for sig in self.signals.values():
            if sig["status"] in ("ACTIVE", "BE", "TRAIL", "PENDING"):
                sig = {**sig, "current_price": fetch_price(sig["instId"])}
                positions.append(sig)
        if not positions:
            return "📭 *目前無持倉*"

        msg = f"📊 *追蹤中訊號 ({len(positions)} 筆)*\n━━━━━━━━━━━━━━━\n\n"
        for i, p in enumerate(positions):
            coin = p["instId"].split("-")[0]
            side_emoji = "🟢" if p["side"] == "LONG" else "🔴"
            order_id = p.get("order_id", "N/A")
            cp = p["current_price"]
            if cp > 0:
                pnl = ((cp - p["entry"]) / p["entry"] * 100) if p["side"] == "LONG" else ((p["entry"] - cp) / p["entry"] * 100)
                pnl_str = f"{pnl:+.2f}%"
            else:
                pnl_str = "—"
            progress = "🏆" if p.get("hit_tp3") else "🥈" if p.get("hit_tp2") else "🥇" if p.get("hit_tp1") else "⏳"
            msg += (
                f"{side_emoji} *{coin}* {p['side']} · {p['status']} {progress}\n"
                f"🆔 `{order_id}`  |  評分 {p.get('score', 0)}\n"
                f"進場 `{p['entry']:.4f}`  現價 `{cp:.4f}` ({pnl_str})\n"
                f"SL `{p['sl']:.4f}`\n"
                f"TP1 `{p['tp1']:.4f}`{'✅' if p.get('hit_tp1') else ''}  "
                f"TP2 `{p['tp2']:.4f}`{'✅' if p.get('hit_tp2') else ''}  "
                f"TP3 `{p['tp3']:.4f}`{'✅' if p.get('hit_tp3') else ''}\n"
            )
            if i < len(positions) - 1:
                msg += "\n━━━━━━━━━━━━━━━\n\n"
        return msg


# ─────────────────────────────────────────────────────────
# 9. 交易歷史
# ─────────────────────────────────────────────────────────
def _record_trade(coin, side, order_id, entry, close_price, close_type, score):
    is_win = close_type in ("TP1", "TP2", "TP3")
    is_be = close_type == "BE"
    pnl = ((close_price - entry) / entry * 100) if side == "LONG" else ((entry - close_price) / entry * 100)
    trade = {
        "time": get_tw_time(),
        "date": get_tw_date(),
        "order_id": order_id,
        "coin": coin,
        "side": side,
        "entry": entry,
        "close": close_price,
        "close_type": close_type,
        "pnl": round(pnl, 2),
        "is_win": is_win,
        "is_be": is_be,
        "score": score,
    }
    try:
        history = []
        if os.path.exists(TRADE_HISTORY_FILE):
            with open(TRADE_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        history.append(trade)
        with open(TRADE_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
        logging.info(f"📝 記錄 {coin} {order_id} {close_type} {pnl:+.2f}%")
    except Exception as e:
        logging.error(f"❌ 記錄交易失敗: {e}")


# ─────────────────────────────────────────────────────────
# 10. 主掃描
# ─────────────────────────────────────────────────────────
def run_scan(tracker: SignalTracker) -> int:
    logging.info("🚀 開始掃描")
    sent = 0

    for instId in ALL_COINS:
        if sent >= MAX_SIGNALS:
            break

        # 冷卻檢查 (2h 同幣種不重複開新單)
        cool_key = f"{instId}_ALL"
        if cool_key in _signal_cooldown and time.time() - _signal_cooldown[cool_key] < 2 * 3600:
            continue

        try:
            current_price = fetch_price(instId)
            if current_price <= 0:
                logging.warning(f"[{instId}] 無法獲取價格")
                continue

            logging.info(f"[{instId}] 現價 {current_price}")

            df = fetch_candles(instId)
            if df is None:
                continue

            signal = generate_signal(instId, df, current_price)
            if not signal:
                continue

            # 檢查是否已在 tracker 有未結的同方向單 (避免重複)
            dup = any(
                s["instId"] == instId and s["side"] == signal["side"]
                and s["status"] in ("PENDING", "ACTIVE", "BE", "TRAIL")
                for s in tracker.signals.values()
            )
            if dup:
                logging.info(f"[{instId}] 已有 {signal['side']} 未結單，略過")
                continue

            in_zone = (
                (signal["side"] == "LONG"
                 and signal["entry"] * (1 - ENTRY_ZONE_TOLERANCE) <= current_price <= signal["entry"] * (1 + 0.002))
                or (signal["side"] == "SHORT"
                    and signal["entry"] * (1 - 0.002) <= current_price <= signal["entry"] * (1 + ENTRY_ZONE_TOLERANCE))
            )

            key, order_id = tracker.add(signal, active=in_zone)

            msg = _fmt_entry(
                coin=instId.split("-")[0],
                side=signal["side"],
                order_id=order_id,
                price=current_price,
                entry=signal["entry"],
                sl=signal["sl"],
                tp1=signal["tp1"],
                tp2=signal["tp2"],
                tp3=signal["tp3"],
                score=signal["score"],
            )
            entry_msg_id = send_tg(msg, reply_to_id=None, buttons=_get_order_button(order_id))
            if entry_msg_id:
                tracker.update_signal(key, entry_msg_id=entry_msg_id)

            _signal_cooldown[cool_key] = time.time()
            sent += 1
            logging.info(f"✅ {instId} 訊號發送成功 {order_id} msg_id={entry_msg_id}")

        except Exception as e:
            logging.error(f"[{instId}] 掃描失敗: {e}")
            continue

    # 檢查既有訊號，嚴格判定 TP/SL
    tracker.check_all()

    logging.info(f"🎯 本輪發送 {sent} 筆新訊號")
    return sent


# ─────────────────────────────────────────────────────────
# 11. 入口
# ─────────────────────────────────────────────────────────
def main():
    try:
        logging.info("=" * 50)
        logging.info("🤖 Alpha Oracle Pro v11.0 專業版啟動")
        logging.info("=" * 50)

        tracker = SignalTracker(ACTIVE_SIGNALS_FILE)

        if len(sys.argv) > 1 and sys.argv[1] in ("/stats", "/持倉"):
            send_tg(tracker.get_position_stats())
            return

        run_scan(tracker)
        logging.info("🎉 執行完成")

    except Exception as e:
        logging.error(f"🔥 系統錯誤: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
