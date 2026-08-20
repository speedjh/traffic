# -*- coding: utf-8 -*-
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "campaign_queue.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.environ.get("CAMPAIGN_DB_URL", f"sqlite:///{DB_PATH}")
LEASE_TIMEOUT_SECS = int(os.environ.get("CAMPAIGN_LEASE_TIMEOUT", "900"))
HEARTBEAT_STALE_SECS = int(os.environ.get("CAMPAIGN_HEARTBEAT_STALE", "90"))

# Spread jobs across local day windows (hour)
WINDOW_START_HOUR = int(os.environ.get("CAMPAIGN_WINDOW_START", "9"))
WINDOW_END_HOUR = int(os.environ.get("CAMPAIGN_WINDOW_END", "22"))
