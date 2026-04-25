#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro v11.1 — 精緻專業版 (Production-Grade, Marketable)
══════════════════════════════════════════════════════════════════════
🆕 v11.1 新增 (相對 v11.0)：
  ✨ 訊息模板精緻化 (信心條、進度條、方向箭頭、emoji 視覺語彙)
  🔒 Fee-aware Break-Even (保本點含來回手續費 0.1%)
  🛡️ Anti-Wick 防插針 (TP/SL 需連續 2 tick 確認，過濾單根長影線假訊號)
  ⚠️ Slippage Guard (PENDING 訊號偏離 >1% 自動作廢)
  💹 資金費率顯示 (>0.05%/8h 警示)
  📊 每日戰績命令 /daily

🔧 v11.0 保留 (核心修正)：
  ✅ 嚴格 TP/SL 比較 (無容差)
  ✅ 階梯式 TP1→TP2→TP3
  ✅ 觸發訊息顯示真實當下價
  ✅ 線層回覆 (reply_to_id)
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


# ═════════════════════════════════════════════════════════
# 🔧 環境變數
# ═════════════════════════════════════════════════════════
def _get_env(key, default=""):
    val = os.getenv(key)
    return val.strip() if val and val.strip() else default


def _get_env_int(key, default):
    val = os.getenv(key)
    try:
        return int(val.strip()) if val and val.strip() else default
    except Exception:
        return default


def _get_env_float(key, default):
    val = os.getenv(key)
    try:
        return float(val.strip()) if val and val.strip() else default
    except Exception:
        return default


# ═════════════════════════════════════════════════════════
# 1. 基礎配置
# ═════════════════════════════════════════════════════════
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

# 🆕 風控參數
FEE_RATE = _get_env_float("FEE_RATE", 0.001)         # 來回手續費估計 0.1%
SLIPPAGE_LIMIT_PCT = _get_env_float("SLIPPAGE_LIMIT", 0.01)  # PENDING 偏離 1% 作廢
ENTRY_ZONE_TOLERANCE = 0.006                          # 進場區間 0.6%
FUNDING_WARN_THRESHOLD = 0.0005                       # 資金費率警示 0.05%/8h

ACTIVE_SIGNALS_FILE = "active_signals.json"
TRADE_HISTORY_FILE = "trade_history.json"

_signal_cooldown = {}


# ═════════════════════════════════════════════════════════
# 2. 時間工具
# ═════════════════════════════════════════════════════════
def get_tw_time() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S TW")


def get_tw_date() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")


# ═════════════════════════════════════════════════════════
# 3. 視覺化元件 (信心條、進度標記)
# ═════════════════════════════════════════════════════════
def _score_bar(score: int, width: int = 10) -> str:
    """🎯 信心度視覺化條: ████████▒░ 85"""
    filled = min(width, int(score / 100 * width))
    empty = width - filled
    return "█" * filled + "▒" * empty


def _grade_emoji(score: int) -> tuple:
    """根據分數回傳 (emoji, 等級標籤)"""
    if score >= 85:
        return "🔥", "A+ 極強"
    if score >= 75:
        return "⭐", "A 強力"
    if score >= 68:
        return "✅", "B+ 合格"
    return "⚪", "B 觀望"


def _direction_emoji(side: str) -> str:
    return "📈 做多 LONG" if side == "LONG" else "📉 做空 SHORT"


def _progress_icons(hit_tp1: bool, hit_tp2: bool, hit_tp3: bool) -> str:
    """進度圖示: ⭕⭕⭕ → 🥇⭕⭕ → 🥇🥈⭕ → 🥇🥈🏆"""
    s = "🥇" if hit_tp1 else "⭕"
    s += "🥈" if hit_tp2 else "⭕"
    s += "🏆" if hit_tp3 else "⭕"
    return s


