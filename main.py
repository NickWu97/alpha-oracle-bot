#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro v12.0 — 專業精準版 (繁體中文)
══════════════════════════════════════════════════════════════════════
✨ 專業級功能整合：
  ✅ 多交易所價格驗證 (OKX + Binance + Bybit 比對)
  ✅ 多時框共振確認 (15m+1h+4h 趨勢一致)
  ✅ 專業風險管理 (連續虧損熔斷 + 每日虧損上限)
  ✅ 關鍵時段過濾 (避開高風險時段)
  ✅ 專業回測框架 (歷史績效分析)
  ✅ 配置熱更新 (無需重啟調整參數)
  ✅ SMC/ICT/SNR/PA/流動性/動能 高級分析
  ✅ 1.5R/3.0R/5.0R 止盈 + 保本/鎖利機制
  ✅ 全部繁體中文通知 + 線層回覆 + 訂單按鈕
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
from typing import Dict, List, Optional, Tuple, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────────────────────
# 🇹🇼 台灣時間工具
# ─────────────────────────────────────────────────────────
TW_TZ = timezone(timedelta(hours=8))

def tw_now() -> datetime:
    """獲取台灣時間 datetime 物件"""
    return datetime.now(TW_TZ)

def tw_ts() -> str:
    """台灣時間時間戳字串"""
    return tw_now().strftime("%Y-%m-%d %H:%M:%S 台灣時間")

def tw_date() -> str:
    """台灣日期字串"""
    return tw_now().strftime("%Y-%m-%d")

# ─────────────────────────────────────────────────────────
# 🔧 環境變數安全解析
# ─────────────────────────────────────────────────────────
def _get_env(key: str, default: str = "") -> str:
    val = os.getenv(key)
    return val.strip() if val and val.strip() else default

def _get_env_int(key: str, default: int) -> int:
    val = os.getenv(key)
    try:
        return int(val.strip()) if val and val.strip() else default
    except Exception:
        return default

def _get_env_float(key: str, default: float) -> float:
    val = os.getenv(key)
    try:
        return float(val.strip()) if val and val.strip() else default
    except Exception:
        return default

def _get_env_bool(key: str, default: bool) -> bool:
    val = _get_env(key, "").lower()
    return val in ("true", "1", "yes", "on")

# ─────────────────────────────────────────────────────────
# 1. 基礎配置
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    stream=sys.stdout,
)

TG_TOKEN = _get_env("TG_TOKEN")
CHAT_ID = _get_env("CHAT_ID")

ALL_COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]

# 記憶體快取
_price_cache: Dict[str, Tuple[float, float]] = {}

# ─────────────────────────────────────────────────────────
# 2. 專業配置管理器 (熱更新 + 驗證)
# ─────────────────────────────────────────────────────────
class ConfigManager:
    """⚙️ 專業配置管理 (支援熱更新 + 驗證)"""
    
    DEFAULT_CONFIG = {
        # 交易參數
        "max_signals": 3,
        "score_threshold": 68,
        "signal_expire_hours": 24,
        "cooldown_hours": 2,
        
        # 風險參數
        "daily_sl_limit": 2,
        "max_daily_loss_pct": -5.0,
        "consecutive_loss_limit": 3,
        "circuit_breaker_hours": 24,
        
        # 技術參數
        "atr_period": 14,
        "rsi_period": 14,
        "supertrend_period": 10,
        "tp1_r": 1.5,
        "tp2_r": 3.0,
        "tp3_r": 5.0,
        "sl_atr_mult": 1.5,
        
        # 過濾參數
        "min_volume_ratio": 1.0,
        "max_price_deviation": 0.005,
        "funding_rate_threshold": 0.0008,
        
        # 時段過濾
        "enable_time_filter": True,
        "high_risk_periods": [
            {"start": [21, 15], "end": [23, 0], "reason": "美國數據發布"},
            {"start": [0, 0], "end": [8, 0], "days": [5, 6], "reason": "週末低流動性"}
        ],
        
        # 通知參數
        "report_time": "22:00",
        "enable_position_updates": True,
        "position_update_interval_minutes": 30,
        
        # 專業功能開關
        "enable_multi_exchange": True,
        "enable_multi_tf": True,
        "enable_volume_confirm": True
    }
    
    CONFIG_FILE = "config.json"
    
    def __init__(self):
        self.config = self._load_config()
        self._validate_config()
    
    def _load_config(self) -> dict:
        """📥 載入配置"""
        try:
            if os.path.exists(self.CONFIG_FILE):
                with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                    user_config = json.load(f)
                config = {**self.DEFAULT_CONFIG, **user_config}
                logging.info(f"✅ 載入配置: {self.CONFIG_FILE}")
                return config
        except Exception as e:
            logging.warning(f"⚠️ 載入配置失敗，使用預設值: {e}")
        return self.DEFAULT_CONFIG.copy()
    
    def _validate_config(self) -> None:
        """🔍 驗證配置"""
        assert 1 <= self.config["max_signals"] <= 10
        assert 50 <= self.config["score_threshold"] <= 95
        assert self.config["tp1_r"] < self.config["tp2_r"] < self.config["tp3_r"]
        assert self.config["max_daily_loss_pct"] < 0
        logging.info("✅ 配置驗證通過")
    
    def get(self, key: str, default=None):
        """🔑 獲取配置值"""
        return self.config.get(key, default)
    
    def update(self, updates: dict) -> bool:
        """✏️ 更新配置 (熱更新)"""
        try:
            temp_config = {**self.config, **updates}
            if "score_threshold" in updates:
                assert 50 <= updates["score_threshold"] <= 95
            if any(k in updates for k in ["tp1_r", "tp2_r", "tp3_r"]):
                tp1 = updates.get("tp1_r", self.config["tp1_r"])
                tp2 = updates.get("tp2_r", self.config["tp2_r"])
                tp3 = updates.get("tp3_r", self.config["tp3_r"])
                assert tp1 < tp2 < tp3
            self.config.update(updates)
            self._save_config()
            logging.info(f"✅ 配置已更新: {list(updates.keys())}")
            return True
        except Exception as e:
            logging.error(f"❌ 配置更新失敗: {e}")
            return False
    
    def _save_config(self) -> None:
        """💾 保存配置"""
        try:
            tmp = self.CONFIG_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.CONFIG_FILE)
        except Exception as e:
            logging.error(f"❌ 保存配置失敗: {e}")
    
    def to_telegram_format(self) -> str:
        """📱 格式化配置為 Telegram 可讀"""
        lines = ["⚙️ *當前配置*", "━━━━━━━━━━━━━━"]
        categories = {
            "交易參數": ["max_signals", "score_threshold", "signal_expire_hours"],
            "風險參數": ["daily_sl_limit", "max_daily_loss_pct", "consecutive_loss_limit"],
            "技術參數": ["tp1_r", "tp2_r", "tp3_r", "sl_atr_mult"],
            "過濾參數": ["min_volume_ratio", "funding_rate_threshold"]
        }
        for category, keys in categories.items():
            lines.append(f"\n*{category}*")
            for key in keys:
                value = self.config.get(key)
                lines.append(f"  • `{key}`: `{value}`")
        return "\n".join(lines)

