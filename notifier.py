# notifier.py
import os
import logging
import aiohttp
from config import config

TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

LEVELS = {"all": 0, "important": 1, "critical": 2}
CURRENT_LEVEL = LEVELS.get(config.get("notification_level", "all"), 0)

async def send_tg_async(msg: str, level: str = "all", parse_mode: str = "Markdown", reply_markup=None) -> bool:
    if LEVELS.get(level, 0) < CURRENT_LEVEL:
        return False
    if not TG_TOKEN or not CHAT_ID:
        logging.warning("TG_TOKEN or CHAT_ID not set")
        return False
    payload = {
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json=payload, timeout=8) as resp:
                return resp.status == 200
    except Exception as e:
        logging.error(f"TG send failed: {e}")
        return False

def send_tg_sync(msg: str, level: str = "all", parse_mode: str = "Markdown"):
    """同步版本（用於非同步環境的輔助）"""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(send_tg_async(msg, level, parse_mode))