# ═════════════════════════════════════════════════════════
# 4. 通知系統 (保留 reply_to_id 線層回覆)
# ═════════════════════════════════════════════════════════
def send_tg(msg: str, parse_mode: str = "Markdown", reply_to_id: int = None, buttons: list = None) -> int:
    if not TG_TOKEN or not CHAT_ID:
        logging.warning("⚠️ TG_TOKEN / CHAT_ID 未設定")
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
        logging.warning(f"⚠️ TG API {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logging.error(f"❌ TG 發送失敗: {e}")
    return None


def _get_order_button(order_id: str) -> list:
    return [{
        "text": f"🔍 查詢訂單 {order_id[-8:]}",
        "callback_data": f"order_{order_id}",
    }]


# ═════════════════════════════════════════════════════════
# 5. 訊息模板 (v11.1 精緻版)
# ═════════════════════════════════════════════════════════
def _fmt_entry(coin, side, order_id, price, entry, sl, tp1, tp2, tp3, score,
               funding: float = None):
    """✨ 進場通知 — 精緻版"""
    grade_em, grade_txt = _grade_emoji(score)
    dir_txt = _direction_emoji(side)
    bar = _score_bar(score)

    tp1_pct = (tp1 - entry) / entry * 100
    tp2_pct = (tp2 - entry) / entry * 100
    tp3_pct = (tp3 - entry) / entry * 100
    sl_pct = (sl - entry) / entry * 100

    funding_line = ""
    if funding is not None:
        fund_pct = funding * 100
        if abs(funding) >= FUNDING_WARN_THRESHOLD:
            funding_line = f"💹 資金費率 `{fund_pct:+.4f}%/8h`  ⚠️ 偏高\n"
        else:
            funding_line = f"💹 資金費率 `{fund_pct:+.4f}%/8h`\n"

    return (
        f"{grade_em} *{coin} · {dir_txt}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🆔 `{order_id}`\n"
        f"⏰ `{get_tw_time()}`\n"
        f"\n"
        f"📊 *訊號強度* `{bar}` `{score}/100` · {grade_txt}\n"
        f"\n"
        f"📍 進場   `{entry:.4f}`\n"
        f"💰 現價   `{price:.4f}`\n"
        f"{funding_line}"
        f"\n"
        f"*🎯 止盈目標*\n"
        f"  🥇 TP1  `{tp1:.4f}`  `{tp1_pct:+.2f}%`  `+1.0R`\n"
        f"  🥈 TP2  `{tp2:.4f}`  `{tp2_pct:+.2f}%`  `+2.5R`\n"
        f"  🏆 TP3  `{tp3:.4f}`  `{tp3_pct:+.2f}%`  `+4.0R`\n"
        f"\n"
        f"*🛑 止損* `{sl:.4f}`  `{sl_pct:+.2f}%`  `-1.0R`\n"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💡 _TP1 → 保本_ · _TP2 → 鎖利到 TP1_ · _TP3 → 全平_"
    )


def _fmt_tp(coin, side, order_id, tp_level, trigger_price, entry, pnl_pct, r_mult,
            advice, hit_tp1: bool, hit_tp2: bool, hit_tp3: bool):
    """✨ 止盈通知 — 精緻版"""
    dir_txt = _direction_emoji(side)
    medal = {"TP1": "🥇", "TP2": "🥈", "TP3": "🏆"}.get(tp_level, "🎯")
    progress = _progress_icons(hit_tp1, hit_tp2, hit_tp3)

    head_emoji = "🎉" if tp_level == "TP3" else "🟢"
    bonus = " · 滿貫達成！" if tp_level == "TP3" else ""

    return (
        f"{head_emoji} *{coin} · {medal} {tp_level} 達標{bonus}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🆔 `{order_id}`\n"
        f"⏰ `{get_tw_time()}`\n"
        f"\n"
        f"{dir_txt}\n"
        f"💥 觸發現價 `{trigger_price:.4f}`\n"
        f"💎 已實現   `+{pnl_pct:.2f}%`  (`+{r_mult:.1f}R`)\n"
        f"📈 進度     {progress}\n"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💡 {advice}"
    )


def _fmt_sl(coin, side, order_id, trigger_price, entry, pnl_pct, is_be,
            hit_tp1: bool, hit_tp2: bool):
    """✨ 止損 / 保本通知 — 精緻版"""
    dir_txt = _direction_emoji(side)

    if is_be:
        head = "🔒 *保本出場*"
        tag = "0.0R"
        advice = "✅ 資金安全，等待下一次機會 💪"
    else:
        head = "❌ *止損出場*"
        tag = "-1.0R"
        advice = "⚠️ 遵守風控，勿加碼攤平，下次會更好"

    # 顯示走過的成果
    reached = ""
    if hit_tp2:
        reached = "📈 本單已吃到 🥇🥈 獲利\n"
    elif hit_tp1:
        reached = "📈 本單已吃到 🥇 獲利\n"

    return (
        f"{head} · *{coin}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🆔 `{order_id}`\n"
        f"⏰ `{get_tw_time()}`\n"
        f"\n"
        f"{dir_txt}\n"
        f"💥 觸發現價 `{trigger_price:.4f}`\n"
        f"📉 結果     `{pnl_pct:+.2f}%`  (`{tag}`)\n"
        f"{reached}"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💡 {advice}"
    )


def _fmt_expire(coin, order_id, entry, price, reason: str = "24h 內未觸發"):
    return (
        f"⏰ *{coin} · 訊號過期*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🆔 `{order_id}`\n"
        f"原因: {reason}\n"
        f"進場價 `{entry:.4f}` · 當前 `{price:.4f}`\n"
        f"\n"
        f"💡 _系統持續掃描下一次機會_"
    )


def _fmt_slippage(coin, order_id, entry, price, dev_pct):
    return (
        f"⚠️ *{coin} · 滑價保護觸發*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🆔 `{order_id}`\n"
        f"進場目標 `{entry:.4f}` · 現價 `{price:.4f}`\n"
        f"偏離 `{dev_pct:+.2f}%` > `{SLIPPAGE_LIMIT_PCT*100:.1f}%` 上限\n"
        f"\n"
        f"💡 _訊號作廢，避免追高/追空_"
    )


# ═════════════════════════════════════════════════════════
# 6. 行情 API
# ═════════════════════════════════════════════════════════
def fetch_price(instId: str, retries: int = 2) -> float:
    """即時價 — 不用快取，確保觸發判斷新鮮"""
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


def fetch_funding_rate(instId: str) -> float:
    """🆕 取資金費率 (每 8h)"""
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}",
            timeout=3,
        ).json()
        if res.get("code") == "0" and res.get("data"):
            return float(res["data"][0].get("fundingRate", 0))
    except Exception as e:
        logging.debug(f"funding rate fetch failed: {e}")
    return 0.0


