import importlib
import os
import shlex
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

# Elakkan dependency Telethon/network semasa unit test helper worker.
upload_stub = types.ModuleType("upload_handler")
async def _noop(*args, **kwargs):
    return None
upload_stub.upload_gofile = _noop
upload_stub.send_telegram_notification = _noop
upload_stub.send_telegram_document = _noop
upload_stub.download_telegram_document_reference = _noop
sys.modules.setdefault("upload_handler", upload_stub)

worker = importlib.import_module("worker")


class GodotWorkerHelperTests(unittest.TestCase):
    def make_project(self, project_text='config_version=5\n', preset_text=None, files=None):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        (root / "project.godot").write_text(project_text, encoding="utf-8")
        if preset_text is not None:
            (root / "export_presets.cfg").write_text(preset_text, encoding="utf-8")
        for rel, content in (files or {}).items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return temp, root

    def test_godot_32_uses_java_8(self):
        self.assertEqual(worker._godot_android_profile("3.2.3")["java"], "8")
        self.assertEqual(worker._godot_android_profile("3.5.3")["java"], "11")
        self.assertEqual(worker._godot_android_profile("3.6.2")["java"], "17")

    def test_godot_362_uses_api35_and_ndk28_toolchain(self):
        profile = worker._godot_android_profile("3.6.2")
        self.assertEqual(profile["compile_sdk"], "35")
        self.assertEqual(profile["build_tools"], "35.0.1")
        self.assertEqual(profile["ndk"], "28.1.13356709")
        legacy = worker._godot_android_profile("3.6.1")
        self.assertEqual(legacy["compile_sdk"], "34")
        self.assertEqual(legacy["ndk"], "23.2.8568313")

    def test_detect_dotnet_from_feature_or_csproj(self):
        temp, root = self.make_project(
            'config_version=5\nconfig/features=PackedStringArray("4.7", "C#", "GL Compatibility")\n'
        )
        with temp:
            self.assertEqual(worker._detect_godot_project_kind(str(root)), "dotnet")

        temp, root = self.make_project(files={"Game.csproj": '<Project Sdk="Godot.NET.Sdk/4.7.2" />'})
        with temp:
            self.assertEqual(worker._detect_godot_project_kind(str(root)), "dotnet")

        temp, root = self.make_project()
        with temp:
            self.assertEqual(worker._detect_godot_project_kind(str(root)), "standard")

    def test_dotnet_sdk_version_policy(self):
        self.assertEqual(worker._godot_dotnet_sdk_major("3.6.2"), 8)
        self.assertEqual(worker._godot_dotnet_sdk_major("4.4.4"), 8)
        self.assertEqual(worker._godot_dotnet_sdk_major("4.5.2"), 9)
        self.assertEqual(worker._godot_dotnet_sdk_major("4.7.2"), 9)

    def test_package_suffix_never_starts_with_digit(self):
        self.assertEqual(worker._safe_android_package_suffix("123 Game"), "app123game")
        self.assertEqual(worker._safe_android_package_suffix("My Cool Game"), "mycoolgame")
        self.assertEqual(worker._safe_android_package_suffix("---"), "project")

    def test_list_android_presets_preserves_multiple_presets(self):
        preset = '''[preset.0]\n\nname="Phone Debug"\nplatform="Android"\nrunnable=true\n\n[preset.0.options]\n\ngradle_build/use_gradle_build=false\ngradle_build/export_format=0\n\n[preset.1]\n\nname="Play Store"\nplatform="Android"\nrunnable=false\n\n[preset.1.options]\n\ngradle_build/use_gradle_build=true\ngradle_build/export_format=1\nkeystore/release="res://release.keystore"\nkeystore/release_user="game"\nkeystore/release_password="secret"\n'''
        temp, root = self.make_project(preset_text=preset)
        with temp:
            presets = worker._list_android_presets(str(root))
            self.assertEqual([p["name"] for p in presets], ["Phone Debug", "Play Store"])
            self.assertEqual(presets[0]["format"], "apk")
            self.assertFalse(presets[0]["gradle"])
            self.assertEqual(presets[1]["format"], "aab")
            self.assertTrue(presets[1]["gradle"])
            self.assertTrue(presets[1]["has_release_signing"])

    def test_select_preset_by_name_or_runnable(self):
        presets = [
            {"name": "Phone", "runnable": True},
            {"name": "Store", "runnable": False},
        ]
        self.assertEqual(worker._select_android_presets(presets, requested="Store", build_all=False)[0]["name"], "Store")
        self.assertEqual(worker._select_android_presets(presets, requested=None, build_all=False)[0]["name"], "Phone")
        self.assertEqual(len(worker._select_android_presets(presets, requested=None, build_all=True)), 2)
        with self.assertRaises(ValueError):
            worker._select_android_presets(presets, requested="Missing", build_all=False)

    def test_godot_cli_flags_are_version_compatible(self):
        self.assertEqual(worker._godot_export_flag(3, "debug"), "--export-debug")
        self.assertEqual(worker._godot_export_flag(3, "release"), "--export")
        self.assertEqual(worker._godot_export_flag(4, "debug"), "--export-debug")
        self.assertEqual(worker._godot_export_flag(4, "release"), "--export-release")
        server = "/tmp/Godot_v3.6.2-stable_server_x11.64"
        self.assertEqual(worker._godot_cli_prefix(server, 3), server)
        self.assertIn("--headless", worker._godot_cli_prefix("/tmp/godot4", 4))

    def test_export_mode_auto_adds_release_only_when_signing_available(self):
        self.assertEqual(worker._export_variants("auto", has_release_signing=False), ["debug"])
        self.assertEqual(worker._export_variants("auto", has_release_signing=True), ["debug", "release"])
        self.assertEqual(worker._export_variants("debug", has_release_signing=True), ["debug"])
        self.assertEqual(worker._export_variants("release", has_release_signing=True), ["release"])
        self.assertEqual(worker._export_variants("both", has_release_signing=False), ["debug", "release"])

    def test_release_resolver_chooses_matching_mono_assets(self):
        release = {
            "draft": False,
            "prerelease": False,
            "tag_name": "4.7.2-stable",
            "assets": [
                {"name": "Godot_v4.7.2-stable_linux.x86_64.zip", "browser_download_url": "std-engine"},
                {"name": "Godot_v4.7.2-stable_export_templates.tpz", "browser_download_url": "std-tpl"},
                {"name": "Godot_v4.7.2-stable_mono_linux_x86_64.zip", "browser_download_url": "mono-engine"},
                {"name": "Godot_v4.7.2-stable_mono_export_templates.tpz", "browser_download_url": "mono-tpl"},
            ],
        }
        with mock.patch.object(worker, "_fetch_json", side_effect=[[release]]):
            resolved = worker._resolve_release_sync("4.7", dotnet=True)
        self.assertEqual(resolved["engine_url"], "mono-engine")
        self.assertEqual(resolved["templates_url"], "mono-tpl")
        self.assertTrue(resolved["dotnet"])

    def test_release_resolver_standard_never_picks_mono_assets(self):
        release = {
            "draft": False,
            "prerelease": False,
            "tag_name": "4.7.2-stable",
            "assets": [
                {"name": "Godot_v4.7.2-stable_linux.x86_64.zip", "browser_download_url": "std-engine"},
                {"name": "Godot_v4.7.2-stable_export_templates.tpz", "browser_download_url": "std-tpl"},
                {"name": "Godot_v4.7.2-stable_mono_linux_x86_64.zip", "browser_download_url": "mono-engine"},
                {"name": "Godot_v4.7.2-stable_mono_export_templates.tpz", "browser_download_url": "mono-tpl"},
            ],
        }
        with mock.patch.object(worker, "_fetch_json", side_effect=[[release]]):
            resolved = worker._resolve_release_sync("4.7", dotnet=False)
        self.assertEqual(resolved["engine_url"], "std-engine")
        self.assertEqual(resolved["templates_url"], "std-tpl")
        self.assertFalse(resolved["dotnet"])

    def test_find_engine_binary_ignores_godotsharp_managed_files(self):
        temp = tempfile.TemporaryDirectory()
        with temp:
            root = Path(temp.name)
            (root / "GodotSharp.dll").write_text("managed", encoding="utf-8")
            engine = root / "Godot_v4.7.2-stable_mono_linux.x86_64"
            engine.write_text("binary", encoding="utf-8")
            self.assertEqual(worker._find_engine_binary(str(root)), str(engine))

    def test_gradle_template_detection(self):
        temp, root = self.make_project()
        with temp:
            self.assertFalse(worker._android_build_template_present(str(root)))
            build = root / "android" / "build"
            build.mkdir(parents=True)
            (build / "build.gradle").write_text("plugins {}", encoding="utf-8")
            self.assertTrue(worker._android_build_template_present(str(root)))

    def test_native_extension_scan_reports_missing_android_library(self):
        gdext = '''[configuration]\nentry_symbol="example_library_init"\n\n[libraries]\nlinux.debug.x86_64="res://bin/libexample.so"\n'''
        temp, root = self.make_project(files={"addons/example/example.gdextension": gdext})
        with temp:
            warnings = worker._validate_android_native_extensions(str(root))
            self.assertEqual(len(warnings), 1)
            self.assertIn("Android", warnings[0])

    def test_prepare_release_signing_accepts_base64_keystore_secret(self):
        import base64

        temp, root = self.make_project()
        encoded = base64.b64encode(b"fake-keystore-binary").decode("ascii")
        with temp, mock.patch.dict(os.environ, {
            "GODOT_RELEASE_KEYSTORE_BASE64": encoded,
            "GODOT_RELEASE_KEYSTORE_USER": "game",
            "GODOT_RELEASE_KEYSTORE_PASSWORD": "secret",
        }, clear=False):
            for key in (
                "GODOT_RELEASE_KEYSTORE_PATH",
                "GODOT_ANDROID_KEYSTORE_RELEASE_PATH",
                "GODOT_ANDROID_KEYSTORE_RELEASE_USER",
                "GODOT_ANDROID_KEYSTORE_RELEASE_PASSWORD",
            ):
                os.environ.pop(key, None)
            logs = []
            self.assertTrue(worker._prepare_release_signing_environment(str(root), logs))
            key_path = Path(os.environ["GODOT_ANDROID_KEYSTORE_RELEASE_PATH"])
            self.assertTrue(key_path.exists())
            self.assertEqual(key_path.read_bytes(), b"fake-keystore-binary")
            self.assertNotIn(encoded, "\n".join(logs))
            self.assertNotIn("secret", "\n".join(logs))

    def test_godot3_gradle_template_falls_back_to_android_source_zip(self):
        temp, root = self.make_project()
        data_home = tempfile.TemporaryDirectory()
        with temp, data_home:
            template_dir = Path(data_home.name) / "godot" / "templates" / "3.6.2.stable"
            template_dir.mkdir(parents=True)
            source_zip = template_dir / "android_source.zip"
            import zipfile
            with zipfile.ZipFile(source_zip, "w") as archive:
                archive.writestr("build.gradle", "plugins {}")
                archive.writestr("settings.gradle", "rootProject.name='godot'\n")
            logs = []
            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": data_home.name}, clear=False):
                import asyncio
                asyncio.run(worker._ensure_android_build_template(
                    "/fake/godot", str(root), "3.6.2", 3, False, logs
                ))
            self.assertTrue((root / "android" / "build" / "build.gradle").exists())
            self.assertTrue((root / "android" / ".build_version").exists())

    def test_prepare_release_signing_resolves_project_relative_secret_path(self):
        temp, root = self.make_project(files={"signing/release.jks": "key"})
        with temp, mock.patch.dict(os.environ, {
            "GODOT_RELEASE_KEYSTORE_PATH": "signing/release.jks",
            "GODOT_RELEASE_KEYSTORE_USER": "game",
            "GODOT_RELEASE_KEYSTORE_PASSWORD": "secret",
        }, clear=False):
            for key in (
                "GODOT_ANDROID_KEYSTORE_RELEASE_PATH",
                "GODOT_ANDROID_KEYSTORE_RELEASE_USER",
                "GODOT_ANDROID_KEYSTORE_RELEASE_PASSWORD",
            ):
                os.environ.pop(key, None)
            logs = []
            self.assertTrue(worker._prepare_release_signing_environment(str(root), logs))
            self.assertEqual(
                os.environ["GODOT_ANDROID_KEYSTORE_RELEASE_PATH"],
                str(root / "signing" / "release.jks"),
            )
            self.assertEqual(os.environ["GODOT_ANDROID_KEYSTORE_RELEASE_USER"], "game")
            self.assertNotIn("secret", "\n".join(logs))


