import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile
from urllib.parse import unquote

import aiohttp

from builder import build_project, run_cmd, setup_android_sdk, setup_java
from upload_handler import upload_gofile, send_telegram_notification, send_telegram_document


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


GODOT_RELEASES_API = "https://api.github.com/repos/godotengine/godot-builds/releases"
_GITHUB_API_TOKEN = os.environ.pop("GITHUB_TOKEN", "").strip()


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _detect_version_hint(project_dir):
    content = _read_text(os.path.join(project_dir, "project.godot"))

    for line in content.splitlines():
        if "config/features" not in line:
            continue
        versions = re.findall(r'["\x27](\d+\.\d+(?:\.\d+)?)["\x27]', line)
        if versions:
            return versions[0]

    config_match = re.search(r"^config_version\s*=\s*(\d+)", content, re.MULTILINE)
    if config_match:
        config_version = int(config_match.group(1))
        if config_version >= 5:
            return "4"
        if config_version == 4:
            return "3"

    return "4"


def _normalize_release_tag(tag):
    normalized = tag.strip().lower()
    normalized = re.sub(r"^godot[-_v]*", "", normalized)
    normalized = re.sub(r"-stable$", "", normalized)
    return normalized


def _version_tuple(version):
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?$", version)
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def _matches_hint(version, hint):
    if not re.match(r"^\d+(?:\.\d+){0,2}$", hint):
        return False
    hint_parts = hint.split(".")
    version_parts = version.split(".")
    return version_parts[: len(hint_parts)] == hint_parts


def _fetch_json(url):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Earlxz-BUILDER-APK-Godot",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if _GITHUB_API_TOKEN:
        headers["Authorization"] = f"Bearer {_GITHUB_API_TOKEN}"

    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def _resolve_release_sync(version_hint):
    matches = []
    for page in range(1, 11):
        releases = _fetch_json(f"{GODOT_RELEASES_API}?per_page=100&page={page}")
        if not releases:
            break
        for release in releases:
            if release.get("draft") or release.get("prerelease"):
                continue
            normalized = _normalize_release_tag(str(release.get("tag_name", "")))
            version = _version_tuple(normalized)
            if version is None or not _matches_hint(normalized, version_hint):
                continue
            matches.append((version, normalized, release))
        if matches:
            break

    if not matches:
        raise RuntimeError(f"Tiada release Godot stabil ditemui untuk versi {version_hint}")

    matches.sort(key=lambda item: item[0], reverse=True)
    version_tuple, normalized, release = matches[0]
    major = version_tuple[0]

    engine_candidates = []
    template_candidates = []
    for asset in release.get("assets", []):
        name = str(asset.get("name", ""))
        lower = name.lower()
        url = str(asset.get("browser_download_url", ""))
        if not url:
            continue
        if "export_templates.tpz" in lower and "mono" not in lower:
            template_candidates.append((name, url))
            continue
        if not lower.endswith(".zip") or "mono" in lower or "linux" not in lower:
            continue
        if not any(token in lower for token in ("x86_64", "x11.64", "linux.64", "headless.64", "server.64")):
            continue
        if any(token in lower for token in ("arm64", "arm32", "web_editor")):
            continue
        engine_candidates.append((name, url))

    if not engine_candidates:
        raise RuntimeError(f"Binary Linux x86_64 Godot {normalized} tidak ditemui")
    if not template_candidates:
        raise RuntimeError(f"Export templates Godot {normalized} tidak ditemui")

    if major == 3:
        engine_candidates.sort(
            key=lambda item: (
                not any(token in item[0].lower() for token in ("headless", "server")),
                "x11.64.zip" not in item[0].lower(),
                len(item[0]),
            )
        )
    else:
        engine_candidates.sort(
            key=lambda item: (
                ".linux.x86_64.zip" not in item[0].lower(),
                "linux.64.zip" not in item[0].lower(),
                len(item[0]),
            )
        )
    template_candidates.sort(key=lambda item: len(item[0]))

    return {
        "version": normalized,
        "major": major,
        "engine_name": engine_candidates[0][0],
        "engine_url": engine_candidates[0][1],
        "templates_name": template_candidates[0][0],
        "templates_url": template_candidates[0][1],
    }


async def _resolve_release(version_hint):
    return await asyncio.to_thread(_resolve_release_sync, version_hint)


def _download_sync(url, destination):
    request = urllib.request.Request(url, headers={"User-Agent": "Earlxz-BUILDER-APK-Godot"})
    with urllib.request.urlopen(request, timeout=300) as response, open(destination, "wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)


async def _download(url, destination):
    await asyncio.to_thread(_download_sync, url, destination)


def _find_engine_binary(directory):
    candidates = []
    for root, _, files in os.walk(directory):
        for filename in files:
            lower = filename.lower()
            if not lower.startswith("godot"):
                continue
            if any(token in lower for token in (".pck", ".txt", ".md", "license")):
                continue
            path = os.path.join(root, filename)
            candidates.append(path)
    if not candidates:
        return None
    candidates.sort(key=lambda path: ("console" in os.path.basename(path).lower(), len(path)))
    return candidates[0]


def _copy_template_contents(source, destination):
    os.makedirs(destination, exist_ok=True)
    for entry in os.listdir(source):
        src = os.path.join(source, entry)
        dst = os.path.join(destination, entry)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)


def _install_templates(extracted_dir, version):
    templates_dir = None
    for root, dirs, _ in os.walk(extracted_dir):
        if os.path.basename(root) == "templates":
            templates_dir = root
            break
        if "templates" in dirs:
            templates_dir = os.path.join(root, "templates")
            break
    if not templates_dir:
        templates_dir = extracted_dir

    version_key = f"{version}.stable"
    data_home = os.path.expanduser(os.environ.get("XDG_DATA_HOME", "~/.local/share"))
    destinations = [
        os.path.join(data_home, "godot", "export_templates", version_key),
        os.path.join(data_home, "godot", "templates", version_key),
    ]
    for destination in destinations:
        if os.path.isdir(destination):
            shutil.rmtree(destination, ignore_errors=True)
        _copy_template_contents(templates_dir, destination)