def fetch_candles(instId: str, tf: str = "15m", limit: int = 100):
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


# ═════════════════════════════════════════════════════════
# 7. 技術指標
# ═════════════════════════════════════════════════════════
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
    mid = sum(row["c"] for row in df[-20:]) / 20
    cp = df[-1]["c"]
    if cp > mid + atr * 0.5:
        return 1
    if cp < mid - atr * 0.5:
        return -1
    return 0


def calc_rsi(df, period: int = 14) -> float:
    if len(df) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(df)):
        ch = df[i]["c"] - df[i - 1]["c"]
        gains.append(ch if ch > 0 else 0)
        losses.append(-ch if ch < 0 else 0)
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
    _, grade = _grade_emoji(score)
    return score, grade


# ═════════════════════════════════════════════════════════
# 8. 訊號生成
# ═════════════════════════════════════════════════════════
def generate_signal(instId, df, current_price):
    if df is None or len(df) < 50:
        return None
    atr = calc_atr(df)
    if atr / current_price > 0.04:
        return None

    signals = []
    for side in ["LONG", "SHORT"]:
        score, grade = calc_score(df, side)
        if score < SCORE_THRESHOLD:
            continue
        entry = current_price
        sl_dist = atr * 1.5
        sl = entry - sl_dist if side == "LONG" else entry + sl_dist
        risk = abs(entry - sl)
        signals.append({
            "instId": instId, "side": side, "tf": "15m",
            "entry": round(entry, 4), "sl": round(sl, 4),
            "tp1": round(entry + risk if side == "LONG" else entry - risk, 4),
            "tp2": round(entry + risk * 2.5 if side == "LONG" else entry - risk * 2.5, 4),
            "tp3": round(entry + risk * 4.0 if side == "LONG" else entry - risk * 4.0, 4),
            "score": score, "grade": grade,
            "created": time.time(),
            "expires": time.time() + SIGNAL_EXPIRE_HOURS * 3600,
        })
    return max(signals, key=lambda x: x["score"]) if signals else None