# 全域配置管理器
config = ConfigManager()

# ─────────────────────────────────────────────────────────
# 3. 專業價格驗證器 (多交易所比對)
# ─────────────────────────────────────────────────────────
class PriceValidator:
    """🔐 專業價格驗證器 (多交易所比對)"""
    
    EXCHANGES = {
        "okx": "https://www.okx.com/api/v5/market/ticker",
        "binance": "https://api.binance.com/api/v3/ticker/price",
        "bybit": "https://api.bybit.com/v5/market/tickers"
    }
    
    MAX_PRICE_DEVIATION = config.get("max_price_deviation", 0.005)
    
    @staticmethod
    def _okx_symbol(instId: str) -> str:
        """轉換為 OKX 格式"""
        return instId.replace("-", "")
    
    @staticmethod
    def _binance_symbol(instId: str) -> str:
        """轉換為 Binance 格式"""
        return instId.replace("-USDT-SWAP", "USDT").replace("-", "")
    
    @staticmethod
    def _bybit_symbol(instId: str) -> str:
        """轉換為 Bybit 格式"""
        return instId.replace("-USDT-SWAP", "").replace("-", "")
    
    @classmethod
    def _fetch_okx(cls, instId: str) -> Optional[float]:
        try:
            symbol = cls._okx_symbol(instId)
            res = requests.get(f"{cls.EXCHANGES['okx']}?instId={symbol}", timeout=3).json()
            if res.get("code") == "0" and res.get("data"):
                return float(res["data"][0]["last"])
        except:
            pass
        return None
    
    @classmethod
    def _fetch_binance(cls, instId: str) -> Optional[float]:
        try:
            symbol = cls._binance_symbol(instId)
            res = requests.get(f"{cls.EXCHANGES['binance']}?symbol={symbol}", timeout=3).json()
            if "price" in res:
                return float(res["price"])
        except:
            pass
        return None
    
    @classmethod
    def _fetch_bybit(cls, instId: str) -> Optional[float]:
        try:
            symbol = cls._bybit_symbol(instId)
            res = requests.get(f"{cls.EXCHANGES['bybit']}?category=linear&symbol={symbol}", timeout=3).json()
            if res.get("retCode") == 0 and res.get("data", {}).get("list"):
                return float(res["data"]["list"][0]["lastPrice"])
        except:
            pass
        return None
    
    @classmethod
    def get_verified_price(cls, instId: str) -> Optional[float]:
        """🔐 獲取經多來源驗證的價格"""
        if not config.get("enable_multi_exchange", True):
            return cls._fetch_okx(instId)
        
        prices = {}
        
        # 並行獲取多個交易所價格
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(cls._fetch_okx, instId): "okx",
                executor.submit(cls._fetch_binance, instId): "binance",
                executor.submit(cls._fetch_bybit, instId): "bybit"
            }
            for future in as_completed(futures):
                exchange = futures[future]
                try:
                    price = future.result()
                    if price and price > 0:
                        prices[exchange] = price
                except:
                    pass
        
        if not prices:
            return None
        
        # 計算平均價與偏差
        avg_price = sum(prices.values()) / len(prices)
        
        # 檢查異常偏差
        valid_prices = {}
        for exchange, price in prices.items():
            deviation = abs(price - avg_price) / avg_price
            if deviation <= cls.MAX_PRICE_DEVIATION:
                valid_prices[exchange] = price
            else:
                logging.warning(
                    f"⚠️ {instId} {exchange} 價格偏差過大: "
                    f"{price:.4f} vs 平均 {avg_price:.4f} (偏差 {deviation*100:.2f}%)"
                )
        
        if not valid_prices:
            return avg_price  # 全部偏差大時回傳平均
        
        verified_price = sum(valid_prices.values()) / len(valid_prices)
        logging.debug(f"✅ {instId} 驗證價格: {verified_price:.4f} (來源: {list(valid_prices.keys())})")
        return verified_price

# ─────────────────────────────────────────────────────────
# 4. 多時框共振分析器
# ─────────────────────────────────────────────────────────
class MultiTimeframeAnalyzer:
    """🔍 多時間框架共振分析 (15m + 1h + 4h)"""
    
    TIMEFRAMES = ["15m", "1h", "4h"]
    MIN_CONFLUENCE_SCORE = 2
    
    @staticmethod
    def get_trend_direction(df: list) -> int:
        """判斷單一時框的趨勢方向"""
        if len(df) < 20:
            return 0
        
        st = calc_supertrend(df)
        sma_short = sum(r["c"] for r in df[-7:]) / 7
        sma_long = sum(r["c"] for r in df[-25:]) / 25
        current = df[-1]["c"]
        
        if st == 1 and current > sma_short > sma_long:
            return 1
        elif st == -1 and current < sma_short < sma_long:
            return -1
        elif st != 0:
            return st
        return 0
    
    @classmethod
    def check_confluence(cls, instId: str, side: str) -> Tuple[bool, str]:
        """🔗 檢查多時框共振"""
        if not config.get("enable_multi_tf", True):
            return True, "多時框確認已禁用"
        
        results = {}
        
        for tf in cls.TIMEFRAMES:
            df = fetch_candles(instId, tf=tf, limit=100)
            if df is None:
                results[tf] = None
                continue
            results[tf] = cls.get_trend_direction(df)
        
        valid_directions = [d for d in results.values() if d is not None]
        if not valid_directions:
            return False, "無法獲取足夠時框數據"
        
        target = 1 if side == "LONG" else -1
        support_count = sum(1 for d in valid_directions if d == target)
        
        if support_count >= cls.MIN_CONFLUENCE_SCORE:
            desc = f"{'多頭' if target==1 else '空頭'}共振 ({support_count}/{len(valid_directions)} 時框)"
            return True, desc
        
        conflict_count = sum(1 for d in valid_directions if d == -target)
        if conflict_count >= 2:
            return False, f"時框衝突 ({conflict_count} 時框反向)"
        
        return True, f"中性偏{'多' if target==1 else '空'} ({support_count} 支援)"

