# health_check.py
import threading
import time
import logging
import requests

def check_apis():
    """檢查 OKX 和 Binance API 連線"""
    try:
        r = requests.get("https://www.okx.com/api/v5/public/time", timeout=5)
        if r.status_code != 200:
            logging.warning("OKX API 異常")
            return False
        r = requests.get("https://api.binance.com/api/v3/time", timeout=5)
        if r.status_code != 200:
            logging.warning("Binance API 異常")
            return False
        return True
    except:
        return False

def health_loop():
    while True:
        if not check_apis():
            logging.error("API 連線異常，嘗試重啟（此處僅記錄，實際重啟需外部管理）")
            # 可在此觸發重新初始化 fetcher
        time.sleep(600)  # 每10分鐘檢查

def start_health_check():
    t = threading.Thread(target=health_loop, daemon=True)
    t.start()
