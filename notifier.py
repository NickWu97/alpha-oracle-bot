# notifier.py
import os
import logging
import asyncio
import threading
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 全域變數，由 main 設定
_tracker = None

def set_tracker(tracker):
    global _tracker
    _tracker = tracker

async def send_tg_async(msg: str, level: str = "all", parse_mode: str = "Markdown"):
    if not TG_TOKEN or not CHAT_ID:
        return
    # 過濾層級實作（省略，同前）
    bot = Bot(TG_TOKEN)
    await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode=parse_mode)

def send_tg_sync(msg: str, level: str = "all", parse_mode: str = "Markdown"):
    asyncio.run(send_tg_async(msg, level, parse_mode))

# 指令處理
async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global scan_paused, pause_until
    if _tracker:
        scan_paused = True
        pause_until = time.time() + 7200  # 暫停2小時
        await update.message.reply_text("⏸ 已暫停新訊號掃描 2 小時")

async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global scan_paused
    scan_paused = False
    await update.message.reply_text("▶️ 已恢復掃描")

async def close_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _tracker:
        for key, sig in list(_tracker.signals.items()):
            # 觸發止損平倉
            _tracker._hit_sl(sig, sig.get("current_price", sig["entry"]), key)
        await update.message.reply_text("🔒 已平倉所有持倉")

async def risk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from risk_manager import risk_mgr
    msg = f"📊 風險狀態\n當前權益 {risk_mgr.current_equity:.2f}\n最大回撤 {risk_mgr.current_drawdown():.2f}%\n日內虧損 {risk_mgr.daily_loss:.2f}%"
    await update.message.reply_text(msg)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _tracker:
        active_count = sum(1 for s in _tracker.signals.values() if s["status"] in ("ACTIVE","BE","TRAIL"))
        msg = f"🤖 系統狀態\n活躍持倉 {active_count}\n掃描暫停 {'是' if scan_paused else '否'}"
        await update.message.reply_text(msg)

def start_command_listener(tracker):
    set_tracker(tracker)
    def run():
        app = Application.builder().token(TG_TOKEN).build()
        app.add_handler(CommandHandler("pause", pause_command))
        app.add_handler(CommandHandler("resume", resume_command))
        app.add_handler(CommandHandler("close_all", close_all_command))
        app.add_handler(CommandHandler("risk", risk_command))
        app.add_handler(CommandHandler("status", status_command))
        app.run_polling()
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
