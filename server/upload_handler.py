import base64
import logging
import os

from telethon import TelegramClient
from telethon.tl.types import InputDocumentFileLocation

logger = logging.getLogger(__name__)


async def get_telethon_client(bot_token):
    api_id = int(os.getenv("API_ID"))
    api_hash = os.getenv("API_HASH")
    client = TelegramClient('bot_session', api_id, api_hash)
    await client.start(bot_token=bot_token)
    return client


async def download_telegram_document_reference(
    bot_token,
    document_id,
    access_hash,
    file_reference_b64,
    dc_id,
    file_size,
    destination,
):
    """Download a Telegram document directly on the build runner.

    The panel passes only Telegram's document reference metadata to GitHub,
    so the panel itself never needs to download or persist the project ZIP.
    """
    if not document_id or not access_hash or not file_reference_b64:
        raise RuntimeError("Rujukan fail Telegram tidak lengkap")

    try:
        file_reference = base64.b64decode(file_reference_b64, validate=True)
    except Exception as error:
        raise RuntimeError("Telegram file_reference tidak sah") from error

    location = InputDocumentFileLocation(
        id=int(document_id),
        access_hash=int(access_hash),
        file_reference=file_reference,
        thumb_size="",
    )

    client = await get_telethon_client(bot_token)
    try:
        kwargs = {}
        if file_size:
            kwargs["file_size"] = int(file_size)
        if dc_id:
            kwargs["dc_id"] = int(dc_id)
        await client.download_file(location, file=destination, **kwargs)
    finally:
        await client.disconnect()

    if not os.path.exists(destination) or os.path.getsize(destination) <= 0:
        raise RuntimeError("Fail Telegram kosong atau gagal dimuat turun")
    return destination


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
    except Exception:
        pass
    return None


async def send_telegram_notification(bot_token, chat_id, message):
    try:
        async with await get_telethon_client(bot_token) as client:
            await client.send_message(int(chat_id), message, parse_mode='html')
            return True
    except Exception:
        return False


async def send_telegram_document(bot_token, chat_id, file_path, caption=None):
    try:
        async with await get_telethon_client(bot_token) as client:
            await client.send_file(int(chat_id), file_path, caption=caption, parse_mode='html', force_document=True)
            return True
    except Exception:
        return False
