import os
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
TEMP_DIR = "/tmp/builder"
os.makedirs(TEMP_DIR, exist_ok=True)
