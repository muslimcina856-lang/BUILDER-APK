import asyncio
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "server" / "upload_handler.py"


class FakeLocation:
    def __init__(self, id, access_hash, file_reference, thumb_size):
        self.id = id
        self.access_hash = access_hash
        self.file_reference = file_reference
        self.thumb_size = thumb_size


class FakeClient:
    def __init__(self):
        self.calls = []
        self.disconnected = False

    async def download_file(self, location, file, **kwargs):
        self.calls.append((location, file, kwargs))
        Path(file).write_bytes(b"PK-test")

    async def disconnect(self):
        self.disconnected = True


class UploadHandlerDirectTests(unittest.IsolatedAsyncioTestCase):
    def load_module(self):
        old_modules = {name: sys.modules.get(name) for name in ("telethon", "telethon.tl", "telethon.tl.types")}
        telethon = types.ModuleType("telethon")
        telethon.TelegramClient = object
        tl = types.ModuleType("telethon.tl")
        tl_types = types.ModuleType("telethon.tl.types")
        tl_types.InputDocumentFileLocation = FakeLocation
        sys.modules["telethon"] = telethon
        sys.modules["telethon.tl"] = tl
        sys.modules["telethon.tl.types"] = tl_types
        try:
            spec = importlib.util.spec_from_file_location("upload_handler_direct_under_test", MODULE_PATH)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        finally:
            for name, previous in old_modules.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous

    async def test_download_uses_document_reference_without_panel_file(self):
        module = self.load_module()
        fake = FakeClient()

        async def get_client(_token):
            return fake

        module.get_telethon_client = get_client
        with tempfile.TemporaryDirectory() as tmp:
            destination = os.path.join(tmp, "project.zip")
            result = await module.download_telegram_document_reference(
                bot_token="token",
                document_id="11",
                access_hash="22",
                file_reference_b64="YWJj",
                dc_id="4",
                file_size="1234",
                destination=destination,
            )
            self.assertEqual(result, destination)
            self.assertEqual(Path(destination).read_bytes(), b"PK-test")

        self.assertTrue(fake.disconnected)
        self.assertEqual(len(fake.calls), 1)
        location, _, kwargs = fake.calls[0]
        self.assertEqual(location.id, 11)
        self.assertEqual(location.access_hash, 22)
        self.assertEqual(location.file_reference, b"abc")
        self.assertEqual(kwargs["dc_id"], 4)
        self.assertEqual(kwargs["file_size"], 1234)

    async def test_invalid_reference_fails_before_network(self):
        module = self.load_module()
        with self.assertRaises(RuntimeError):
            await module.download_telegram_document_reference(
                bot_token="token",
                document_id="11",
                access_hash="22",
                file_reference_b64="not@@base64",
                dc_id="4",
                file_size="1234",
                destination="unused.zip",
            )


if __name__ == "__main__":
    unittest.main()
