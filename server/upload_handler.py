import os
import logging
from telethon import TelegramClient

logger = logging.getLogger(__name__)

async def get_telethon_client(bot_token):
    api_id = int(os.getenv("API_ID"))
    api_hash = os.getenv("API_HASH")
    client = TelegramClient('bot_session', api_id, api_hash)
    await client.start(bot_token=bot_token)
    return client

async def upload_gofile(file_path):
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.gofile.io/servers") as r:
                res = await r.json()
                server = res["data"]["servers"][0]["name"]
            url = f"https://{server}.gofile.io/contents/uploadfile"
            with open(file_path, "rb") as f:
                async with session.post(url, data={"file": f}) as r:
                    res = await r.json()
                    if res["status"] == "ok":
                        return res["data"]["downloadPage"]
    except: pass
    return None

async def send_telegram_notification(bot_token, chat_id, message):
    try:
        async with await get_telethon_client(bot_token) as client:
            await client.send_message(int(chat_id), message, parse_mode='html')
            return True
    except: return False

async def send_telegram_document(bot_token, chat_id, file_path, caption=None):
    try:
        async with await get_telethon_client(bot_token) as client:
            await client.send_file(int(chat_id), file_path, caption=caption, parse_mode='html', force_document=True)
            return True
    except: return False
