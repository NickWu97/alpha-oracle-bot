#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
price_monitor.py — Alpha Oracle Pro 即時價格監控（獨立輕量版）
════════════════════════════════════════════════════════
‣ 每分鐘由 Cowork 排程喚醒，一次內快速多輪輪詢
‣ 只做一件事：讀 active_signals.json → 逐筆比對 OKX 即時價格
              → 觸到 TP/SL 水位 → 立即發 Telegram
‣ 不做新訊號生成，不做評分計算，極度輕量
‣ 憑證來源優先順序：
    1. 環境變數 TG_TOKEN / CHAT_ID
    2. 同目錄 monitor_config.json
════════════════════════════════════════════════════════
"""
import os, json, sys, time, logging, requests, uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── 路徑 ──────────────────────────────────────────────
BASE_DIR           = Path(__file__).parent
ACTIVE_SIGNALS_FILE = BASE_DIR / "active_signals.json"
MONITOR_CONFIG_FILE = BASE_DIR / "monitor_config.json"
PRICE_LOG_FILE      = BASE_DIR / "price_monitor.log"

# ── Logging ───────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(PRICE_LOG_FILE, encoding="utf-8"),
    ],
)

TW_TZ = timezone(timedelta(hours=8))

def tw_now():  return datetime.now(TW_TZ)
def tw_ts():   return tw_now().strftime("%Y-%m-%d %H:%M:%S 台灣時間")

# ── 憑證 ─────────────────────────────────────────────
def _load_credentials() -> tuple[str, str]:
    tg = os.getenv("TG_TOKEN","").strip()
    ch = os.getenv("CHAT_ID","").strip()
    if tg and ch:
        return tg, ch
    if MONITOR_CONFIG_FILE.exists():
        try:
            cfg = json.loads(MONITOR_CONFIG_FILE.read_text())
            return cfg.get("TG_TOKEN",""), cfg.get("CHAT_ID","")
        except Exception:
            pass
    return "", ""

TG_TOKEN, CHAT_ID = _load_credentials()

# ── Telegram ──────────────────────────────────────────
def send_tg(msg: str, reply_to: int | None = None) -> int | None:
    if not TG_TOKEN or not CHAT_ID:
        logging.warning("⚠️  TG 憑證未設定，略過發送")
        return None
    payload = {
        "chat_id": CHAT_ID, "text": msg,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if reply_to:
        payload["reply_to_message_id"] = reply_to
        payload["allow_sending_without_reply"] = True
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json=payload, timeout=8,
        )
        if r.status_code == 200:
            return r.json().get("result",{}).get("message_id")
    except Exception as e:
        logging.error(f"TG 發送失敗：{e}")
    return None

# ── OKX 即時價格（帶快取，5 秒內不重複請求）─────────
_price_cache: dict = {}

def fetch_price(instId: str) -> float:
    now = time.time()
    if instId in _price_cache:
        p, t = _price_cache[instId]
        if now - t < 5:
            return p
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/market/ticker?instId={instId}",
            timeout=4,
        ).json()
        if res.get("code") == "0" and res.get("data"):
            p = float(res["data"][0]["last"])
            if p > 0:
                _price_cache[instId] = (p, now)
                return p
    except Exception as e:
        logging.warning(f"price fetch {instId}: {e}")
    return _price_cache.get(instId, (0.0, 0))[0]

# ── 訊號讀寫 ─────────────────────────────────────────
def load_signals() -> dict:
    if not ACTIVE_SIGNALS_FILE.exists():
        return {}
    try:
        return json.loads(ACTIVE_SIGNALS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logging.error(f"讀取 signals 失敗：{e}"); return {}

def save_signals(signals: dict) -> None:
    tmp = str(ACTIVE_SIGNALS_FILE) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(signals, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(ACTIVE_SIGNALS_FILE))
    except Exception as e:
        logging.error(f"寫入 signals 失敗：{e}")

# ── 通知格式 ─────────────────────────────────────────
def _fmt_tp_alert(sig, tp_label, trigger_price, pnl_pct, r_mult, wick=False):
    coin = sig["instId"].split("-")[0]
    dir_ = "做多" if sig["side"]=="LONG" else "做空"
    wn   = "\n🪡 插針觸發" if wick else ""
    pct_map = {"TP1":"出場 30%","TP2":"再出 30%","TP3":"全數出場 🏆"}
    return (
        f"🎯 *{coin} {tp_label} 達標！*\n"
        f"━━━━━━━━━━━━━━\n"
        f"🆔 `{sig.get('order_id','?')}`\n"
        f"⏰ {tw_ts()}\n"
        f"方向：{dir_}　觸發：`{trigger_price:.4f}`{wn}\n"
        f"獲利：`{pnl_pct:+.2f}%`（`{r_mult:+.1f}R`）\n"
        f"💡 建議{pct_map.get(tp_label,'平倉')}"
    )

def _fmt_sl_alert(sig, trigger_price, pnl_pct, mode, r_val, wick=False):
    coin  = sig["instId"].split("-")[0]
    dir_  = "做多" if sig["side"]=="LONG" else "做空"
    wn    = "\n🪡 插針觸發" if wick else ""
    label = {"BE":"🔒 保本出場","LOCK":"🔐 鎖利出場","LOSS":"❌ 止損離場"}.get(mode,"❌ 止損")
    r_tag = f"`{r_val:+.1f}R`"
    return (
        f"{label} *{coin}*\n"
        f"━━━━━━━━━━━━━━\n"
        f"🆔 `{sig.get('order_id','?')}`\n"
        f"⏰ {tw_ts()}\n"
        f"方向：{dir_}　觸發：`{trigger_price:.4f}`{wn}\n"
        f"結果：`{pnl_pct:+.2f}%` {r_tag}"
    )

def _fmt_price_update(sig, price):
    coin  = sig["instId"].split("-")[0]
    entry = sig["entry"]
    side  = sig["side"]
    pnl   = (price-entry)/entry*100 if side=="LONG" else (entry-price)/entry*100
    emoji = "🟢" if pnl >= 0 else "🔴"
    dist_tp1 = abs(price - sig["tp1"])
    dist_sl  = abs(price - sig["sl"])
    pct_tp1  = dist_tp1 / entry * 100
    pct_sl   = dist_sl  / entry * 100
    return (
        f"📡 *{coin}* 即時追蹤\n"
        f"現價 `{price:.4f}` {emoji}{pnl:+.2f}%\n"
        f"TP1距離 `{pct_tp1:.2f}%` | SL距離 `{pct_sl:.2f}%`"
    )

# ── 核心：逐筆 K 線型態比對 ─────────────────────────
def check_signal(sig: dict, price: float) -> dict | None:
    """
    回傳 None = 無狀態變化
    回傳 dict = {"action":"TP1"/"TP2"/"TP3"/"BE"/"LOCK"/"LOSS", ...}
    判斷邏輯：
      高/低點觸及 → wick 觸發
      收盤超過    → 正常觸發
    這裡用即時 ticker 的 last price（15s 內最接近的收盤代理）
    """
    side  = sig.get("side")
    entry = sig.get("entry",0)
    sl    = sig.get("sl",0)
    tp1, tp2, tp3 = sig.get("tp1",0), sig.get("tp2",0), sig.get("tp3",0)
    if not entry or not sl:
        return None

    # 方向函式
    if side == "LONG":
        favor   = lambda t: price >= t
        against = lambda t: price <= t
    else:
        favor   = lambda t: price <= t
        against = lambda t: price >= t

    # TP1
    if not sig.get("hit_tp1") and favor(tp1):
        pnl = (tp1-entry)/entry*100 if side=="LONG" else (entry-tp1)/entry*100
        return {"action":"TP1","price":tp1,"pnl":pnl,"r":1.5,
                "new_sl":entry,"new_status":"BE"}
    # TP2
    if not sig.get("hit_tp2") and favor(tp2):
        pnl = (tp2-entry)/entry*100 if side=="LONG" else (entry-tp2)/entry*100
        return {"action":"TP2","price":tp2,"pnl":pnl,"r":3.0,
                "new_sl":tp1,"new_status":"TRAIL"}
    # TP3
    if not sig.get("hit_tp3") and favor(tp3):
        pnl = (tp3-entry)/entry*100 if side=="LONG" else (entry-tp3)/entry*100
        return {"action":"TP3","price":tp3,"pnl":pnl,"r":5.0}
    # SL
    if against(sl):
        pnl = (sl-entry)/entry*100 if side=="LONG" else (entry-sl)/entry*100
        if sig.get("hit_tp2"):
            mode, r_val = "LOCK", 1.5
        elif sig.get("hit_tp1"):
            mode, r_val = "BE", 0.0
        else:
            mode, r_val = "LOSS", -1.0
        return {"action":mode,"price":sl,"pnl":pnl,"r":r_val}
    return None

# ── 接近水位預警（距離 < 0.3%）──────────────────────
def check_proximity(sig: dict, price: float) -> str | None:
    entry = sig.get("entry",0)
    if not entry:
        return None
    side = sig.get("side")
    if side == "LONG":
        targets = [
            ("TP1", sig.get("tp1",0), "🔔"),
            ("TP2", sig.get("tp2",0), "🔔"),
            ("SL",  sig.get("sl",0),  "⚠️"),
        ]
    else:
        targets = [
            ("TP1", sig.get("tp1",0), "🔔"),
            ("TP2", sig.get("tp2",0), "🔔"),
            ("SL",  sig.get("sl",0),  "⚠️"),
        ]
    alerts = []
    for label, tgt, emoji in targets:
        if tgt <= 0: continue
        pct = abs(price - tgt) / entry * 100
        if pct < 0.30:
            alerts.append(f"{emoji} {label} 僅剩 `{pct:.2f}%`（`{tgt:.4f}`）")
    if not alerts:
        return None
    coin = sig["instId"].split("-")[0]
    return (
        f"📍 *{coin}* 接近水位警示\n"
        f"⏰ {tw_ts()}\n"
        f"現價：`{price:.4f}`\n" +
        "\n".join(alerts)
    )

# ── 主監控循環 ────────────────────────────────────────
def monitor_once(signals: dict, prox_notified: set) -> tuple[dict, set]:
    """
    執行一輪掃描：
      1. 抓所有 ACTIVE/BE/TRAIL 訊號的即時價格
      2. 若觸價 → 即時發 TG、更新狀態
      3. 若接近水位（0.3% 內）且未通知過 → 發預警
    回傳更新後的 signals 與已通知集合
    """
    to_remove = []
    for key, sig in list(signals.items()):
        status = sig.get("status","")
        if status not in ("ACTIVE","BE","TRAIL"):
            continue
        price = fetch_price(sig["instId"])
        if price <= 0:
            continue
        sig["current_price"] = price
        result = check_signal(sig, price)
        reply_to = sig.get("entry_message_id")

        if result:
            action = result["action"]
            tprice = result["price"]
            pnl    = result["pnl"]
            r      = result["r"]

            if action in ("TP1","TP2","TP3"):
                send_tg(_fmt_tp_alert(sig, action, tprice, pnl, r), reply_to=reply_to)
                logging.info(f"🎯 {sig['instId']} {action} 達標 @ {tprice:.4f}")
                if action == "TP1":
                    sig["hit_tp1"]  = True
                    sig["sl"]       = result["new_sl"]
                    sig["status"]   = result["new_status"]
                elif action == "TP2":
                    sig["hit_tp2"]  = True
                    sig["sl"]       = result["new_sl"]
                    sig["status"]   = result["new_status"]
                elif action == "TP3":
                    sig["hit_tp3"]  = True
                    to_remove.append(key)
            elif action in ("BE","LOCK","LOSS"):
                send_tg(_fmt_sl_alert(sig, tprice, pnl, action, r), reply_to=reply_to)
                logging.info(f"🛑 {sig['instId']} {action} @ {tprice:.4f}")
                to_remove.append(key)
            # 移除「已通知」的接近警示（已觸價了）
            prox_notified.discard(key)

        else:
            # 接近水位預警（每個 key 只通知一次，觸價後重置）
            prox_msg = check_proximity(sig, price)
            if prox_msg and key not in prox_notified:
                send_tg(prox_msg, reply_to=reply_to)
                prox_notified.add(key)
                logging.info(f"📍 {sig['instId']} 接近水位預警 @ {price:.4f}")

    for key in to_remove:
        signals.pop(key, None)

    return signals, prox_notified


# ── 入口：支援帶參數的多輪輪詢 ──────────────────────
# 用法：python price_monitor.py [polls] [interval_sec]
# 預設：6 輪 × 9 秒 = ~54 秒（覆蓋整個 1 分鐘 cron 窗口）
def main():
    polls    = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    interval = int(sys.argv[2]) if len(sys.argv) > 2 else 9

    if not TG_TOKEN or not CHAT_ID:
        logging.error("❌ 缺少 TG_TOKEN / CHAT_ID，請設定環境變數或 monitor_config.json")
        sys.exit(1)

    logging.info(f"🔍 監控啟動 — {polls} 輪 × {interval}s ≈ {polls*interval}s 覆蓋")
    signals      = load_signals()
    active_count = sum(1 for s in signals.values() if s.get("status") in ("ACTIVE","BE","TRAIL"))

    if active_count == 0:
        logging.info("📭 目前無活躍訊號，本輪結束")
        return

    logging.info(f"📊 活躍訊號：{active_count} 筆")
    prox_notified: set = set()

    for i in range(polls):
        if not any(s.get("status") in ("ACTIVE","BE","TRAIL") for s in signals.values()):
            logging.info("✅ 所有訊號已結算，提早結束")
            break
        logging.info(f"  輪 {i+1}/{polls} — {tw_ts()}")
        signals, prox_notified = monitor_once(signals, prox_notified)
        save_signals(signals)
        if i < polls - 1:
            time.sleep(interval)

    logging.info("✅ 本輪監控完成")

if __name__ == "__main__":
    main()