# ═════════════════════════════════════════════════════════
# 9. SignalTracker (嚴格觸發 + anti-wick + 階梯式 TP)
# ═════════════════════════════════════════════════════════
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

    def add(self, signal, active=False, entry_msg_id=None):
        order_id = f"{int(time.time())}-{uuid.uuid4().hex[:8].upper()}"
        key = f"{signal['instId']}_{signal['side']}_{order_id}"
        self.signals[key] = {
            **signal,
            "order_id": order_id,
            "status": "ACTIVE" if active else "PENDING",
            "hit_tp1": False, "hit_tp2": False, "hit_tp3": False,
            "entry_msg_id": entry_msg_id,
            "activated_at": time.time() if active else None,
            # 🆕 anti-wick 用：記錄「過線但尚未確認」的狀態
            "pending_trigger": None,   # "TP1" / "TP2" / "TP3" / "SL" / None
        }
        self._save()
        logging.info(f"📌 新增訂單 {order_id} {signal['instId']} {signal['side']}")
        return key, order_id

    def update_signal(self, key, **kwargs):
        if key in self.signals:
            self.signals[key].update(kwargs)
            self._save()

    def check_all(self):
        to_remove = []
        for key, sig in list(self.signals.items()):
            try:
                closed = self._check_one(key, sig)
                if closed:
                    to_remove.append(key)
            except Exception as e:
                logging.error(f"❌ check_one [{key}]: {e}")
        for key in to_remove:
            if key in self.signals:
                del self.signals[key]
        self._save()

    def _confirm_trigger(self, sig, level: str) -> bool:
        """
        🛡️ Anti-wick 二次確認邏輯:
           第一次過線 → 記為 pending
           第二次也過線 → 確認觸發
           中途回到線內 → 取消 pending
        """
        if sig.get("pending_trigger") == level:
            return True   # 上次已過線，這次也過線 → 確認
        sig["pending_trigger"] = level
        self._save()
        logging.info(f"  ⏳ {sig['order_id']} {level} 首次過線，待確認")
        return False

    def _clear_pending(self, sig):
        if sig.get("pending_trigger"):
            logging.info(f"  ↩️ {sig['order_id']} 回到線內，取消 {sig['pending_trigger']} 待確認")
            sig["pending_trigger"] = None

    def _check_one(self, key, sig) -> bool:
        price = fetch_price(sig["instId"])
        if price <= 0:
            logging.warning(f"[{sig['instId']}] 無價格，略過")
            return False

        coin = sig["instId"].split("-")[0]
        order_id = sig.get("order_id", "N/A")
        side = sig["side"]
        status = sig["status"]
        entry = sig["entry"]
        sl = sig["sl"]
        tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
        reply_id = sig.get("entry_msg_id")

        # ─── PENDING ───
        if status == "PENDING":
            # 🆕 過期
            if time.time() > sig["expires"]:
                send_tg(_fmt_expire(coin, order_id, entry, price), reply_to_id=reply_id)
                logging.info(f"⏰ {order_id} 過期")
                return True

            # 🆕 滑價保護：若現價偏離 entry 超過上限，作廢
            dev_pct = (price - entry) / entry * 100
            if abs(dev_pct) / 100 > SLIPPAGE_LIMIT_PCT:
                send_tg(_fmt_slippage(coin, order_id, entry, price, dev_pct),
                        reply_to_id=reply_id)
                logging.info(f"⚠️ {order_id} 滑價過大作廢 {dev_pct:+.2f}%")
                return True

            # 進場區
            in_zone = (
                (side == "LONG" and entry * (1 - ENTRY_ZONE_TOLERANCE) <= price <= entry * (1 + 0.002))
                or (side == "SHORT" and entry * (1 - 0.002) <= price <= entry * (1 + ENTRY_ZONE_TOLERANCE))
            )
            if in_zone:
                sig["status"] = "ACTIVE"
                sig["activated_at"] = time.time()
                funding = fetch_funding_rate(sig["instId"])
                msg = _fmt_entry(coin, side, order_id, price, entry, sl, tp1, tp2, tp3,
                                 sig["score"], funding=funding)
                new_msg_id = send_tg(msg, reply_to_id=reply_id,
                                     buttons=_get_order_button(order_id))
                if new_msg_id:
                    sig["entry_msg_id"] = new_msg_id
                self._save()
                logging.info(f"🟢 {order_id} 進場 @ {price:.4f}")
            return False

        if status not in ("ACTIVE", "BE", "TRAIL"):
            return False

        # ═══════════ 觸發檢查 (含 anti-wick) ═══════════
        # 📌 SL
        sl_cross = (side == "LONG" and price <= sl) or (side == "SHORT" and price >= sl)
        if sl_cross:
            if not self._confirm_trigger(sig, "SL"):
                return False   # 第一次過線，等下次確認
            is_be = status in ("BE", "TRAIL") and abs(sl - entry) < entry * (FEE_RATE + 0.0001)
            pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
            send_tg(
                _fmt_sl(coin, side, order_id, price, entry, pnl, is_be,
                        sig.get("hit_tp1", False), sig.get("hit_tp2", False)),
                reply_to_id=reply_id,
                buttons=_get_order_button(order_id),
            )
            _record_trade(coin, side, order_id, entry, price, "BE" if is_be else "SL", sig["score"])
            logging.info(f"🔴 {order_id} {'BE' if is_be else 'SL'} 確認 @ {price:.4f}")
            return True

        # 📌 TP1 (必須先過 TP1)
        if not sig["hit_tp1"]:
            tp1_cross = (side == "LONG" and price >= tp1) or (side == "SHORT" and price <= tp1)
            if tp1_cross:
                if not self._confirm_trigger(sig, "TP1"):
                    return False
                sig["hit_tp1"] = True
                # 🆕 fee-aware BE：保本點含手續費
                sig["sl"] = entry * (1 + FEE_RATE) if side == "LONG" else entry * (1 - FEE_RATE)
                sig["status"] = "BE"
                sig["pending_trigger"] = None
                pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
                send_tg(
                    _fmt_tp(coin, side, order_id, "TP1", price, entry, pnl, 1.0,
                            "建議平倉 ⅓ · SL 上移至保本 (含手續費)",
                            True, False, False),
                    reply_to_id=reply_id,
                    buttons=_get_order_button(order_id),
                )
                _record_trade(coin, side, order_id, entry, price, "TP1", sig["score"])
                self._save()
                logging.info(f"🥇 {order_id} TP1 確認 @ {price:.4f}")
            else:
                # 沒過線，清 pending
                if sig.get("pending_trigger") == "TP1":
                    self._clear_pending(sig)
                    self._save()

        # 📌 TP2 (必須先 TP1)
        if sig["hit_tp1"] and not sig["hit_tp2"]:
            tp2_cross = (side == "LONG" and price >= tp2) or (side == "SHORT" and price <= tp2)
            if tp2_cross:
                if not self._confirm_trigger(sig, "TP2"):
                    return False
                sig["hit_tp2"] = True
                sig["sl"] = tp1   # 鎖利到 TP1
                sig["status"] = "TRAIL"
                sig["pending_trigger"] = None
                pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
                send_tg(
                    _fmt_tp(coin, side, order_id, "TP2", price, entry, pnl, 2.5,
                            "建議平倉 ⅓ · SL 鎖利到 TP1",
                            True, True, False),
                    reply_to_id=reply_id,
                    buttons=_get_order_button(order_id),
                )
                _record_trade(coin, side, order_id, entry, price, "TP2", sig["score"])
                self._save()
                logging.info(f"🥈 {order_id} TP2 確認 @ {price:.4f}")
            else:
                if sig.get("pending_trigger") == "TP2":
                    self._clear_pending(sig)
                    self._save()

        # 📌 TP3 (必須先 TP2)
        if sig["hit_tp2"] and not sig["hit_tp3"]:
            tp3_cross = (side == "LONG" and price >= tp3) or (side == "SHORT" and price <= tp3)
            if tp3_cross:
                if not self._confirm_trigger(sig, "TP3"):
                    return False
                sig["hit_tp3"] = True
                pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
                send_tg(
                    _fmt_tp(coin, side, order_id, "TP3", price, entry, pnl, 4.0,
                            "🎉 建議全部平倉，完美收割！",
                            True, True, True),
                    reply_to_id=reply_id,
                    buttons=_get_order_button(order_id),
                )
                _record_trade(coin, side, order_id, entry, price, "TP3", sig["score"])
                logging.info(f"🏆 {order_id} TP3 確認 @ {price:.4f} — 訂單結束")
                return True
            else:
                if sig.get("pending_trigger") == "TP3":
                    self._clear_pending(sig)
                    self._save()

        return False

    def get_position_stats(self) -> str:
        positions = []
        for sig in self.signals.values():
            if sig["status"] in ("ACTIVE", "BE", "TRAIL", "PENDING"):
                sig = {**sig, "current_price": fetch_price(sig["instId"])}
                positions.append(sig)
        if not positions:
            return "📭 *目前無持倉*\n\n🔄 _系統持續掃描中_"

        msg = f"📊 *追蹤中訊號* (`{len(positions)}` 筆)\n━━━━━━━━━━━━━━━━━━\n\n"
        for i, p in enumerate(positions):
            coin = p["instId"].split("-")[0]
            side_em = "🟢" if p["side"] == "LONG" else "🔴"
            cp = p["current_price"]
            if cp > 0:
                pnl = ((cp - p["entry"]) / p["entry"] * 100) if p["side"] == "LONG" \
                    else ((p["entry"] - cp) / p["entry"] * 100)
                pnl_emoji = "🟢" if pnl >= 0 else "🔴"
                pnl_str = f"{pnl_emoji} `{pnl:+.2f}%`"
            else:
                pnl_str = "—"
            progress = _progress_icons(p.get("hit_tp1", False),
                                       p.get("hit_tp2", False),
                                       p.get("hit_tp3", False))
            msg += (
                f"{side_em} *{coin}* · {p['side']} · `{p['status']}` {progress}\n"
                f"🆔 `{p.get('order_id', 'N/A')}`  ·  評分 `{p.get('score', 0)}`\n"
                f"進場 `{p['entry']:.4f}` → 現價 `{cp:.4f}` {pnl_str}\n"
                f"🛑 `{p['sl']:.4f}`  "
                f"🥇 `{p['tp1']:.4f}`{'✅' if p.get('hit_tp1') else ''}  "
                f"🥈 `{p['tp2']:.4f}`{'✅' if p.get('hit_tp2') else ''}  "
                f"🏆 `{p['tp3']:.4f}`{'✅' if p.get('hit_tp3') else ''}\n"
            )
            if i < len(positions) - 1:
                msg += "\n━━━━━━━━━━━━━━━━━━\n\n"
        return msg


