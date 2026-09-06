import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

upload_stub = types.ModuleType("upload_handler")
async def _noop(*args, **kwargs):
    return None
upload_stub.upload_gofile = _noop
upload_stub.send_telegram_notification = _noop
upload_stub.send_telegram_document = _noop
upload_stub.download_telegram_document_reference = _noop
sys.modules["upload_handler"] = upload_stub

worker = importlib.import_module("worker")


class DirectHandoffTests(unittest.TestCase):
    def test_project_detected_message_contains_useful_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_dir = Path(tmp)
            project = build_dir / "nested" / "my_game"
            project.mkdir(parents=True)
            (project / "project.godot").write_text(
                'config_version=5\nconfig/features=PackedStringArray("4.7", "C#")\nconfig/name="My Game"\n',
                encoding="utf-8",
            )
            message = worker.make_project_detection_message(
                str(project), str(build_dir), "godot", "game.zip", java_version="17"
            )
            self.assertIn("PROJECT DETECTED", message)
            self.assertIn("Godot", message)
            self.assertIn("nested/my_game", message)
            self.assertIn("My Game", message)
            self.assertIn("C# / .NET", message)
            self.assertIn("4.7", message)

    def test_unknown_project_message_is_explicit(self):
        message = worker.make_project_not_detected_message("mystery.zip")
        self.assertIn("PROJECT NOT DETECTED", message)
        self.assertIn("mystery.zip", message)
        self.assertIn("Godot", message)
        self.assertIn("Android Native", message)

    def test_workflow_accepts_direct_telegram_document_reference(self):
        workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
        for key in (
            "source_mode:",
            "telegram_document_id:",
            "telegram_access_hash:",
            "telegram_file_reference:",
            "telegram_dc_id:",
            "telegram_file_size:",
        ):
            self.assertIn(key, workflow)
        self.assertIn("SOURCE_MODE:", workflow)
        self.assertIn("TELEGRAM_DOCUMENT_ID:", workflow)


if __name__ == "__main__":
    unittest.main()

class DirectRunnerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_source_is_downloaded_then_detected_before_build(self):
        import zipfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_zip = root / "source.zip"
            project_root = root / "project_src"
            project_root.mkdir()
            (project_root / "settings.gradle").write_text("rootProject.name='DirectNative'\n", encoding="utf-8")
            with zipfile.ZipFile(source_zip, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(project_root / "settings.gradle", "DirectNative/settings.gradle")

            notices = []

            async def fake_download(**kwargs):
                Path(kwargs["destination"]).write_bytes(source_zip.read_bytes())
                return kwargs["destination"]

            async def fake_notice(bot_token, chat_id, message):
                notices.append(message)
                return True

            async def fake_build(project_dir, payload):
                self.assertEqual(payload["type"], "native")
                return {"success": False, "error": "intentional stop", "logs": []}

            async def fake_failure(*args, **kwargs):
                return None

            env = {
                "BOT_TOKEN": "test-token",
                "CHAT_ID": "123",
                "USER_DISPLAY": "tester",
                "TARGET_FILE": "direct.zip",
                "SOURCE_MODE": "telegram",
                "TELEGRAM_DOCUMENT_ID": "1",
                "TELEGRAM_ACCESS_HASH": "2",
                "TELEGRAM_FILE_REFERENCE": "YWJj",
                "TELEGRAM_DC_ID": "4",
                "TELEGRAM_FILE_SIZE": str(source_zip.stat().st_size),
            }
            old_cwd = os.getcwd()
            os.chdir(root)
            try:
                with mock.patch.dict(os.environ, env, clear=False), \
                     mock.patch.object(worker, "download_telegram_document_reference", side_effect=fake_download), \
                     mock.patch.object(worker, "send_telegram_notification", side_effect=fake_notice), \
                     mock.patch.object(worker, "build_project", side_effect=fake_build), \
                     mock.patch.object(worker, "send_failure", side_effect=fake_failure):
                    code = await worker.main()
            finally:
                os.chdir(old_cwd)

            self.assertEqual(code, 1)
            self.assertTrue(any("PROJECT DETECTED" in message for message in notices))
            detected = next(message for message in notices if "PROJECT DETECTED" in message)
            self.assertIn("Android Native", detected)
            self.assertIn("DirectNative", detected)