class GodotBuildFlowTests(unittest.IsolatedAsyncioTestCase):
    def make_project(self, project_text, preset_text, files=None):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        (root / "project.godot").write_text(project_text, encoding="utf-8")
        (root / "export_presets.cfg").write_text(preset_text, encoding="utf-8")
        for rel, content in (files or {}).items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return temp, root

    async def _successful_export_cmd(self, command, cwd=None, timeout=1200):
        args = shlex.split(command)
        if any(flag in args for flag in ("--export-debug", "--export-release", "--export")):
            output = Path(args[-1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"artifact")
        return 0, "", ""

    async def test_configure_android_paths_restores_project_file_exactly(self):
        project = (
            'config_version=5\n\n'
            '[editor_plugins]\n'
            'enabled=PackedStringArray("res://addons/original/plugin.cfg")\n'
        )
        preset = '[preset.0]\nname="Android"\nplatform="Android"\n'
        temp, root = self.make_project(project, preset)
        original = (root / "project.godot").read_bytes()

        async def fake_editor(command, cwd=None, timeout=1200):
            sentinel = os.environ.get("EARLXZ_GODOT_SETUP_SENTINEL")
            if sentinel:
                Path(sentinel).write_text("ok", encoding="utf-8")
            return 0, "", ""

        with temp, mock.patch.object(worker, "run_cmd", new=mock.AsyncMock(side_effect=fake_editor)):
            await worker._configure_android_paths(
                "/fake/godot", str(root), [], 4, debug_keystore="/tmp/debug.keystore"
            )
            self.assertEqual((root / "project.godot").read_bytes(), original)
            addons = root / "addons"
            if addons.exists():
                self.assertFalse(any(
                    path.name.startswith("earlxz_builder_android_setup_")
                    for path in addons.glob("*")
                ))

    async def test_fallback_android_preset_is_removed_after_build(self):
        linux_only = '[preset.0]\nname="Linux"\nplatform="Linux/X11"\nrunnable=true\n'
        temp, root = self.make_project(
            'config_version=5\nconfig/features=PackedStringArray("4.7")\n',
            linux_only,
        )
        original = (root / "export_presets.cfg").read_text(encoding="utf-8")
        with temp, \
             mock.patch.object(worker, "_setup_godot", new=mock.AsyncMock(return_value=("/fake/godot", "4.7.2", 4))), \
             mock.patch.object(worker, "_setup_godot_android_requirements", new=mock.AsyncMock(return_value={})), \
             mock.patch.object(worker, "_ensure_godot_debug_keystore", new=mock.AsyncMock(return_value=str(root / "debug.keystore"))), \
             mock.patch.object(worker, "_configure_android_paths", new=mock.AsyncMock()), \
             mock.patch.object(worker, "run_cmd", new=mock.AsyncMock(side_effect=self._successful_export_cmd)):
            result = await worker.build_godot(str(root), {"godot_export_mode": "debug"})
            self.assertTrue(result["success"])
            self.assertEqual((root / "export_presets.cfg").read_text(encoding="utf-8"), original)

    async def test_fallback_preset_restores_original_bytes_exactly(self):
        linux_only = b'\xef\xbb\xbf[preset.0]\r\nname="Linux"\r\nplatform="Linux/X11"\r\nrunnable=true\r\n'
        temp, root = self.make_project(
            'config_version=5\nconfig/features=PackedStringArray("4.7")\n',
            '[preset.0]\nname="Linux"\nplatform="Linux/X11"\n',
        )
        (root / "export_presets.cfg").write_bytes(linux_only)
        with temp, \
             mock.patch.object(worker, "_setup_godot", new=mock.AsyncMock(return_value=("/fake/godot", "4.7.2", 4))), \
             mock.patch.object(worker, "_setup_godot_android_requirements", new=mock.AsyncMock(return_value={})), \
             mock.patch.object(worker, "_ensure_godot_debug_keystore", new=mock.AsyncMock(return_value=str(root / "debug.keystore"))), \
             mock.patch.object(worker, "_configure_android_paths", new=mock.AsyncMock()), \
             mock.patch.object(worker, "run_cmd", new=mock.AsyncMock(side_effect=self._successful_export_cmd)):
            result = await worker.build_godot(str(root), {"godot_export_mode": "debug"})
            restored = (root / "export_presets.cfg").read_bytes()
        self.assertTrue(result["success"], result)
        self.assertEqual(restored, linux_only)

    async def test_standard_auto_mode_exports_debug_only_without_release_signing(self):
        preset = '''[preset.0]\nname="Android"\nplatform="Android"\nrunnable=true\n[preset.0.options]\ngradle_build/use_gradle_build=false\ngradle_build/export_format=0\n'''
        temp, root = self.make_project('config_version=5\nconfig/features=PackedStringArray("4.7")\n', preset)
        with temp, \
             mock.patch.object(worker, "_setup_godot", new=mock.AsyncMock(return_value=("/fake/godot", "4.7.2", 4))), \
             mock.patch.object(worker, "_setup_godot_android_requirements", new=mock.AsyncMock(return_value={})), \
             mock.patch.object(worker, "_ensure_godot_debug_keystore", new=mock.AsyncMock(return_value=str(root / "debug.keystore"))), \
             mock.patch.object(worker, "_configure_android_paths", new=mock.AsyncMock()), \
             mock.patch.object(worker, "run_cmd", new=mock.AsyncMock(side_effect=self._successful_export_cmd)) as run:
            result = await worker.build_godot(str(root), {"godot_export_mode": "auto"})
        self.assertTrue(result["success"])
        self.assertEqual([Path(path).name for path in result["files"]], ["app-debug.apk"])
        commands = "\n".join(call.args[0] for call in run.await_args_list)
        self.assertIn("--export-debug", commands)
        self.assertNotIn("--export-release", commands)

    async def test_auto_mode_exports_debug_and_release_when_signing_is_configured(self):
        preset = '''[preset.0]\nname="Android"\nplatform="Android"\nrunnable=true\n[preset.0.options]\ngradle_build/use_gradle_build=false\ngradle_build/export_format=0\nkeystore/release="res://release.keystore"\nkeystore/release_user="game"\nkeystore/release_password="secret"\n'''
        temp, root = self.make_project('config_version=5\nconfig/features=PackedStringArray("4.7")\n', preset)
        with temp, \
             mock.patch.object(worker, "_setup_godot", new=mock.AsyncMock(return_value=("/fake/godot", "4.7.2", 4))), \
             mock.patch.object(worker, "_setup_godot_android_requirements", new=mock.AsyncMock(return_value={})), \
             mock.patch.object(worker, "_ensure_godot_debug_keystore", new=mock.AsyncMock(return_value=str(root / "debug.keystore"))), \
             mock.patch.object(worker, "_configure_android_paths", new=mock.AsyncMock()), \
             mock.patch.object(worker, "run_cmd", new=mock.AsyncMock(side_effect=self._successful_export_cmd)) as run:
            result = await worker.build_godot(str(root), {"godot_export_mode": "auto"})
        self.assertTrue(result["success"])
        self.assertEqual(
            [Path(path).name for path in result["files"]],
            ["app-debug.apk", "app-release.apk"],
        )
        commands = "\n".join(call.args[0] for call in run.await_args_list)
        self.assertIn("--export-debug", commands)
        self.assertIn("--export-release", commands)

    async def test_dotnet_project_uses_dotnet_setup_and_mono_engine(self):
        project = 'config_version=5\nconfig/features=PackedStringArray("4.7", "C#")\n'
        preset = '''[preset.0]\nname="Android"\nplatform="Android"\nrunnable=true\n[preset.0.options]\ngradle_build/use_gradle_build=false\ngradle_build/export_format=0\n'''
        temp, root = self.make_project(project, preset, {"Game.csproj": '<Project Sdk="Godot.NET.Sdk/4.7.2" />'})
        setup_godot = mock.AsyncMock(return_value=("/fake/godot-mono", "4.7.2", 4))
        setup_dotnet = mock.AsyncMock(return_value=9)
        with temp, \
             mock.patch.object(worker, "_setup_godot", new=setup_godot), \
             mock.patch.object(worker, "_setup_dotnet_sdk", new=setup_dotnet), \
             mock.patch.object(worker, "_setup_godot_android_requirements", new=mock.AsyncMock(return_value={})), \
             mock.patch.object(worker, "_ensure_godot_debug_keystore", new=mock.AsyncMock(return_value=str(root / "debug.keystore"))), \
             mock.patch.object(worker, "_configure_android_paths", new=mock.AsyncMock()), \
             mock.patch.object(worker, "run_cmd", new=mock.AsyncMock(side_effect=self._successful_export_cmd)):
            result = await worker.build_godot(str(root), {"godot_export_mode": "debug"})
        self.assertTrue(result["success"])
        setup_godot.assert_awaited_once_with(str(root), mock.ANY, dotnet=True)
        setup_dotnet.assert_awaited_once_with("4.7.2", mock.ANY)

    async def test_aab_preset_requests_gradle_template(self):
        preset = '''[preset.0]\nname="Play Store"\nplatform="Android"\nrunnable=true\n[preset.0.options]\ngradle_build/use_gradle_build=true\ngradle_build/export_format=1\n'''
        temp, root = self.make_project('config_version=5\nconfig/features=PackedStringArray("4.7")\n', preset)
        ensure_gradle = mock.AsyncMock()
        with temp, \
             mock.patch.object(worker, "_setup_godot", new=mock.AsyncMock(return_value=("/fake/godot", "4.7.2", 4))), \
             mock.patch.object(worker, "_setup_godot_android_requirements", new=mock.AsyncMock(return_value={})), \
             mock.patch.object(worker, "_ensure_godot_debug_keystore", new=mock.AsyncMock(return_value=str(root / "debug.keystore"))), \
             mock.patch.object(worker, "_configure_android_paths", new=mock.AsyncMock()), \
             mock.patch.object(worker, "_ensure_android_build_template", new=ensure_gradle), \
             mock.patch.object(worker, "run_cmd", new=mock.AsyncMock(side_effect=self._successful_export_cmd)):
            result = await worker.build_godot(str(root), {"godot_export_mode": "debug"})
        self.assertTrue(result["success"])
        self.assertEqual(Path(result["files"][0]).suffix, ".aab")
        ensure_gradle.assert_awaited_once()

    async def test_godot3_release_uses_legacy_export_switch(self):
        preset = '[preset.0]\nname="Android"\nplatform="Android"\nrunnable=true\n[preset.0.options]\nkeystore/release="res://release.keystore"\nkeystore/release_user="game"\nkeystore/release_password="secret"\n'
        temp, root = self.make_project(
            'config_version=4\nconfig/features=PoolStringArray("3.6")\n', preset
        )
        with temp, \
             mock.patch.object(worker, "_setup_godot", new=mock.AsyncMock(return_value=("/fake/Godot_server_x11.64", "3.6.2", 3))), \
             mock.patch.object(worker, "_setup_godot_android_requirements", new=mock.AsyncMock(return_value={})), \
             mock.patch.object(worker, "_ensure_godot_debug_keystore", new=mock.AsyncMock(return_value=str(root / "debug.keystore"))), \
             mock.patch.object(worker, "_configure_android_paths", new=mock.AsyncMock()), \
             mock.patch.object(worker, "run_cmd", new=mock.AsyncMock(side_effect=self._successful_export_cmd)) as run:
            result = await worker.build_godot(str(root), {"godot_export_mode": "release"})
        self.assertTrue(result["success"])
        commands = "\n".join(call.args[0] for call in run.await_args_list)
        self.assertIn(" --export ", commands)
        self.assertNotIn("--export-release", commands)
        self.assertNotIn("--no-window", commands)

    async def test_godot3_secure_signing_is_injected_temporarily_and_restored(self):
        preset = '[preset.0]\nname="Android"\nplatform="Android"\nrunnable=true\n[preset.0.options]\ncustom_build/use_custom_build=false\n'
        temp, root = self.make_project(
            'config_version=4\nconfig/features=PoolStringArray("3.6")\n', preset,
            files={"signing/release.jks": "key"},
        )
        original = (root / "export_presets.cfg").read_bytes()
        seen = {}

        async def signed_export(command, cwd=None, timeout=1200):
            current = (root / "export_presets.cfg").read_text(encoding="utf-8")
            seen["preset"] = current
            if (
                'keystore/release="' not in current
                or 'keystore/release_user="game"' not in current
                or 'keystore/release_password="secret"' not in current
            ):
                return 1, "", "Godot 3 release signing was not bridged into preset"
            return await self._successful_export_cmd(command, cwd=cwd, timeout=timeout)

        env = {
            "GODOT_RELEASE_KEYSTORE_PATH": "signing/release.jks",
            "GODOT_RELEASE_KEYSTORE_USER": "game",
            "GODOT_RELEASE_KEYSTORE_PASSWORD": "secret",
        }
        with temp, mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(worker, "_setup_godot", new=mock.AsyncMock(return_value=("/fake/Godot_server_x11.64", "3.6.2", 3))), \
             mock.patch.object(worker, "_setup_godot_android_requirements", new=mock.AsyncMock(return_value={})), \
             mock.patch.object(worker, "_ensure_godot_debug_keystore", new=mock.AsyncMock(return_value=str(root / "debug.keystore"))), \
             mock.patch.object(worker, "_configure_android_paths", new=mock.AsyncMock()), \
             mock.patch.object(worker, "run_cmd", new=mock.AsyncMock(side_effect=signed_export)):
            for key in (
                "GODOT_ANDROID_KEYSTORE_RELEASE_PATH",
                "GODOT_ANDROID_KEYSTORE_RELEASE_USER",
                "GODOT_ANDROID_KEYSTORE_RELEASE_PASSWORD",
            ):
                os.environ.pop(key, None)
            result = await worker.build_godot(str(root), {"godot_export_mode": "release"})
            restored = (root / "export_presets.cfg").read_bytes()

        self.assertTrue(result["success"], result)
        self.assertIn('keystore/release_user="game"', seen["preset"])
        self.assertEqual(restored, original)

    async def test_base64_release_keystore_is_removed_after_build(self):
        import base64

        preset = '[preset.0]\nname="Android"\nplatform="Android"\nrunnable=true\n[preset.0.options]\ngradle_build/use_gradle_build=false\n'
        temp, root = self.make_project(
            'config_version=5\nconfig/features=PackedStringArray("4.7")\n', preset
        )
        seen = {}

        async def capture_export(command, cwd=None, timeout=1200):
            seen["path"] = os.environ.get("GODOT_ANDROID_KEYSTORE_RELEASE_PATH")
            return await self._successful_export_cmd(command, cwd=cwd, timeout=timeout)

        env = {
            "GODOT_RELEASE_KEYSTORE_BASE64": base64.b64encode(b"secret-key").decode("ascii"),
            "GODOT_RELEASE_KEYSTORE_USER": "game",
            "GODOT_RELEASE_KEYSTORE_PASSWORD": "secret",
        }
        with temp, mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(worker, "_setup_godot", new=mock.AsyncMock(return_value=("/fake/godot", "4.7.2", 4))), \
             mock.patch.object(worker, "_setup_godot_android_requirements", new=mock.AsyncMock(return_value={})), \
             mock.patch.object(worker, "_ensure_godot_debug_keystore", new=mock.AsyncMock(return_value=str(root / "debug.keystore"))), \
             mock.patch.object(worker, "_configure_android_paths", new=mock.AsyncMock()), \
             mock.patch.object(worker, "run_cmd", new=mock.AsyncMock(side_effect=capture_export)):
            for key in (
                "GODOT_RELEASE_KEYSTORE_PATH",
                "GODOT_ANDROID_KEYSTORE_RELEASE_PATH",
                "GODOT_ANDROID_KEYSTORE_RELEASE_USER",
                "GODOT_ANDROID_KEYSTORE_RELEASE_PASSWORD",
            ):
                os.environ.pop(key, None)
            result = await worker.build_godot(str(root), {"godot_export_mode": "release"})

        self.assertTrue(result["success"], result)
        self.assertTrue(seen.get("path"))
        self.assertFalse(Path(seen["path"]).exists())

    async def test_release_only_without_signing_fails_before_export(self):
        preset = '''[preset.0]\nname="Android"\nplatform="Android"\nrunnable=true\n[preset.0.options]\ngradle_build/use_gradle_build=false\ngradle_build/export_format=0\n'''
        temp, root = self.make_project('config_version=5\nconfig/features=PackedStringArray("4.7")\n', preset)
        run = mock.AsyncMock(side_effect=self._successful_export_cmd)
        with temp, \
             mock.patch.object(worker, "_setup_godot", new=mock.AsyncMock(return_value=("/fake/godot", "4.7.2", 4))), \
             mock.patch.object(worker, "_setup_godot_android_requirements", new=mock.AsyncMock(return_value={})), \
             mock.patch.object(worker, "_ensure_godot_debug_keystore", new=mock.AsyncMock(return_value=str(root / "debug.keystore"))), \
             mock.patch.object(worker, "_configure_android_paths", new=mock.AsyncMock()), \
             mock.patch.object(worker, "run_cmd", new=run):
            result = await worker.build_godot(str(root), {"godot_export_mode": "release"})
        self.assertFalse(result["success"])
        self.assertIn("release keystore", result["error"].lower())
        run.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
