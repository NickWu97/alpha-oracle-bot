
# config.py
import json
import os

class Config:
    def __init__(self, config_path="config.json"):
        self.config_path = config_path
        self.data = self._load()
    
    def _load(self):
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}
    
    def get(self, key, default=None):
        keys = key.split('.')
        d = self.data
        for k in keys:
            if isinstance(d, dict):
                d = d.get(k)
            else:
                return default
        return d if d is not None else default

config = Config()