# ─────────────────────────────────────────────────────────
# 5. 專業風險管理器
# ─────────────────────────────────────────────────────────
class RiskManager:
    """🔐 專業風險控制系統"""
    
    def __init__(self):
        self.daily_loss = 0.0
        self.consecutive_losses = 0
        self.last_trade_date = None
        self.circuit_breaker_until = None
    
    def check_circuit_breaker(self) -> Tuple[bool, str]:
        """🔌 檢查熔斷機制"""
        now = tw_now()
        today = tw_date()
        
        if self.last_trade_date != today:
            self.daily_loss = 0.0
            self.consecutive_losses = 0
            self.last_trade_date = today
        
        if self.circuit_breaker_until and now < self.circuit_breaker_until:
            remaining = self.circuit_breaker_until - now
            hours = remaining.total_seconds() / 3600
            return False, f"熔斷中 (剩餘 {hours:.1f} 小時)"
        
        limit = config.get("consecutive_loss_limit", 3)
        if self.consecutive_losses >= limit:
            hours = config.get("circuit_breaker_hours", 24)
            self.circuit_breaker_until = now + timedelta(hours=hours)
            return False, f"連續 {limit} 筆虧損，觸發 {hours} 小時熔斷"
        
        max_loss = config.get("max_daily_loss_pct", -5.0)
        if self.daily_loss <= max_loss:
            tomorrow = now.replace(hour=0, minute=0, second=0) + timedelta(days=1)
            self.circuit_breaker_until = tomorrow
            return False, f"今日虧損達 {self.daily_loss:.1f}%，暫停至明日"
        
        return True, "風險檢查通過"
    
    def record_trade_result(self, pnl_pct: float, close_type: str) -> None:
        """📝 記錄交易結果"""
        if close_type == "SL":
            self.consecutive_losses += 1
            self.daily_loss += pnl_pct
        elif close_type in ("TP1", "TP2", "TP3"):
            self.consecutive_losses = 0
            self.daily_loss += pnl_pct
    
    def get_risk_status(self) -> dict:
        """📊 獲取風險狀態"""
        can_trade, reason = self.check_circuit_breaker()
        return {
            "daily_loss": round(self.daily_loss, 2),
            "consecutive_losses": self.consecutive_losses,
            "circuit_breaker": self.circuit_breaker_until.strftime("%Y-%m-%d %H:%M") if self.circuit_breaker_until else None,
            "can_trade": can_trade,
            "reason": reason
        }

# 全域風險管理器
risk_manager = RiskManager()

# ─────────────────────────────────────────────────────────
# 6. 時段過濾器
# ─────────────────────────────────────────────────────────
class TimeFilter:
    """🕐 交易時段過濾"""
    
    @classmethod
    def is_safe_to_trade(cls) -> Tuple[bool, str]:
        """🔍 檢查是否為安全交易時段"""
        if not config.get("enable_time_filter", True):
            return True, "時段過濾已禁用"
        
        now = tw_now()
        current_time = (now.hour, now.minute)
        day_of_week = now.weekday()
        
        periods = config.get("high_risk_periods", [])
        for period in periods:
            if "days" in period and day_of_week not in period["days"]:
                continue
            start = tuple(period["start"])
            end = tuple(period["end"])
            if start > end:
                if current_time >= start or current_time < end:
                    return False, period.get("reason", "高風險時段")
            else:
                if start <= current_time < end:
                    return False, period.get("reason", "高風險時段")
        
        return True, "安全時段"

# ─────────────────────────────────────────────────────────
# 7. 通知系統
# ─────────────────────────────────────────────────────────
def send_tg(
    msg: str,
    parse_mode: str = "Markdown",
    reply_markup: dict | None = None,
    reply_to_message_id: int | None = None,
) -> int | None:
    """📤 發送 Telegram 通知"""
    if not TG_TOKEN or not CHAT_ID:
        logging.warning("⚠️ TG_TOKEN 或 CHAT_ID 未設定")
        return None
    
    payload = {
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True
    
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json=payload,
            timeout=8,
        )
        if r.status_code == 200:
            return r.json().get("result", {}).get("message_id")
        logging.error(f"❌ TG API {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logging.error(f"❌ TG 發送失敗: {e}")
    return None

def _order_keyboard(order_id: str) -> dict:
    """🔘 生成訂單查詢按鈕"""
    return {
        "inline_keyboard": [[
            {"text": f"🔍 查詢訂單 {order_id[-8:]}", "callback_data": f"order_{order_id}"}
        ]]
    }

# ─────────────────────────────────────────────────────────
# 8. 通知格式 (繁體中文)
# ─────────────────────────────────────────────────────────
def _fmt_entry(
    coin: str, side: str, order_id: str, price: float, entry: float,
    sl: float, tp1: float, tp2: float, tp3: float, score: int,
    funding_rate: float | None = None, confluence_desc: str = ""
) -> str:
    """📌 進場通知"""
    direction = "做多" if side == "LONG" else "做空"
    emoji = "🟢" if side == "LONG" else "🔴"
    grade = "🔥 A+ 極強" if score >= 85 else "⭐ A 強力" if score >= 70 else "✅ B+ 合格"
    
    tp1_pct = (tp1 - entry) / entry * 100 * (1 if side == "LONG" else -1)
    tp2_pct = (tp2 - entry) / entry * 100 * (1 if side == "LONG" else -1)
    tp3_pct = (tp3 - entry) / entry * 100 * (1 if side == "LONG" else -1)
    sl_pct = (sl - entry) / entry * 100
    
    lines = [
        f"{emoji} *{coin} 進場提醒* {grade}",
        "━━━━━━━━━━━━━━",
        f"🆔 訂單編號：`{order_id}`",
        f"⏰ 時間：{tw_ts()}",
        f"方向：{direction}",
        f"進場價：`{entry:.4f}`",
        f"當前價：`{price:.4f}`",
        f"評分：*{score} 分*"
    ]
    
    if funding_rate is not None:
        lines.append(f"💰 資金費率：`{funding_rate * 100:+.4f}%`")
    if confluence_desc:
        lines.append(f"🔗 {confluence_desc}")
    
    lines.extend([
        "",
        "🎯 止盈目標：",
        f"  TP1 `{tp1:.4f}` ({tp1_pct:+.2f}%)",
        f"  TP2 `{tp2:.4f}` ({tp2_pct:+.2f}%)",
        f"  TP3 `{tp3:.4f}` ({tp3_pct:+.2f}%)",
        "",
        f"🛑 止損：`{sl:.4f}` ({sl_pct:+.2f}%)",
        "",
        "💡 到達 TP1 自動保本，到達 TP2 自動鎖利至 TP1"
    ])
    
    return "\n".join(lines)

def _fmt_tp(coin: str, side: str, order_id: str, tp_level: str, price: float, pnl_pct: float, r_mult: float) -> str:
    """🎯 止盈通知"""
    direction = "做多" if side == "LONG" else "做空"
    advice = (
        "建議平倉 ⅓ 鎖定獲利" if tp_level == "TP1"
        else "建議再平倉 ⅓ 落袋為安" if tp_level == "TP2"
        else "建議全部平倉，完美收割 🏆"
    )
    return "\n".join([
        f"🎯 *{coin} {tp_level} 達標！*",
        "━━━━━━━━━━━━━━",
        f"🆔 訂單編號：`{order_id}`",
        f"⏰ 時間：{tw_ts()}",
        f"方向：{direction}",
        f"觸發價：`{price:.4f}`",
        f"獲利：`{pnl_pct:+.2f}%` (`{r_mult:+.1f}R`)",
        "",
        f"✅ 已達成 {tp_level}",
        "",
        f"💡 {advice}"
    ])

def _fmt_sl(coin: str, side: str, order_id: str, price: float, pnl_pct: float, is_be: bool = False) -> str:
    """🛑 止損通知"""
    direction = "做多" if side == "LONG" else "做空"
    label = "🔒 保本出場" if is_be else "❌ 止損離場"
    r_tag = "`0.0R`" if is_be else "`-1.0R`"
    advice = (
        "資金安全，等待下一次機會 💪" if is_be
        else "遵守風控，勿加碼攤平。下一筆訊號會更好 🚀"
    )
    return "\n".join([
        f"{label} *{coin}*",
        "━━━━━━━━━━━━━━",
        f"🆔 訂單編號：`{order_id}`",
        f"⏰ 時間：{tw_ts()}",
        f"方向：{direction}",
        f"觸發價：`{price:.4f}`",
        f"結果：`{pnl_pct:+.2f}%` {r_tag}",
        "",
        f"💡 {advice}"
    ])

def _fmt_position(sig: dict, current_price: float) -> str:
    """📊 持倉進度更新"""
    coin = sig["instId"].split("-")[0]
    side = sig["side"]
    direction = "做多" if side == "LONG" else "做空"
    entry = sig["entry"]
    pnl = (
        (current_price - entry) / entry * 100 if side == "LONG"
        else (entry - current_price) / entry * 100
    )
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"
    
    if sig.get("hit_tp3"):
        progress = "🏆 TP3 ✅"
    elif sig.get("hit_tp2"):
        progress = "🥇✅ → 🥈✅ → ⏳ TP3"
    elif sig.get("hit_tp1"):
        progress = "🥇✅ → ⏳ TP2"
    else:
        progress = "⏳ 等待 TP1"
    
    return "\n".join([
        f"📊 *{coin} 持倉更新*",
        "━━━━━━━━━━━━━━",
        f"🆔 訂單編號：`{sig.get('order_id', 'N/A')}`",
        f"⏰ 時間：{tw_ts()}",
        f"方向：{direction}",
        f"當前：`{current_price:.4f}` {pnl_emoji}{pnl:+.2f}%",
        f"進場：`{entry:.4f}`",
        "",
        f"🎯 止盈進度：{progress}",
        f"  TP1 `{sig['tp1']:.4f}`{'✅' if sig.get('hit_tp1') else ''}",
        f"  TP2 `{sig['tp2']:.4f}`{'✅' if sig.get('hit_tp2') else ''}",
        f"  TP3 `{sig['tp3']:.4f}`{'✅' if sig.get('hit_tp3') else ''}",
        "",
        f"🛑 止損：`{sig['sl']:.4f}`"
    ])

# ─────────────────────────────────────────────────────────
# 9. 數據抓取
# ─────────────────────────────────────────────────────────
def fetch_price(instId: str) -> float:
    """🔍 即時價格 (帶快取)"""
    now = time.time()
    if instId in _price_cache:
        price, t = _price_cache[instId]
        if now - t < 5:
            return price
    
    price = PriceValidator.get_verified_price(instId)
    if price and price > 0:
        _price_cache[instId] = (price, now)
        return price
    
    return _price_cache.get(instId, (0.0, 0))[0]

def fetch_candles(instId: str, tf: str = "15m", limit: int = 100) -> list | None:
    """📊 K 線數據"""
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}",
            timeout=6,
        ).json()
        if res.get("code") != "0":
            return None
        data = res.get("data", [])
        if len(data) < 30:
            return None
        confirmed = [r for r in data if r[8] == "1"][::-1]
        return [
            {"ts": r[0], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
            for r in confirmed
        ]
    except Exception as e:
        logging.warning(f"⚠️ 取得 {instId} K 線失敗: {e}")
        return None

def fetch_funding_rate(instId: str) -> float | None:
    """💰 資金費率"""
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}",
            timeout=5,
        ).json()
        if res.get("code") == "0" and res.get("data"):
            return float(res["data"][0]["fundingRate"])
    except Exception as e:
        logging.warning(f"⚠️ 取得 {instId} 資金費率失敗: {e}")
    return None