async def _setup_godot(project_dir, logs):
    version_hint = _detect_version_hint(project_dir)
    logs.append(f"Godot version hint: {version_hint}")
    release = await _resolve_release(version_hint)
    logs.append(f"Godot stable selected: {release['version']}")

    install_root = os.path.join(tempfile.gettempdir(), f"godot-{release['version']}")
    engine_archive = os.path.join(tempfile.gettempdir(), release["engine_name"])
    templates_archive = os.path.join(tempfile.gettempdir(), release["templates_name"])
    template_extract = os.path.join(tempfile.gettempdir(), f"godot-templates-{release['version']}")

    shutil.rmtree(install_root, ignore_errors=True)
    shutil.rmtree(template_extract, ignore_errors=True)
    os.makedirs(install_root, exist_ok=True)
    os.makedirs(template_extract, exist_ok=True)

    await _download(release["engine_url"], engine_archive)
    await _download(release["templates_url"], templates_archive)

    with zipfile.ZipFile(engine_archive, "r") as archive:
        archive.extractall(install_root)
    with zipfile.ZipFile(templates_archive, "r") as archive:
        archive.extractall(template_extract)

    engine = _find_engine_binary(install_root)
    if not engine:
        raise RuntimeError("Binary Godot tidak ditemui selepas extraction")
    os.chmod(engine, os.stat(engine).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    _install_templates(template_extract, release["version"])
    logs.append("Godot export templates installed")
    return engine, release["version"], release["major"]


def _find_android_preset(project_dir):
    path = os.path.join(project_dir, "export_presets.cfg")
    content = _read_text(path)
    if not content:
        return None, "apk"

    sections = re.split(r"(?=^\[preset\.\d+\]\s*$)", content, flags=re.MULTILINE)
    for section in sections:
        if not re.search(r'^platform\s*=\s*["\x27]Android["\x27]\s*$', section, re.MULTILINE):
            continue
        name_match = re.search(r'^name\s*=\s*["\x27](.*?)["\x27]\s*$', section, re.MULTILINE)
        if not name_match:
            continue
        export_format = "aab" if re.search(
            r'^(?:gradle_build|custom_build)/export_format\s*=\s*1\s*$', section, re.MULTILINE
        ) else "apk"
        return name_match.group(1), export_format
    return None, "apk"


def _write_fallback_preset(project_dir, major):
    path = os.path.join(project_dir, "export_presets.cfg")
    project_name = os.path.basename(os.path.abspath(project_dir)) or "project"
    package_suffix = re.sub(r"[^a-z0-9]+", "", project_name.lower())[:40] or "project"

    if major == 3:
        content = f'''[preset.0]

name="Android"
platform="Android"
runnable=true
custom_features=""
export_filter="all_resources"
include_filter=""
exclude_filter=""
export_path=""
patch_list=PoolStringArray(  )
script_export_mode=1
script_encryption_key=""

[preset.0.options]

custom_template/debug=""
custom_template/release=""
package/unique_name="com.earlxz.builder.{package_suffix}"
package/name=""
version/code=1
version/name="1.0"
architectures/armeabi-v7a=true
architectures/arm64-v8a=true
architectures/x86=false
architectures/x86_64=false
'''
    else:
        content = f'''[preset.0]

name="Android"
platform="Android"
runnable=true
advanced_options=false
dedicated_server=false
custom_features=""
export_filter="all_resources"
include_filter=""
exclude_filter=""
export_path=""
script_export_mode=2

[preset.0.options]

custom_template/debug=""
custom_template/release=""
gradle_build/use_gradle_build=false
gradle_build/export_format=0
package/unique_name="com.earlxz.builder.{package_suffix}"
package/name=""
version/code=1
version/name="1.0"
architectures/armeabi-v7a=true
architectures/arm64-v8a=true
architectures/x86=false
architectures/x86_64=false
'''

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


async def _ensure_debug_keystore(logs):
    keystore = os.path.expanduser("~/.android/debug.keystore")
    if os.path.exists(keystore):
        command = (
            "keytool -list -keystore " + shlex.quote(keystore)
            + " -storepass android -alias androiddebugkey"
        )
        code, _, _ = await run_cmd(command, timeout=60)
        if code == 0:
            logs.append("Android debug keystore ready")
            return keystore
        os.remove(keystore)

    os.makedirs(os.path.dirname(keystore), exist_ok=True)
    command = (
        "keytool -genkeypair -v -keystore " + shlex.quote(keystore)
        + " -storepass android -alias androiddebugkey -keypass android"
        + " -keyalg RSA -keysize 2048 -validity 10000"
        + " -dname " + shlex.quote("CN=Android Debug,O=Android,C=US")
    )
    code, output, error = await run_cmd(command, timeout=120)
    if code != 0 or not os.path.exists(keystore):
        raise RuntimeError(f"Gagal menjana debug keystore: {(error or output)[-1000:]}")
    logs.append("Android debug keystore generated")
    return keystore


def _godot_android_profile(version):
    parsed = _version_tuple(version) or (4, 0, 0)
    major, minor, _ = parsed

    if major <= 3:
        if minor <= 5:
            return {
                "java": "11",
                "compile_sdk": "31",
                "build_tools": "30.0.3",
                "cmake": "3.10.2.4988404",
                "ndk": "21.4.7075529",
            }
        return {
            "java": "17",
            "compile_sdk": "34",
            "build_tools": "34.0.0",
            "cmake": "3.10.2.4988404",
            "ndk": "23.2.8568313",
        }

    if minor == 0:
        return {
            "java": "11",
            "compile_sdk": "33",
            "build_tools": "33.0.2",
            "cmake": "3.10.2.4988404",
            "ndk": "23.2.8568313",
        }
    if minor <= 2:
        return {
            "java": "17",
            "compile_sdk": "33",
            "build_tools": "33.0.2",
            "cmake": "3.10.2.4988404",
            "ndk": "23.2.8568313",
        }
    if minor <= 4:
        return {
            "java": "17",
            "compile_sdk": "34",
            "build_tools": "34.0.0",
            "cmake": "3.10.2.4988404",
            "ndk": "23.2.8568313",
        }
    return {
        "java": "17",
        "compile_sdk": "35",
        "build_tools": "35.0.0" if minor == 5 else "35.0.1",
        "cmake": "3.10.2.4988404",
        "ndk": "28.1.13356709",
    }


def _find_sdkmanager():
    android_home = os.environ.get("ANDROID_HOME", "/usr/local/lib/android/sdk")
    candidates = [
        os.path.join(android_home, "cmdline-tools", "latest", "bin", "sdkmanager"),
        os.path.join(android_home, "tools", "bin", "sdkmanager"),
    ]
    return next((candidate for candidate in candidates if os.path.exists(candidate)), None)


async def _active_java_major():
    code, output, error = await run_cmd("java -version", timeout=60)
    if code != 0:
        raise RuntimeError(f"Tidak dapat mengesahkan versi Java: {(error or output)[-500:]}")
    version_text = error or output
    match = re.search(r'version\s+["\x27](\d+)(?:\.(\d+))?', version_text)
    if not match:
        raise RuntimeError(f"Format versi Java tidak dikenali: {version_text[-500:]}")
    major = int(match.group(1))
    if major == 1 and match.group(2):
        major = int(match.group(2))
    return major


async def _setup_godot_android_requirements(version, logs):
    profile = _godot_android_profile(version)

    # Android command-line tools terbaru memerlukan Java moden. Gunakan Java 17
    # semasa memasang SDK/NDK, kemudian aktifkan semula Java yang diperlukan
    # oleh versi Godot sebelum proses eksport bermula.
    if not await setup_java("17"):
        raise RuntimeError("Java 17 setup gagal untuk Android sdkmanager")
    sdk_java = await _active_java_major()
    if sdk_java < 17:
        raise RuntimeError(
            f"Android sdkmanager memerlukan Java 17 atau lebih baharu, Java {sdk_java} aktif"
        )
    logs.append(f"Java {sdk_java} ready for Android sdkmanager")

    sdkmanager = _find_sdkmanager()
    if not sdkmanager:
        raise RuntimeError("sdkmanager Android tidak ditemui")

    android_home = os.environ.get("ANDROID_HOME", "/usr/local/lib/android/sdk")
    packages = [
        "platform-tools",
        f"build-tools;{profile['build_tools']}",
        f"platforms;android-{profile['compile_sdk']}",
        "cmdline-tools;latest",
        f"cmake;{profile['cmake']}",
        f"ndk;{profile['ndk']}",
    ]
    package_args = " ".join(shlex.quote(package) for package in packages)
    command = (
        f"yes | {shlex.quote(sdkmanager)} --sdk_root={shlex.quote(android_home)} "
        f"{package_args}"
    )
    code, output, error = await run_cmd(command, timeout=900)
    if code != 0:
        raise RuntimeError(
            "Gagal memasang komponen Android Godot: "
            + (error or output or "unknown sdkmanager error")[-2000:]
        )

    expected_paths = [
        os.path.join(android_home, "platform-tools"),
        os.path.join(android_home, "build-tools", profile["build_tools"]),
        os.path.join(android_home, "platforms", f"android-{profile['compile_sdk']}"),
        os.path.join(android_home, "cmake", profile["cmake"]),
        os.path.join(android_home, "ndk", profile["ndk"]),
    ]
    missing = [expected for expected in expected_paths if not os.path.exists(expected)]
    if missing:
        raise RuntimeError("Komponen Android Godot tidak lengkap: " + ", ".join(missing))

    os.environ["ANDROID_HOME"] = android_home
    os.environ["ANDROID_SDK_ROOT"] = android_home
    os.environ["ANDROID_NDK_ROOT"] = os.path.join(android_home, "ndk", profile["ndk"])
    os.environ["ANDROID_NDK_HOME"] = os.environ["ANDROID_NDK_ROOT"]
    logs.append(
        "Android Godot ready: "
        f"SDK {profile['compile_sdk']}, Build Tools {profile['build_tools']}, "
        f"NDK {profile['ndk']}, CMake {profile['cmake']}"
    )

    if profile["java"] != "17":
        if not await setup_java(profile["java"]):
            raise RuntimeError(f"Java {profile['java']} setup gagal untuk Godot {version}")
    active_java = await _active_java_major()
    if active_java != int(profile["java"]):
        raise RuntimeError(
            f"Godot {version} memerlukan Java {profile['java']}, tetapi Java {active_java} aktif"
        )
    logs.append(f"Java {active_java} ready for Godot {version}")
    return profile


async def _ensure_debug_signing_tools(logs):
    if not await setup_java("17"):
        raise RuntimeError("Java 17 setup gagal untuk Android debug signing")
    active_java = await _active_java_major()
    if active_java < 17:
        raise RuntimeError(
            f"Android debug signing memerlukan Java 17 atau lebih baharu, Java {active_java} aktif"
        )
    logs.append(f"Java {active_java} ready for Android debug signing")

    android_home = os.environ.get("ANDROID_HOME", "/usr/local/lib/android/sdk")
    required_dir = os.path.join(android_home, "build-tools", "35.0.1")
    if not all(os.path.exists(os.path.join(required_dir, tool)) for tool in ("zipalign", "apksigner")):
        sdkmanager = _find_sdkmanager()
        if not sdkmanager:
            raise RuntimeError("sdkmanager Android tidak ditemui untuk memasang signing tools")
        command = (
            f"yes | {shlex.quote(sdkmanager)} --sdk_root={shlex.quote(android_home)} "
            + shlex.quote("build-tools;35.0.1")
        )
        code, output, error = await run_cmd(command, timeout=600)
        if code != 0:
            raise RuntimeError(
                "Gagal memasang Android Build Tools 35.0.1: "
                + (error or output or "unknown sdkmanager error")[-1000:]
            )
    if not all(os.path.exists(os.path.join(required_dir, tool)) for tool in ("zipalign", "apksigner")):
        raise RuntimeError("zipalign atau apksigner Build Tools 35.0.1 tidak lengkap")
    logs.append("Android Build Tools 35.0.1 ready for debug signing")


def _find_android_build_tool(tool_name):
    android_home = os.environ.get("ANDROID_HOME", "/usr/local/lib/android/sdk")
    required = os.path.join(android_home, "build-tools", "35.0.1", tool_name)
    if os.path.exists(required):
        return required

    build_tools_dir = os.path.join(android_home, "build-tools")
    if not os.path.isdir(build_tools_dir):
        return shutil.which(tool_name)
    versions = sorted(
        os.listdir(build_tools_dir),
        key=lambda version: _version_tuple(version) or (0, 0, 0),
        reverse=True,
    )
    for version in versions:
        candidate = os.path.join(build_tools_dir, version, tool_name)
        if os.path.exists(candidate):
            return candidate
    return shutil.which(tool_name)


def _strip_archive_signatures(archive_path):
    temporary = archive_path + ".unsigned"
    signature_suffixes = (".SF", ".RSA", ".DSA", ".EC")
    try:
        with zipfile.ZipFile(archive_path, "r") as source:
            with zipfile.ZipFile(temporary, "w") as target:
                for item in source.infolist():
                    upper = item.filename.upper()
                    is_signature = upper == "META-INF/MANIFEST.MF" or (
                        upper.startswith("META-INF/") and upper.endswith(signature_suffixes)
                    )
                    if is_signature:
                        continue
                    target.writestr(item, source.read(item))
        os.replace(temporary, archive_path)
    except Exception:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise


async def _debug_sign_apk(apk_path, keystore, logs):
    zipalign = _find_android_build_tool("zipalign")
    apksigner = _find_android_build_tool("apksigner")
    if not zipalign or not apksigner:
        raise RuntimeError("zipalign atau apksigner tidak ditemui untuk debug signing")

    _strip_archive_signatures(apk_path)
    aligned_path = apk_path + ".aligned"
    signed_path = apk_path + ".debug-signed"
    try:
        command = (
            f"{shlex.quote(zipalign)} -P 16 -f 4 "
            f"{shlex.quote(apk_path)} {shlex.quote(aligned_path)}"
        )
        code, output, error = await run_cmd(command, timeout=180)
        if code != 0 or not os.path.exists(aligned_path):
            raise RuntimeError(f"zipalign gagal: {(error or output)[-1000:]}")

        command = (
            f"{shlex.quote(apksigner)} sign --ks {shlex.quote(keystore)} "
            "--ks-key-alias androiddebugkey --ks-pass pass:android "
            "--key-pass pass:android --v4-signing-enabled false "
            f"--out {shlex.quote(signed_path)} {shlex.quote(aligned_path)}"
        )
        code, output, error = await run_cmd(command, timeout=180)
        if code != 0 or not os.path.exists(signed_path):
            raise RuntimeError(f"apksigner gagal: {(error or output)[-1000:]}")

        command = f"{shlex.quote(apksigner)} verify --verbose --print-certs {shlex.quote(signed_path)}"
        code, output, error = await run_cmd(command, timeout=120)
        if code != 0:
            raise RuntimeError(f"Pengesahan APK gagal: {(error or output)[-1000:]}")

        command = (
            f"{shlex.quote(zipalign)} -c -P 16 -v 4 "
            f"{shlex.quote(signed_path)}"
        )
        code, output, error = await run_cmd(command, timeout=120)
        if code != 0:
            raise RuntimeError(f"Pengesahan zipalign APK gagal: {(error or output)[-1000:]}")

        os.replace(signed_path, apk_path)
        logs.append(f"Debug signed and verified: {os.path.basename(apk_path)}")
    finally:
        for temporary in (aligned_path, signed_path):
            if os.path.exists(temporary):
                os.remove(temporary)


async def _debug_sign_aab(aab_path, keystore, logs):
    _strip_archive_signatures(aab_path)
    signed_path = aab_path + ".debug-signed"
    try:
        command = (
            "jarsigner -keystore " + shlex.quote(keystore)
            + " -storepass android -keypass android"
            + " -sigalg SHA256withRSA -digestalg SHA-256"
            + " -signedjar " + shlex.quote(signed_path)
            + " " + shlex.quote(aab_path)
            + " androiddebugkey"
        )
        code, output, error = await run_cmd(command, timeout=180)
        if code != 0 or not os.path.exists(signed_path):
            raise RuntimeError(f"jarsigner AAB gagal: {(error or output)[-1000:]}")

        command = f"jarsigner -verify -verbose {shlex.quote(signed_path)}"
        code, output, error = await run_cmd(command, timeout=120)
        verification_text = (output + "\n" + error).lower()
        if code != 0 or "jar verified." not in verification_text:
            raise RuntimeError(f"Pengesahan AAB gagal: {(error or output)[-1000:]}")

        os.replace(signed_path, aab_path)
        logs.append(f"Debug signed and verified: {os.path.basename(aab_path)}")
    finally:
        if os.path.exists(signed_path):
            os.remove(signed_path)


async def _debug_sign_apks(apks_path, keystore, logs):
    temporary = apks_path + ".debug-signed"
    work_dir = tempfile.mkdtemp(prefix="earlxz-apks-")
    signed_count = 0
    try:
        with zipfile.ZipFile(apks_path, "r") as source:
            with zipfile.ZipFile(temporary, "w") as target:
                for index, item in enumerate(source.infolist()):
                    if item.is_dir():
                        target.writestr(item, b"")
                        continue
                    data = source.read(item)
                    if item.filename.lower().endswith(".apk"):
                        local_apk = os.path.join(work_dir, f"entry-{index}.apk")
                        with open(local_apk, "wb") as output:
                            output.write(data)
                        await _debug_sign_apk(local_apk, keystore, logs)
                        with open(local_apk, "rb") as signed_apk:
                            data = signed_apk.read()
                        signed_count += 1
                    target.writestr(item, data)
        if signed_count == 0:
            raise RuntimeError("Fail APKS tidak mengandungi APK")
        os.replace(temporary, apks_path)
        logs.append(f"Debug signed APKS entries: {signed_count}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        if os.path.exists(temporary):
            os.remove(temporary)


async def ensure_debug_signed_outputs(result):
    files = result.get("files", [])
    logs = result.setdefault("logs", [])
    if not files:
        raise RuntimeError("Build berjaya tetapi tiada output untuk debug signing")

    await _ensure_debug_signing_tools(logs)
    keystore = await _ensure_debug_keystore(logs)
    for output_path in files:
        if not os.path.exists(output_path):
            raise RuntimeError(f"Output build tidak ditemui: {output_path}")
        extension = os.path.splitext(output_path)[1].lower()
        if extension == ".apk":
            await _debug_sign_apk(output_path, keystore, logs)
        elif extension == ".aab":
            await _debug_sign_aab(output_path, keystore, logs)
        elif extension == ".apks":
            await _debug_sign_apks(output_path, keystore, logs)
        else:
            raise RuntimeError(f"Format output tidak disokong untuk debug signing: {output_path}")
    result["debug_signed"] = True
    return result


async def _configure_android_paths(engine, project_dir, logs, major, debug_keystore):
    project_file = os.path.join(project_dir, "project.godot")
    if not os.path.exists(project_file):
        raise RuntimeError("project.godot tidak ditemui semasa konfigurasi Android Godot")

    with open(project_file, "rb") as source:
        original_project = source.read()
    project_text = original_project.decode("utf-8-sig", errors="replace")

    addons_dir = os.path.join(project_dir, "addons")
    addons_existed = os.path.isdir(addons_dir)
    addon_base = f"earlxz_builder_android_setup_{os.getpid()}"
    addon_name = addon_base
    counter = 1
    while os.path.exists(os.path.join(addons_dir, addon_name)):
        addon_name = f"{addon_base}_{counter}"
        counter += 1
    plugin_dir = os.path.join(addons_dir, addon_name)
    plugin_cfg = os.path.join(plugin_dir, "plugin.cfg")
    plugin_script = os.path.join(plugin_dir, "plugin.gd")
    plugin_resource = f"res://addons/{addon_name}/plugin.cfg"

    array_type = "PoolStringArray" if major == 3 else "PackedStringArray"
    editor_section = (
        "[editor_plugins]\n"
        f'enabled={array_type}("{plugin_resource}")\n'
    )
    section_pattern = re.compile(
        r"(?ms)^\[editor_plugins\]\s*\n.*?(?=^\[[^\n]+\]\s*$|\Z)"
    )
    if section_pattern.search(project_text):
        configured_project = section_pattern.sub(editor_section + "\n", project_text, count=1)
    else:
        configured_project = project_text.rstrip() + "\n\n" + editor_section

    plugin_config = '''[plugin]
name="Earlxz Builder Android Setup"
description="Temporary Android export configuration"
author="Earlxz"
version="1.0"
script="plugin.gd"
'''
    if major == 3:
        plugin_code = '''tool
extends EditorPlugin

func _enter_tree():
    var settings = get_editor_interface().get_editor_settings()
    var android_home = OS.get_environment("ANDROID_HOME")
    var java_home = OS.get_environment("JAVA_HOME")
    var debug_keystore = OS.get_environment("EARLXZ_GODOT_DEBUG_KEYSTORE")
    if android_home != "":
        settings.set("export/android/android_sdk_path", android_home)
    if java_home != "":
        settings.set("export/android/java_sdk_path", java_home)
    if debug_keystore != "":
        settings.set("export/android/debug_keystore", debug_keystore)
    settings.set("export/android/debug_keystore_user", "androiddebugkey")
    settings.set("export/android/debug_keystore_pass", "android")
    var sentinel_path = OS.get_environment("EARLXZ_GODOT_SETUP_SENTINEL")
    if sentinel_path != "":
        var file = File.new()
        if file.open(sentinel_path, File.WRITE) == OK:
            file.store_string("ok")
            file.close()
    get_tree().quit()
'''
    else:
        plugin_code = '''@tool
extends EditorPlugin

func _enter_tree():
    var settings = get_editor_interface().get_editor_settings()
    var android_home = OS.get_environment("ANDROID_HOME")
    var java_home = OS.get_environment("JAVA_HOME")
    var debug_keystore = OS.get_environment("EARLXZ_GODOT_DEBUG_KEYSTORE")
    if android_home != "":
        settings.set_setting("export/android/android_sdk_path", android_home)
    if java_home != "":
        settings.set_setting("export/android/java_sdk_path", java_home)
    if debug_keystore != "":
        settings.set_setting("export/android/debug_keystore", debug_keystore)
    settings.set_setting("export/android/debug_keystore_user", "androiddebugkey")
    settings.set_setting("export/android/debug_keystore_pass", "android")
    var sentinel_path = OS.get_environment("EARLXZ_GODOT_SETUP_SENTINEL")
    if sentinel_path != "":
        var file = FileAccess.open(sentinel_path, FileAccess.WRITE)
        if file:
            file.store_string("ok")
    get_tree().quit()
'''

    sentinel_dir = tempfile.mkdtemp(prefix="earlxz_godot_setup_")
    sentinel_path = os.path.join(sentinel_dir, "configured")
    managed_env = {
        "EARLXZ_GODOT_DEBUG_KEYSTORE": os.path.abspath(debug_keystore),
        "EARLXZ_GODOT_SETUP_SENTINEL": sentinel_path,
        "GODOT_ANDROID_KEYSTORE_DEBUG_PATH": os.path.abspath(debug_keystore),
        "GODOT_ANDROID_KEYSTORE_DEBUG_USER": "androiddebugkey",
        "GODOT_ANDROID_KEYSTORE_DEBUG_PASSWORD": "android",
    }
    previous_env = {name: os.environ.get(name) for name in managed_env}

    try:
        os.makedirs(plugin_dir, exist_ok=False)
        with open(plugin_cfg, "w", encoding="utf-8", newline="\n") as output:
            output.write(plugin_config)
        with open(plugin_script, "w", encoding="utf-8", newline="\n") as output:
            output.write(plugin_code)
        with open(project_file, "w", encoding="utf-8", newline="\n") as output:
            output.write(configured_project)

        os.environ.update(managed_env)
        display_flag = "--no-window" if major == 3 else "--headless"
        command = (
            f"{shlex.quote(engine)} {display_flag} --editor "
            f"--path {shlex.quote(project_dir)}"
        )
        code, output, error = await run_cmd(command, timeout=180)
        if code != 0:
            raise RuntimeError(
                "Godot gagal menyimpan Android SDK/JDK/debug keystore: "
                + (error or output or "unknown Godot editor error")[-2000:]
            )
        if not os.path.exists(sentinel_path):
            raise RuntimeError(
                "Plugin konfigurasi Android Godot tidak dijalankan; tetapan eksport tidak disahkan"
            )
        logs.append("Godot Android SDK/JDK/debug keystore configured")
    finally:
        try:
            with open(project_file, "wb") as output:
                output.write(original_project)
        except OSError:
            logger.exception("Gagal memulihkan project.godot selepas konfigurasi sementara")

        shutil.rmtree(plugin_dir, ignore_errors=True)
        if not addons_existed:
            try:
                if os.path.isdir(addons_dir) and not os.listdir(addons_dir):
                    os.rmdir(addons_dir)
            except OSError:
                pass
        shutil.rmtree(sentinel_dir, ignore_errors=True)

        for name, previous_value in previous_env.items():
            if previous_value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous_value

async def build_godot(project_dir, config):
    logs = []
    if not os.path.exists(os.path.join(project_dir, "project.godot")):
        return {"success": False, "error": "project.godot tidak ditemui", "logs": logs}

    if any(filename.lower().endswith(".csproj") for filename in os.listdir(project_dir)):
        return {
            "success": False,
            "error": "Godot C# / Mono belum disokong oleh runner ini. Gunakan projek Godot GDScript.",
            "logs": logs,
        }

    created_preset = False
    preset_path = os.path.join(project_dir, "export_presets.cfg")
    original_preset = None

    try:
        engine, version, major = await _setup_godot(project_dir, logs)
        await _setup_godot_android_requirements(version, logs)
        debug_keystore = await _ensure_debug_keystore(logs)
        await _configure_android_paths(engine, project_dir, logs, major, debug_keystore)

        preset_name, output_format = _find_android_preset(project_dir)
        if not preset_name:
            if os.path.exists(preset_path):
                original_preset = _read_text(preset_path)
            _write_fallback_preset(project_dir, major)
            preset_name = "Android"
            output_format = "apk"
            created_preset = True
            logs.append("Android export preset generated temporarily")

        output_dir = os.path.join(project_dir, "godot_build")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.abspath(os.path.join(output_dir, f"app-debug.{output_format}"))

        display_flag = "--no-window" if major == 3 else "--headless"
        command = (
            f"{shlex.quote(engine)} {display_flag} --path {shlex.quote(project_dir)} "
            f"--export-debug {shlex.quote(preset_name)} {shlex.quote(output_path)}"
        )
        code, output, error = await run_cmd(command, timeout=1800)
        logs.append(f"Godot {major} debug export: {'OK' if code == 0 else 'FAIL'}")

        if code != 0 or not os.path.exists(output_path):
            details = (error or output or "Unknown Godot export error")[-8000:]
            return {
                "success": False,
                "error": f"Godot Android debug export gagal\n{details}",
                "logs": logs,
            }

        logs.append(f"Godot {version} output (debug signed): {os.path.basename(output_path)}")
        return {"success": True, "files": [output_path], "logs": logs}
    except Exception as error:
        logger.exception("Godot build setup failed")
        return {"success": False, "error": f"Godot build gagal: {error}", "logs": logs}
    finally:
        if created_preset:
            try:
                if original_preset is None:
                    os.remove(preset_path)
                else:
                    with open(preset_path, "w", encoding="utf-8") as f:
                        f.write(original_preset)
            except OSError:
                pass


def get_flutter_version_from_pubspec(project_dir):
    """Detect minimum Flutter version from pubspec.yaml."""
    pubspec_path = os.path.join(project_dir, "pubspec.yaml")
    if not os.path.exists(pubspec_path):
        return "stable"
    try:
        with open(pubspec_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        match = re.search(r'flutter\s*:\s*["\']?>=\s*([0-9]+\.[0-9]+\.[0-9]+)', content)
        if match:
            return match.group(1)
    except Exception:
        pass
    return "stable"


def is_old_flutter_style(project_dir):
    """Detect old Flutter Android plugin-loader style."""
    settings_path = os.path.join(project_dir, "android", "settings.gradle")
    if not os.path.exists(settings_path):
        return False
    try:
        with open(settings_path, "r", encoding="utf-8", errors="replace") as f:
            return "app_plugin_loader.gradle" in f.read()
    except Exception:
        return False


def get_java_version_for_project(project_dir):
    """Detect the highest Java requirement declared by the project."""
    skip_dirs = {"build", ".gradle", "node_modules", ".dart_tool"}
    candidates = []

    def extract_java_versions(content):
        found = []
        for match in re.finditer(r'\bJavaVersion\.VERSION_(?:1_(\d)|(\d{1,2}))\b', content):
            found.append(int(match.group(1) or match.group(2)))
        for match in re.finditer(r'jvmTarget\s*=\s*["\x27](1\.(\d)|(\d{1,2}))["\x27]', content):
            found.append(int(match.group(2) or match.group(3)))
        for match in re.finditer(r'JavaLanguageVersion\.of\((\d+)\)', content):
            found.append(int(match.group(1)))
        for match in re.finditer(r'(?:source|target)Compatibility\s*=\s*(\d+)\b', content):
            found.append(int(match.group(1)))
        return found

    agp_version = None
    for root, dirs, filenames in os.walk(project_dir):
        dirs[:] = [d for d in dirs if os.path.basename(d) not in skip_dirs]
        for filename in filenames:
            if filename not in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"):
                continue
            try:
                path = os.path.join(root, filename)
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                candidates.extend(extract_java_versions(content))
                if agp_version is None:
                    match = re.search(r'com\.android\.tools\.build:gradle:([0-9.]+)', content)
                    if match:
                        agp_version = match.group(1)
                    plugin_match = re.search(
                        r'id\s*\(?\s*["\x27]com\.android\.(?:application|library)["\x27]\s*\)?\s+version\s+["\x27]([0-9.]+)["\x27]',
                        content,
                    )
                    if plugin_match:
                        agp_version = plugin_match.group(1)
            except Exception:
                pass

    agp_minimum = 17
    if agp_version:
        try:
            major = int(agp_version.split(".")[0])
            if major >= 8:
                agp_minimum = 17
            elif major >= 7:
                agp_minimum = 11
            else:
                agp_minimum = 8
        except Exception:
            pass

    return str(max(candidates + [agp_minimum, 17]))


def detect_project_type(project_dir):
    """Detect the project type from its files only; no user-provided hint is used."""

    def exists(*parts):
        return os.path.exists(os.path.join(project_dir, *parts))

    def isdir(*parts):
        return os.path.isdir(os.path.join(project_dir, *parts))

    has_smali = isdir("smali") or isdir("smali_classes2") or isdir("smali_classes3")
    if exists("apktool.yml") or (exists("AndroidManifest.xml") and has_smali):
        return "smali"

    if exists("project.godot"):
        return "godot"

    if exists("pubspec.yaml"):
        return "flutter"

    package_path = os.path.join(project_dir, "package.json")
    if exists("package.json"):
        try:
            with open(package_path, "r", encoding="utf-8", errors="replace") as f:
                package_data = json.load(f)
            dependencies = {}
            dependencies.update(package_data.get("dependencies", {}))
            dependencies.update(package_data.get("devDependencies", {}))
            dependency_names = " ".join(dependencies.keys()).lower()
            package_name = str(package_data.get("name", "")).lower()

            has_capacitor = "@capacitor/core" in dependency_names or "@capacitor/android" in dependency_names
            has_ionic = "@ionic" in dependency_names or "ionic" in package_name

            if has_ionic:
                return "ionic"
            if has_capacitor:
                return "capacitor"
            if "react-native" in dependency_names:
                return "react_native"
            if exists("config.xml"):
                return "cordova"
        except Exception:
            pass

    if exists("capacitor.config.json") or exists("capacitor.config.ts"):
        return "capacitor"

    if exists("config.xml"):
        return "cordova"

    for filename in ("settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts"):
        if exists(filename):
            return "native"

    return None


def package_result(project_dir, result, target_file):
    if result.get("success"):
        base_name, extension = os.path.splitext(target_file)
        output_zip = f"{base_name}.zip" if extension.lower() == ".txt" else target_file
    else:
        output_zip = f"{os.path.splitext(target_file)[0]}_error.zip"

    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        if result.get("success"):
            for filepath in result.get("files", []):
                if os.path.exists(filepath):
                    archive.write(filepath, os.path.basename(filepath))
        else:
            os.makedirs(project_dir, exist_ok=True)
            error_log = os.path.join(project_dir, "build_error.log")
            with open(error_log, "w", encoding="utf-8") as f:
                f.write("=== BUILD ERROR ===\n\n")
                f.write(result.get("error", "Unknown error") + "\n\n")
                if result.get("logs"):
                    f.write("=== BUILD LOGS ===\n\n")
                    f.write("\n".join(result["logs"]))
            archive.write(error_log, "ERROR_LOG.txt")
    return output_zip


def find_project_directory(build_dir):
    ignored_dirs = {
        ".git", ".gradle", ".dart_tool", "node_modules", "build",
        "godot_build", "__pycache__", "__macosx",
    }
    candidates = []

    for root, dirs, _ in os.walk(build_dir):
        dirs[:] = [d for d in dirs if d.lower() not in ignored_dirs]
        project_type = detect_project_type(root)
        if not project_type:
            continue
        relative = os.path.relpath(root, build_dir)
        depth = 0 if relative == "." else relative.count(os.sep) + 1
        priority = {
            "smali": 0,
            "godot": 1,
            "flutter": 2,
            "react_native": 3,
            "ionic": 4,
            "capacitor": 5,
            "cordova": 6,
            "native": 7,
        }.get(project_type, 99)
        candidates.append((depth, priority, root, project_type))

    if not candidates:
        return build_dir
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates[0][2]


async def download_project_link(download_url, destination):
    timeout = aiohttp.ClientTimeout(total=600)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(download_url, allow_redirects=True) as response:
            if response.status != 200:
                raise RuntimeError(f"Download gagal: HTTP {response.status}")
            with open(destination, "wb") as f:
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    f.write(chunk)
    if not os.path.exists(destination) or os.path.getsize(destination) == 0:
        raise RuntimeError("Fail pautan kosong atau tidak dapat dimuat turun")


async def send_failure(bot_token, chat_id, user_display, project_type, result, project_dir, target_file):
    try:
        output_zip = package_result(project_dir, result, target_file)
    except Exception:
        logger.exception("Gagal membungkus log ralat")
        output_zip = None

    caption = "<blockquote>" + (
        "<b>BUILD FAILED</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"◆ User: {user_display}\n"
        f"◆ Type: {project_type.upper()}\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "Sila semak log ralat yang dilampirkan.\n"
        "BUILD BY @Earlxz"
    ) + "</blockquote>"
    sent = False
    if output_zip:
        try:
            sent = await send_telegram_document(bot_token, chat_id, output_zip, caption)
        except Exception:
            logger.exception("Gagal menghantar fail ralat ke Telegram")
    if not sent:
        try:
            await send_telegram_notification(bot_token, chat_id, caption)
        except Exception:
            logger.exception("Gagal menghantar notifikasi ralat ke Telegram")
    return output_zip


def sanitize_target_file(raw_target):
    decoded = unquote(raw_target or "source.zip").replace("\\", "/")
    filename = os.path.basename(decoded).strip()
    if not filename or filename in (".", ".."):
        raise ValueError("TARGET_FILE tidak sah")
    return filename


async def main():
    bot_token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    channel_id = os.getenv("CHANNEL_ID")
    user_display = os.getenv("USER_DISPLAY", "Unknown")
    in_progress = os.getenv("IN_PROGRESS", "1")
    raw_target_file = os.getenv("TARGET_FILE", "source.zip")
    try:
        target_file = sanitize_target_file(raw_target_file)
    except ValueError as error:
        logger.error(str(error))
        try:
            await send_telegram_notification(
                bot_token,
                chat_id,
                f"<blockquote><b>BUILD FAILED</b>\n\n{error}\n\nBUILD BY @Earlxz</blockquote>",
            )
        except Exception:
            logger.exception("Gagal menghantar ralat TARGET_FILE ke Telegram")
        return 1

    submitted_file = os.path.join("temp", target_file)
    source_zip = submitted_file
    downloaded_zip = None
    build_dir = "build_area"

    if not os.path.exists(source_zip):
        result = {
            "success": False,
            "error": f"Source zip not found: {source_zip}",
            "logs": [f"TARGET_FILE decoded as: {target_file}"],
        }
        logger.error(result["error"])
        await send_failure(
            bot_token, chat_id, user_display, "unknown", result, build_dir, target_file
        )
        return 1

    if target_file.lower().endswith(".txt"):
        try:
            with open(source_zip, "r", encoding="utf-8", errors="replace") as f:
                download_url = f.read().strip()
            if not download_url.startswith(("http://", "https://")):
                raise RuntimeError("Pautan projek tidak sah")
            downloaded_zip = os.path.splitext(source_zip)[0] + ".zip"
            logger.info("Downloading project from submitted link")
            await download_project_link(download_url, downloaded_zip)
            source_zip = downloaded_zip
            logger.info("Project download completed")
        except Exception as error:
            result = {
                "success": False,
                "error": f"Gagal memuat turun projek daripada pautan: {error}",
                "logs": [],
            }
            await send_failure(
                bot_token, chat_id, user_display, "unknown", result, build_dir, target_file
            )
            return 1

    try:
        os.makedirs(build_dir, exist_ok=True)
        with zipfile.ZipFile(source_zip, "r") as archive:
            archive.extractall(build_dir)
    except Exception as error:
        result = {
            "success": False,
            "error": f"Fail projek bukan ZIP yang sah atau rosak: {error}",
            "logs": [],
        }
        await send_failure(
            bot_token, chat_id, user_display, "unknown", result, build_dir, target_file
        )
        return 1

    project_dir = build_dir
    final_type = None
    try:
        project_dir = find_project_directory(build_dir)
        final_type = detect_project_type(project_dir)
        logger.info(f"Project type detected: {final_type or 'unknown'}")

        if not final_type:
            result = {
                "success": False,
                "error": "Jenis projek tidak dapat dikesan daripada kandungan ZIP.",
                "logs": [],
            }
        else:
            java_version = get_java_version_for_project(project_dir)
            if final_type in ("react_native", "ionic", "cordova", "capacitor"):
                java_version = "17"

            flutter_version = "stable"
            old_flutter_style = False
            if final_type == "flutter":
                flutter_version = get_flutter_version_from_pubspec(project_dir)
                old_flutter_style = is_old_flutter_style(project_dir)
                logger.info(
                    f"Flutter version required: {flutter_version}, old style: {old_flutter_style}"
                )

            config = {
                "java_version": java_version,
                "flutter_version": flutter_version,
                "old_flutter_style": old_flutter_style,
            }
            logger.info(f"Targeting Java {java_version} for {final_type} project")

            if final_type == "godot":
                result = await build_godot(project_dir, config)
            else:
                result = await build_project(project_dir, {"type": final_type, "config": config})
            if result.get("success"):
                result = await ensure_debug_signed_outputs(result)
    except Exception as error:
        logger.exception("Unhandled build worker error")
        result = {
            "success": False,
            "error": f"Build worker gagal: {error}",
            "logs": [],
        }

    exit_code = 0
    if not result.get("success"):
        exit_code = 1
        logger.error(f"Build Failed: {result.get('error')}")
        await send_failure(
            bot_token,
            chat_id,
            user_display,
            final_type or "unknown",
            result,
            project_dir,
            target_file,
        )
    else:
        try:
            output_zip = package_result(project_dir, result, target_file)
        except Exception as error:
            logger.exception("Gagal membungkus output build")
            exit_code = 1
            failure_result = {
                "success": False,
                "error": f"Build siap tetapi output gagal dibungkus: {error}",
                "logs": result.get("logs", []),
            }
            await send_failure(
                bot_token,
                chat_id,
                user_display,
                final_type or "unknown",
                failure_result,
                project_dir,
                target_file,
            )
        else:
            user_caption = "<blockquote>" + (
                "<b>Build Successful!</b>\n\n"
                f"Project: {output_zip}\n"
                f"Type: {final_type.upper()}\n\n"
                "⚠️ Output menggunakan debug signing. Sign semula dengan keystore sendiri untuk release.\n\n"
                "BUILD BY @Earlxz"
            ) + "</blockquote>"
            channel_caption = "<blockquote>" + (
                "<b>BUILD SUCCESSFUL</b>\n\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"◆ User: {user_display}\n"
                f"◆ APK: {target_file}\n"
                f"◆ Type: {final_type.upper()}\n"
                f"◆ InProgress: {in_progress}\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "Build By: @BuildApkEarlbot"
            ) + "</blockquote>"

            sent = await send_telegram_document(bot_token, chat_id, output_zip, user_caption)
            if not sent:
                file_size = os.path.getsize(output_zip)
                if file_size > 2147483648:
                    link = await upload_gofile(output_zip)
                    user_message = user_caption + f"\n\n🔗 Link: {link}"
                else:
                    user_message = (
                        user_caption
                        + "\n\n❌ Gagal menghantar fail. Saiz fail mungkin terlalu besar atau ralat teknikal."
                    )
                await send_telegram_notification(bot_token, chat_id, user_message)

            if channel_id and channel_id.strip():
                await send_telegram_notification(bot_token, channel_id, channel_caption)

    shutil.rmtree(build_dir, ignore_errors=True)
    for path in {source_zip, downloaded_zip}:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