# ═════════════════════════════════════════════════════════
# 10. 交易歷史 + 戰績
# ═════════════════════════════════════════════════════════
def _record_trade(coin, side, order_id, entry, close_price, close_type, score):
    is_win = close_type in ("TP1", "TP2", "TP3")
    is_be = close_type == "BE"
    pnl = ((close_price - entry) / entry * 100) if side == "LONG" \
        else ((entry - close_price) / entry * 100)
    trade = {
        "time": get_tw_time(), "date": get_tw_date(),
        "order_id": order_id, "coin": coin, "side": side,
        "entry": entry, "close": close_price, "close_type": close_type,
        "pnl": round(pnl, 2), "is_win": is_win, "is_be": is_be, "score": score,
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


def get_daily_summary(days: int = 1) -> str:
    """📊 每日戰績"""
    if not os.path.exists(TRADE_HISTORY_FILE):
        return "📭 *尚無交易紀錄*"
    try:
        with open(TRADE_HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
    except Exception:
        return "⚠️ *戰績檔讀取失敗*"

    cutoff = (datetime.now(timezone.utc) + timedelta(hours=8) - timedelta(days=days)).strftime("%Y-%m-%d")
    recent = [t for t in history if t.get("date", "") >= cutoff]
    if not recent:
        return f"📭 *最近 {days} 天無交易*"

    wins = sum(1 for t in recent if t["is_win"])
    losses = sum(1 for t in recent if not t["is_win"] and not t.get("is_be"))
    bes = sum(1 for t in recent if t.get("is_be"))
    total = len(recent)
    total_pnl = sum(t["pnl"] for t in recent)
    win_rate = (wins / total * 100) if total else 0
    avg_win = sum(t["pnl"] for t in recent if t["is_win"]) / wins if wins else 0
    avg_loss = sum(t["pnl"] for t in recent if not t["is_win"] and not t.get("is_be")) / losses if losses else 0
    best = max(recent, key=lambda t: t["pnl"])
    worst = min(recent, key=lambda t: t["pnl"])

    return (
        f"📊 *近 {days} 日戰績*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"總交易 `{total}` 筆  ·  勝率 `{win_rate:.1f}%`\n"
        f"🟢 勝 `{wins}`   🔴 負 `{losses}`   🔒 保本 `{bes}`\n"
        f"\n"
        f"📈 總盈虧 `{total_pnl:+.2f}%`\n"
        f"平均獲利 `{avg_win:+.2f}%`  ·  平均虧損 `{avg_loss:+.2f}%`\n"
        f"\n"
        f"🏆 最佳 `{best['coin']}` `{best['pnl']:+.2f}%`\n"
        f"💢 最差 `{worst['coin']}` `{worst['pnl']:+.2f}%`\n"
        f"━━━━━━━━━━━━━━━━━━"
    )


# ═════════════════════════════════════════════════════════
# 11. 主掃描
# ═════════════════════════════════════════════════════════
def run_scan(tracker: SignalTracker) -> int:
    logging.info("🚀 開始掃描")
    sent = 0

    for instId in ALL_COINS:
        if sent >= MAX_SIGNALS:
            break

        cool_key = f"{instId}_ALL"
        if cool_key in _signal_cooldown and time.time() - _signal_cooldown[cool_key] < 2 * 3600:
            continue

        try:
            current_price = fetch_price(instId)
            if current_price <= 0:
                continue
            logging.info(f"[{instId}] 現價 {current_price}")

            df = fetch_candles(instId)
            if df is None:
                continue
            signal = generate_signal(instId, df, current_price)
            if not signal:
                continue

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

            funding = fetch_funding_rate(instId)
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
                funding=funding,
            )
            entry_msg_id = send_tg(msg, reply_to_id=None,
                                   buttons=_get_order_button(order_id))
            if entry_msg_id:
                tracker.update_signal(key, entry_msg_id=entry_msg_id)

            _signal_cooldown[cool_key] = time.time()
            sent += 1
            logging.info(f"✅ {instId} 發送 {order_id} msg_id={entry_msg_id}")

        except Exception as e:
            logging.error(f"[{instId}] 掃描失敗: {e}")
            continue

    tracker.check_all()
    logging.info(f"🎯 本輪發送 {sent} 筆")
    return sent


# ═════════════════════════════════════════════════════════
# 12. 入口
# ═════════════════════════════════════════════════════════
def main():
    try:
        logging.info("=" * 50)
        logging.info("🤖 Alpha Oracle Pro v11.1 精緻專業版")
        logging.info("=" * 50)

        tracker = SignalTracker(ACTIVE_SIGNALS_FILE)

        if len(sys.argv) > 1:
            cmd = sys.argv[1]
            if cmd in ("/stats", "/持倉"):
                send_tg(tracker.get_position_stats())
                return
            if cmd in ("/daily", "/戰績"):
                send_tg(get_daily_summary(1))
                return
            if cmd in ("/weekly", "/週報"):
                send_tg(get_daily_summary(7))
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