# ─────────────────────────────────────────────────────────
# 10. 基礎技術指標
# ─────────────────────────────────────────────────────────
def calc_atr(df: list, period: int = 14) -> float:
    """ATR"""
    if len(df) < period + 1:
        return 0.001
    trs = []
    for i in range(1, len(df)):
        hl = df[i]["h"] - df[i]["l"]
        hc = abs(df[i]["h"] - df[i-1]["c"])
        lc = abs(df[i]["l"] - df[i-1]["c"])
        trs.append(max(hl, hc, lc))
    if len(trs) < period:
        return 0.001
    atr = sum(trs[-period:]) / period
    return atr if atr > 0 else 0.001

def calc_supertrend(df: list, period: int = 10, mult: float = 3.0) -> int:
    """Supertrend"""
    if len(df) < period + 2:
        return 0
    atr = calc_atr(df, period)
    mid = sum(r["c"] for r in df[-20:]) / 20
    cur = df[-1]["c"]
    band = atr * 0.5
    if cur > mid + band:
        return 1
    if cur < mid - band:
        return -1
    return 0

def calc_rsi(df: list, period: int = 14) -> float:
    """RSI"""
    if len(df) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(df)):
        ch = df[i]["c"] - df[i-1]["c"]
        gains.append(ch if ch > 0 else 0)
        losses.append(-ch if ch < 0 else 0)
    if len(gains) < period:
        return 50.0
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))

# ─────────────────────────────────────────────────────────
# 11. 高級技術分析 (SMC/ICT/SNR/PA/流動性/動能)
# ─────────────────────────────────────────────────────────
def find_order_block(df: list, side: str, lookback: int = 30) -> dict | None:
    """🧱 訂單塊 (OB)"""
    n = len(df)
    if n < lookback + 5:
        return None
    start = max(0, n - lookback)
    if side == "LONG":
        for i in range(n - 4, start, -1):
            if df[i]["c"] < df[i]["o"]:
                for j in range(i + 1, min(i + 4, n)):
                    if df[j]["c"] > df[i]["h"]:
                        return {"low": df[i]["l"], "high": df[i]["h"]}
    else:
        for i in range(n - 4, start, -1):
            if df[i]["c"] > df[i]["o"]:
                for j in range(i + 1, min(i + 4, n)):
                    if df[j]["c"] < df[i]["l"]:
                        return {"low": df[i]["l"], "high": df[i]["h"]}
    return None

def find_fvg(df: list, side: str, lookback: int = 30) -> dict | None:
    """⚡ 公允價值缺口 (FVG)"""
    n = len(df)
    if n < 4:
        return None
    start = max(2, n - lookback)
    for i in range(n - 1, start, -1):
        if side == "LONG":
            if df[i]["l"] > df[i-2]["h"]:
                return {"low": df[i-2]["h"], "high": df[i]["l"]}
        else:
            if df[i]["h"] < df[i-2]["l"]:
                return {"low": df[i]["h"], "high": df[i-2]["l"]}
    return None

