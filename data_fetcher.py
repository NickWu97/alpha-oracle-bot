# data_fetcher.py
import time
import logging
import aiohttp
from typing import Dict, List, Optional, Tuple

class AsyncDataFetcher:
    def __init__(self):
        self.session = None
        self.price_cache: Dict[str, Tuple[float, float]] = {}
        self.candle_cache: Dict[str, Tuple[List[Dict], float]] = {}
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, *args):
        await self.session.close()
    
    async def fetch_price(self, instId: str, source: str = "okx") -> Optional[float]:
        now = time.time()
        key = f"{source}_{instId}"
        if key in self.price_cache and now - self.price_cache[key][1] < 5:
            return self.price_cache[key][0]
        
        urls = {
            "okx": f"https://www.okx.com/api/v5/market/ticker?instId={instId}",
            "binance": f"https://api.binance.com/api/v3/ticker/price?symbol={instId.replace('-USDT-SWAP','USDT')}",
        }
        if source not in urls:
            return None
        try:
            async with self.session.get(urls[source], timeout=5) as resp:
                data = await resp.json()
                if source == "okx" and data.get("code") == "0":
                    price = float(data["data"][0]["last"])
                elif source == "binance" and "price" in data:
                    price = float(data["price"])
                else:
                    return None
                if price > 0:
                    self.price_cache[key] = (price, now)
                    return price
        except Exception as e:
            logging.warning(f"fetch {source} {instId} error: {e}")
        return None
    
    async def fetch_candles(self, instId: str, tf: str = "15m", limit: int = 300, cache_seconds: int = 240) -> Optional[List[Dict]]:
        cache_key = f"{instId}_{tf}"
        now = time.time()
        if cache_key in self.candle_cache and now - self.candle_cache[cache_key][1] < cache_seconds:
            return self.candle_cache[cache_key][0]
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}"
        try:
            async with self.session.get(url, timeout=8) as resp:
                data = await resp.json()
                if data.get("code") != "0":
                    return None
                candles = []
                for r in data["data"]:
                    if r[8] != "1":  # 只取已收線
                        continue
                    candles.append({
                        "ts": r[0], "o": float(r[1]), "h": float(r[2]),
                        "l": float(r[3]), "c": float(r[4]), "v": float(r[5])
                    })
                candles = candles[::-1]  # 由舊到新
                if len(candles) >= 30:
                    self.candle_cache[cache_key] = (candles, now)
                    return candles
        except Exception as e:
            logging.warning(f"fetch candles {instId} error: {e}")
        return None
    
    async def fetch_mtf_trend(self, instId: str) -> Dict:
        """簡化版，只回傳 1H / 4H 的 supertrend 方向"""
        result = {}
        for tf in ("1H", "4H"):
            candles = await self.fetch_candles(instId, tf=tf, limit=100)
            if candles:
                from indicators import calc_supertrend
                st = calc_supertrend(candles)
                result[tf] = {"supertrend": st, "trend": "up" if st==1 else "down" if st==-1 else "side"}
            else:
                result[tf] = {"supertrend": 0, "trend": "side"}
        return result

    async def fetch_funding_rate(self, instId: str) -> Optional[float]:
        url = f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}"
        try:
            async with self.session.get(url, timeout=5) as resp:
                data = await resp.json()
                if data.get("code") == "0" and data.get("data"):
                    return float(data["data"][0]["fundingRate"])
        except Exception as e:
            logging.warning(f"fetch funding {instId} error: {e}")
        return None