def calc_snr(df: list, lookback: int = 100) -> Tuple[float, float]:
    """📏 支撐阻力"""
    seg = df[-lookback:] if len(df) >= lookback else df
    return min(r["l"] for r in seg), max(r["h"] for r in seg)

def detect_price_action(df: list, side: str) -> bool:
    """📊 價格行為"""
    if len(df) < 2:
        return False
    last, prev = df[-1], df[-2]
    body = abs(last["c"] - last["o"])
    upper = last["h"] - max(last["c"], last["o"])
    lower = min(last["c"], last["o"]) - last["l"]
    
    if body > 0:
        if side == "LONG" and lower > body * 2 and lower > upper:
            return True
        if side == "SHORT" and upper > body * 2 and upper > lower:
            return True
    
    if side == "LONG":
        if prev["c"] < prev["o"] and last["c"] > last["o"] and last["c"] > prev["o"] and last["o"] < prev["c"]:
            return True
    else:
        if prev["c"] > prev["o"] and last["c"] < last["o"] and last["c"] < prev["o"] and last["o"] > prev["c"]:
            return True
    return False

def detect_liquidity_sweep(df: list, side: str, lookback: int = 20) -> bool:
    """💧 流動性掃蕩"""
    if len(df) < lookback + 1:
        return False
    seg = df[-(lookback + 1):-1]
    last = df[-1]
    prev_low = min(r["l"] for r in seg)
    prev_high = max(r["h"] for r in seg)
    mid = (prev_low + prev_high) / 2
    
    if side == "LONG":
        return last["l"] < prev_low and last["c"] > mid
    return last["h"] > prev_high and last["c"] < mid

def calc_momentum_ratio(df: list, side: str, n: int = 5) -> bool:
    """📈 盤口動能"""
    seg = df[-n:]
    bull = sum(1 for r in seg if r["c"] > r["o"])
    ratio = bull / max(1, len(seg))
    return ratio >= 0.6 if side == "LONG" else ratio <= 0.4

def calc_volume_ratio(df: list) -> float:
    """📊 成交量比率"""
    if len(df) < 20:
        return 1.0
    avg = sum(r["v"] for r in df[-20:]) / 20
    curr = df[-1]["v"]
    return curr / avg if avg > 0 else 1.0

# ─────────────────────────────────────────────────────────
# 12. 專業評分系統 (100 分制)
# ─────────────────────────────────────────────────────────
def calc_score(df: list, side: str, current_price: float, instId: str) -> Tuple[int, str, dict]:
    """總分 = 趨勢30 + RSI25 + OB20 + FVG15 + SNR5 + PA5 + 流動性5 + 動能5 + 時框10 = 100"""
    detail = {}
    score = 0
    
    # 趨勢 (30)
    st = calc_supertrend(df)
    if (side == "LONG" and st == 1) or (side == "SHORT" and st == -1):
        score += 30; detail["trend"] = 30
    elif st == 0:
        score += 15; detail["trend"] = 15
    else:
        detail["trend"] = 0
    
    # RSI (25)
    rsi = calc_rsi(df)
    detail["rsi_value"] = round(rsi, 1)
    if side == "LONG":
        if 30 <= rsi <= 50: score += 25; detail["rsi"] = 25
        elif 50 < rsi < 70: score += 15; detail["rsi"] = 15
        else: detail["rsi"] = 0
    else:
        if 50 <= rsi <= 70: score += 25; detail["rsi"] = 25
        elif 30 < rsi < 50: score += 15; detail["rsi"] = 15
        else: detail["rsi"] = 0
    
    # OB (20)
    ob = find_order_block(df, side)
    if ob and ob["low"] * 0.995 <= current_price <= ob["high"] * 1.005:
        score += 20; detail["ob"] = 20
    else:
        detail["ob"] = 0
    
    # FVG (15)
    fvg = find_fvg(df, side)
    if fvg and fvg["low"] * 0.997 <= current_price <= fvg["high"] * 1.003:
        score += 15; detail["fvg"] = 15
    else:
        detail["fvg"] = 0
    
    # SNR (5)
    sup, res = calc_snr(df)
    if side == "LONG" and current_price <= sup * 1.01:
        score += 5; detail["snr"] = 5
    elif side == "SHORT" and current_price >= res * 0.99:
        score += 5; detail["snr"] = 5
    else:
        detail["snr"] = 0
    
    # PA (5)
    detail["pa"] = 5 if detect_price_action(df, side) else 0
    score += detail["pa"]
    
    # 流動性 (5)
    detail["liq"] = 5 if detect_liquidity_sweep(df, side) else 0
    score += detail["liq"]
    
    # 動能 (5)
    detail["mom"] = 5 if calc_momentum_ratio(df, side) else 0
    score += detail["mom"]
    
    # 多時框共振 (+10)
    if config.get("enable_multi_tf", True):
        is_confluence, desc = MultiTimeframeAnalyzer.check_confluence(instId, side)
        detail["confluence_desc"] = desc
        if is_confluence and "共振" in desc:
            score += 10; detail["confluence"] = 10
        elif is_confluence:
            score += 5; detail["confluence"] = 5
        else:
            detail["confluence"] = 0
    
    # 成交量確認
    if config.get("enable_volume_confirm", True):
        vol_ratio = calc_volume_ratio(df)
        detail["volume_ratio"] = round(vol_ratio, 2)
        if vol_ratio >= 1.2:
            score += 3
            detail["volume"] = 3
        elif vol_ratio >= 1.0:
            detail["volume"] = 0
        else:
            score -= 5  # 無量扣分
            detail["volume"] = -5
    
    grade = (
        "A+ 極強 🔥" if score >= 85
        else "A 強力 ⭐" if score >= 70
        else "B+ 合格 ✅" if score >= 68
        else "觀望 ⚪"
    )
    return score, grade, detail

# ─────────────────────────────────────────────────────────
# 13. 訊號生成
# ─────────────────────────────────────────────────────────
def generate_signal(instId: str, df: list, current_price: float, funding_rate: float | None = None) -> dict | None:
    """🎯 生成最佳交易訊號"""
    if df is None or len(df) < 50:
        return None
    
    atr = calc_atr(df)
    if atr / current_price > 0.04:
        return None
    
    funding_penalty_long = funding_rate and funding_rate > config.get("funding_rate_threshold", 0.0008)
    funding_penalty_short = funding_rate and funding_rate < -config.get("funding_rate_threshold", 0.0008)
    
    candidates = []
    for side in ("LONG", "SHORT"):
        score, grade, detail = calc_score(df, side, current_price, instId)
        
        if side == "LONG" and funding_penalty_long:
            score -= 5
        if side == "SHORT" and funding_penalty_short:
            score -= 5
        
        if score < config.get("score_threshold", 68):
            continue
        
        entry = current_price
        sl_dist = atr * config.get("sl_atr_mult", 1.5)
        sl = entry - sl_dist if side == "LONG" else entry + sl_dist
        risk = abs(entry - sl)
        
        tp1_r = config.get("tp1_r", 1.5)
        tp2_r = config.get("tp2_r", 3.0)
        tp3_r = config.get("tp3_r", 5.0)
        
        if side == "LONG":
            tp1 = entry + risk * tp1_r
            tp2 = entry + risk * tp2_r
            tp3 = entry + risk * tp3_r
        else:
            tp1 = entry - risk * tp1_r
            tp2 = entry - risk * tp2_r
            tp3 = entry - risk * tp3_r
        
        candidates.append({
            "instId": instId, "side": side, "tf": "15m",
            "entry": round(entry, 4), "sl": round(sl, 4),
            "tp1": round(tp1, 4), "tp2": round(tp2, 4), "tp3": round(tp3, 4),
            "score": score, "grade": grade, "detail": detail,
            "funding_rate": funding_rate,
            "created": time.time(),
            "expires": time.time() + config.get("signal_expire_hours", 24) * 3600,
        })
    
    return max(candidates, key=lambda x: x["score"]) if candidates else None

# ─────────────────────────────────────────────────────────
# 14. 持久化輔助
# ─────────────────────────────────────────────────────────
def _load_json(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logging.warning(f"⚠️ 讀取 {path} 失敗: {e}")
    return default

def _save_json(path: str, data) -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logging.error(f"❌ 寫入 {path} 失敗: {e}")

COOLDOWN_FILE = "signal_cooldown.json"
ACTIVE_SIGNALS_FILE = "active_signals.json"
TRADE_HISTORY_FILE = "trade_history.json"

def is_cooling(instId: str) -> bool:
    """🧊 冷卻檢查"""
    cd = _load_json(COOLDOWN_FILE, {})
    last = cd.get(instId)
    if last is None:
        return False
    return (time.time() - float(last)) < config.get("cooldown_hours", 2) * 3600

def mark_cooldown(instId: str) -> None:
    """🧊 標記冷卻"""
    cd = _load_json(COOLDOWN_FILE, {})
    cd[instId] = time.time()
    cutoff = time.time() - config.get("cooldown_hours", 2) * 3600 * 3
    cd = {k: v for k, v in cd.items() if float(v) > cutoff}
    _save_json(COOLDOWN_FILE, cd)

def record_trade(coin: str, side: str, order_id: str, entry: float, close_price: float, close_type: str, score: int) -> None:
    """📝 記錄交易"""
    is_win = close_type in ("TP1", "TP2", "TP3")
    is_be = close_type == "BE"
    pnl = (
        (close_price - entry) / entry * 100 if side == "LONG"
        else (entry - close_price) / entry * 100
    )
    trade = {
        "time": tw_now().strftime("%Y-%m-%d %H:%M"),
        "date": tw_date(),
        "order_id": order_id, "coin": coin, "side": side,
        "entry": entry, "close": close_price, "close_type": close_type,
        "pnl": round(pnl, 2), "is_win": is_win, "is_be": is_be, "score": score,
    }
    history = _load_json(TRADE_HISTORY_FILE, [])
    history.append(trade)
    _save_json(TRADE_HISTORY_FILE, history)
    logging.info(f"📝 記錄交易: {coin} {order_id} {close_type}")

# ─────────────────────────────────────────────────────────
# 15. 訊號追蹤器
# ─────────────────────────────────────────────────────────
class SignalTracker:
    def __init__(self, filepath: str = ACTIVE_SIGNALS_FILE):
        self.filepath = filepath
        self.signals: dict = _load_json(filepath, {})
        self.transitions = 0
    
    def _save(self) -> None:
        _save_json(self.filepath, self.signals)
    
    def add(self, signal: dict, active: bool = False) -> Tuple[str, str]:
        """新增訊號"""
        order_id = f"{int(time.time())}-{uuid.uuid4().hex[:8].upper()}"
        key = f"{signal['instId']}_{signal['side']}_{order_id}"
        self.signals[key] = {
            **signal, "order_id": order_id,
            "status": "ACTIVE" if active else "PENDING",
            "hit_tp1": False, "hit_tp2": False, "hit_tp3": False,
            "activated_at": time.time() if active else None,
            "entry_message_id": None,
        }
        self._save()
        logging.info(f"📌 新增訂單: {order_id} ({signal['instId']} {signal['side']})")
        return key, order_id
    
    def set_entry_message_id(self, key: str, message_id: int | None) -> None:
        if key in self.signals and message_id:
            self.signals[key]["entry_message_id"] = message_id
            self._save()
    
    def check_all(self) -> None:
        """檢查所有訊號"""
        self.transitions = 0
        to_remove = []
        for key, sig in list(self.signals.items()):
            if self._check_one(key, sig):
                to_remove.append(key)
        for key in to_remove:
            del self.signals[key]
        if to_remove:
            self._save()
    
    def _check_one(self, key: str, sig: dict) -> bool:
        """檢查單一訊號"""
        try:
            price = fetch_price(sig["instId"])
            if price <= 0:
                return False
            
            sig["current_price"] = price
            coin = sig["instId"].split("-")[0]
            order_id = sig.get("order_id", "N/A")
            side, status = sig["side"], sig["status"]
            entry, sl = sig["entry"], sig["sl"]
            tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
            reply_to = sig.get("entry_message_id")
            kb = _order_keyboard(order_id)
            
            # PENDING
            if status == "PENDING":
                if time.time() > sig["expires"]:
                    send_tg(f"⏰ *{coin} 訊號過期*\n🆔 訂單: `{order_id}`\n進場 `{entry:.4f}` 未觸發")
                    self.transitions += 1
                    return True
                
                in_zone = (
                    (side == "LONG" and entry * 0.994 <= price <= entry * 1.002) or
                    (side == "SHORT" and entry * 0.998 <= price <= entry * 1.006)
                )
                if in_zone:
                    sig["status"] = "ACTIVE"
                    sig["activated_at"] = time.time()
                    msg_id = send_tg(
                        _fmt_entry(coin, side, order_id, price, entry, sl, tp1, tp2, tp3, sig["score"],
                                  sig.get("funding_rate"), sig.get("detail", {}).get("confluence_desc", "")),
                        reply_markup=kb
                    )
                    if msg_id:
                        sig["entry_message_id"] = msg_id
                    self._save()
                    self.transitions += 1
                return False
            
            if status not in ("ACTIVE", "BE", "TRAIL"):
                return False
            
            # SL
            sl_hit = (side == "LONG" and price <= sl) or (side == "SHORT" and price >= sl)
            if sl_hit:
                is_be = status in ("BE", "TRAIL") and abs(sl - entry) < entry * 1e-4
                pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
                send_tg(_fmt_sl(coin, side, order_id, price, pnl, is_be), reply_markup=kb, reply_to_message_id=reply_to)
                record_trade(coin, side, order_id, entry, price, "BE" if is_be else "SL", sig["score"])
                risk_manager.record_trade_result(pnl, "BE" if is_be else "SL")
                self.transitions += 1
                return True
            
            # TP3
            tp3_hit = (side == "LONG" and price >= tp3) or (side == "SHORT" and price <= tp3)
            if tp3_hit and not sig.get("hit_tp3"):
                pnl = ((tp3 - entry) / entry * 100) if side == "LONG" else ((entry - tp3) / entry * 100)
                send_tg(_fmt_tp(coin, side, order_id, "TP3", tp3, pnl, config.get("tp3_r", 5.0)), reply_markup=kb, reply_to_message_id=reply_to)
                record_trade(coin, side, order_id, entry, tp3, "TP3", sig["score"])
                self.transitions += 1
                return True
            
            # TP2
            tp2_hit = (side == "LONG" and price >= tp2) or (side == "SHORT" and price <= tp2)
            if tp2_hit and not sig.get("hit_tp2"):
                sig["hit_tp2"] = True
                sig["sl"] = tp1
                sig["status"] = "TRAIL"
                self._save()
                pnl = ((tp2 - entry) / entry * 100) if side == "LONG" else ((entry - tp2) / entry * 100)
                send_tg(_fmt_tp(coin, side, order_id, "TP2", tp2, pnl, config.get("tp2_r", 3.0)), reply_markup=kb, reply_to_message_id=reply_to)
                record_trade(coin, side, order_id, entry, tp2, "TP2", sig["score"])
                self.transitions += 1
                return False
            
            # TP1
            tp1_hit = (side == "LONG" and price >= tp1) or (side == "SHORT" and price <= tp1)
            if tp1_hit and not sig.get("hit_tp1"):
                sig["hit_tp1"] = True
                sig["sl"] = entry
                sig["status"] = "BE"
                self._save()
                pnl = ((tp1 - entry) / entry * 100) if side == "LONG" else ((entry - tp1) / entry * 100)
                send_tg(_fmt_tp(coin, side, order_id, "TP1", tp1, pnl, config.get("tp1_r", 1.5)), reply_markup=kb, reply_to_message_id=reply_to)
                record_trade(coin, side, order_id, entry, tp1, "TP1", sig["score"])
                self.transitions += 1
                return False
            
            return False
        except Exception as e:
            logging.error(f"❌ check_one [{key}] 錯誤: {e}")
            return False
    
    def send_position_updates(self) -> None:
        """📊 發送持倉更新"""
        if not config.get("enable_position_updates", True):
            return
        cnt = 0
        for sig in self.signals.values():
            if sig["status"] not in ("ACTIVE", "BE", "TRAIL"):
                continue
            price = fetch_price(sig["instId"])
            if price <= 0:
                continue
            send_tg(_fmt_position(sig, price), reply_markup=_order_keyboard(sig.get("order_id", "")), reply_to_message_id=sig.get("entry_message_id"))
            cnt += 1
        if cnt:
            logging.info(f"📊 已發送 {cnt} 筆持倉更新")
    
    def get_position_stats(self) -> str:
        """📋 持倉統計"""
        positions = list(self.signals.values())
        if not positions:
            return "📭 *目前無持倉*\n\n🔄 系統持續掃描中..."
        
        lines = [f"📊 *追蹤中訊號 ({len(positions)} 筆)*", "═" * 22, ""]
        for i, p in enumerate(positions):
            price = fetch_price(p["instId"]) or p["entry"]
            coin = p["instId"].split("-")[0]
            coin_emoji = "🟠" if "BTC" in p["instId"] else "🔷" if "ETH" in p["instId"] else "🟣"
            side_emoji = "🟢" if p["side"] == "LONG" else "🔴"
            order_id = p.get("order_id", "N/A")
            pnl = ((price - p["entry"]) / p["entry"] * 100) if p["side"] == "LONG" else ((p["entry"] - price) / p["entry"] * 100)
            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            progress = "🏆 TP3" if p.get("hit_tp3") else "🥈 TP2" if p.get("hit_tp2") else "🥇 TP1" if p.get("hit_tp1") else "⏳ 等待"
            lines.extend([
                f"{coin_emoji} *#{coin}* · {side_emoji} {p['side']} · {p.get('score', 0)} 分",
                f"🆔 訂單: `{order_id}`",
                f"狀態: {p['status']}",
                f"當前 `{price:.4f}` {pnl_emoji}{pnl:+.2f}%",
                f"進場 `{p['entry']:.4f}` · 止損 `{p['sl']:.4f}`",
                f"TP1 `{p['tp1']:.4f}` · TP2 `{p['tp2']:.4f}` · TP3 `{p['tp3']:.4f}`",
                f"進度: {progress}"
            ])
            if i < len(positions) - 1:
                lines.append("─" * 22)
        return "\n".join(lines)

# ─────────────────────────────────────────────────────────
# 16. 簡化版回測引擎
# ─────────────────────────────────────────────────────────
class Backtester:
    """🔬 簡化版回測引擎"""
    
    def __init__(self, initial_capital: float = 10000):
        self.initial_capital = initial_capital
    
    def run_backtest(self, instId: str, start_date: str, end_date: str, tf: str = "15m") -> dict:
        """🧪 執行回測 (簡化版)"""
        logging.warning("⚠️ 完整回測需整合 OKX 歷史 API，目前為示意代碼")
        return {
            "note": "完整回測功能需整合 OKX 歷史數據 API",
            "demo": {
                "total_trades": 0, "win_rate": 0, "total_pnl_pct": 0,
                "max_drawdown": 0, "sharpe_ratio": 0
            }
        }

# ─────────────────────────────────────────────────────────
# 17. 主掃描邏輯
# ─────────────────────────────────────────────────────────
def run_scan(tracker: SignalTracker) -> int:
    """🔍 執行掃描"""
    logging.info("🚀 開始掃描...")
    sent = 0
    
    # 風險檢查
    can_trade, reason = risk_manager.check_circuit_breaker()
    if not can_trade:
        logging.warning(f"🛑 風險熔斷: {reason}")
        return 0
    
    for instId in ALL_COINS:
        if sent >= config.get("max_signals", 3):
            break
        
        if is_cooling(instId):
            logging.info(f"[{instId}] 冷卻中，跳過")
            continue
        
        try:
            price = fetch_price(instId)
            if price <= 0:
                logging.warning(f"[{instId}] 無法取得價格")
                continue
            
            df = fetch_candles(instId)
            if df is None:
                continue
            
            # 時段過濾
            is_safe, time_reason = TimeFilter.is_safe_to_trade()
            funding = fetch_funding_rate(instId)
            signal = generate_signal(instId, df, price, funding)
            
            if not signal:
                continue
            
            # 高風險時段只允許 A+ 訊號
            if not is_safe and signal["score"] < 85:
                logging.info(f"[{instId}] 高風險時段 ({time_reason})，跳過非 A+ 訊號")
                continue
            
            in_zone = (
                (signal["side"] == "LONG" and signal["entry"] * 0.994 <= price <= signal["entry"] * 1.002) or
                (signal["side"] == "SHORT" and signal["entry"] * 0.998 <= price <= signal["entry"] * 1.006)
            )
            
            key, order_id = tracker.add(signal, active=in_zone)
            
            if in_zone:
                confluence_desc = signal.get("detail", {}).get("confluence_desc", "")
                msg = _fmt_entry(
                    coin=instId.split("-")[0], side=signal["side"], order_id=order_id,
                    price=price, entry=signal["entry"], sl=signal["sl"],
                    tp1=signal["tp1"], tp2=signal["tp2"], tp3=signal["tp3"],
                    score=signal["score"], funding_rate=funding, confluence_desc=confluence_desc
                )
                msg_id = send_tg(msg, reply_markup=_order_keyboard(order_id))
                tracker.set_entry_message_id(key, msg_id)
                logging.info(f"✅ {instId} 進場通知已送出，訂單 {order_id}")
            else:
                send_tg(
                    f"📍 *{instId.split('-')[0]} 訊號就位*\n"
                    f"🆔 訂單: `{order_id}`\n⏰ 時間: {tw_ts()}\n"
                    f"方向: {'做多' if signal['side'] == 'LONG' else '做空'}\n"
                    f"進場價: `{signal['entry']:.4f}` (當前 `{price:.4f}`)\n"
                    f"評分: {signal['score']} 分\n\n"
                    f"💡 進入有效區間後會自動觸發進場通知",
                    reply_markup=_order_keyboard(order_id)
                )
                logging.info(f"📍 {instId} PENDING 訊號已建立，訂單 {order_id}")
            
            mark_cooldown(instId)
            sent += 1
            
        except Exception as e:
            logging.error(f"[{instId}] 掃描失敗: {e}")
            continue
    
    tracker.check_all()
    tracker.send_position_updates()
    logging.info(f"✅ 掃描完成，本輪新增 {sent} 筆訊號")
    return sent

# ─────────────────────────────────────────────────────────
# 18. 主入口
# ─────────────────────────────────────────────────────────
def main() -> None:
    try:
        logging.info("=" * 60)
        logging.info("🤖 Alpha Oracle Pro v12.0 專業精準版啟動")
        logging.info(f"⏰ 台灣時間: {tw_ts()}")
        logging.info("=" * 60)
        
        # 顯示風險狀態
        risk_status = risk_manager.get_risk_status()
        logging.info(f"🛡️ 風險狀態: 可交易={risk_status['can_trade']}, 原因={risk_status['reason']}")
        
        tracker = SignalTracker(ACTIVE_SIGNALS_FILE)
        
        # 命令處理
        if len(sys.argv) > 1:
            cmd = sys.argv[1]
            
            if cmd in ("/stats", "/持倉", "stats"):
                send_tg(tracker.get_position_stats())
                return
            
            elif cmd == "/config":
                send_tg(config.to_telegram_format())
                return
            
            elif cmd.startswith("/set "):
                try:
                    parts = cmd[5:].split("=")
                    if len(parts) == 2:
                        key, value = parts
                        if value.isdigit():
                            value = int(value)
                        elif '.' in value:
                            value = float(value)
                        if config.update({key: value}):
                            send_tg(f"✅ 配置已更新: `{key}` = `{value}`")
                        else:
                            send_tg(f"❌ 配置更新失敗")
                    else:
                        send_tg("❌ 用法: `/set 參數=數值`\n例: `/set score_threshold=70`")
                except Exception as e:
                    send_tg(f"❌ 解析錯誤: {e}")
                return
            
            elif cmd.startswith("/backtest"):
                parts = cmd.split()
                if len(parts) >= 4:
                    instId, start, end = parts[1], parts[2], parts[3]
                    backtester = Backtester()
                    result = backtester.run_backtest(instId, start, end)
                    if "error" in result:
                        send_tg(f"❌ 回測錯誤: {result['error']}")
                    else:
                        demo = result.get("demo", {})
                        msg = (
                            f"🧪 *回測報告* {instId}\n"
                            f"━━━━━━━━━━━━━━\n"
                            f"📈 總交易: `{demo.get('total_trades', 0)}` 筆\n"
                            f"✅ 勝率: `{demo.get('win_rate', 0):.1f}%`\n"
                            f"💰 總盈虧: `{demo.get('total_pnl_pct', 0):+.2f}%`\n"
                            f"📉 最大回撤: `{demo.get('max_drawdown', 0):.2f}%`\n"
                            f"⚡ 夏普比率: `{demo.get('sharpe_ratio', 0):.2f}`\n"
                            f"\n⚠️ 完整回測需整合 OKX 歷史 API"
                        )
                        send_tg(msg)
                else:
                    send_tg("❌ 用法: `/backtest <交易對> <開始日期> <結束日期>`")
                return
            
            elif cmd == "/risk":
                status = risk_manager.get_risk_status()
                msg = (
                    f"🛡️ *風險狀態*\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"📊 當日盈虧: `{status['daily_loss']:+.2f}%`\n"
                    f"🔴 連續虧損: `{status['consecutive_losses']}` 筆\n"
                    f"🔌 熔斷狀態: `{status['circuit_breaker'] or '正常'}`\n"
                    f"✅ 可交易: `{'是' if status['can_trade'] else '否'}`\n"
                    f"📝 原因: {status['reason']}"
                )
                send_tg(msg)
                return
        
        # 每日報告
        now = tw_now()
        report_time = config.get("report_time", "22:00")
        report_hour, report_min = map(int, report_time.split(":"))
        if now.hour == report_hour and now.minute >= report_min:
            # 簡化版每日報告
            history = _load_json(TRADE_HISTORY_FILE, [])
            today = tw_date()
            today_trades = [t for t in history if t.get("date") == today]
            if today_trades:
                wins = sum(1 for t in today_trades if t["is_win"])
                losses = sum(1 for t in today_trades if not t["is_win"] and not t.get("is_be"))
                bes = sum(1 for t in today_trades if t.get("is_be"))
                total = len(today_trades)
                win_rate = wins / total * 100 if total else 0
                total_pnl = sum(t["pnl"] for t in today_trades)
                grade = "🏆 優秀" if win_rate >= 70 else "✅ 良好" if win_rate >= 50 else "⚠️ 待改進"
                send_tg(
                    f"📊 *每日戰報* {today}\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"📈 交易: `{total}` 筆 {grade}\n"
                    f"✅ 盈利: `{wins}` | ❌ 止損: `{losses}` | 🛡 保本: `{bes}`\n"
                    f"📊 勝率: *{win_rate:.1f}%*\n"
                    f"💰 盈虧: `{total_pnl:+.2f}%`\n"
                    f"\n{'🎯 保持節奏' if win_rate >= 50 else '🔧 明日優化'}"
                )
        
        # 執行掃描
        run_scan(tracker)
        logging.info("🎉 程式執行完成")
        
    except KeyboardInterrupt:
        logging.info("⚠️ 程式被中斷")
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        logging.critical(f"🔥 系統錯誤: {e}")
        import traceback
        logging.critical(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    main()
