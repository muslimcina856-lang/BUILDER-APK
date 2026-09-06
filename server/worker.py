import asyncio
import base64
import html
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
from urllib.parse import unquote, urlparse

import aiohttp

from builder import build_project, run_cmd, setup_java
from upload_handler import (
    download_telegram_document_reference,
    upload_gofile,
    send_telegram_notification,
    send_telegram_document,
)


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


def _detect_godot_project_kind(project_dir):
    """Return ``dotnet`` for Godot C# projects, otherwise ``standard``.

    Godot records ``C#`` in ``config/features`` for .NET projects.  A root
    ``.csproj``/``.sln`` is also treated as authoritative so older Godot 3
    projects are detected correctly without recursively mistaking an addon
    source tree for the main project.
    """
    project_text = _read_text(os.path.join(project_dir, "project.godot"))
    if re.search(r'["\x27]C#["\x27]', project_text, re.IGNORECASE):
        return "dotnet"
    if re.search(r"(?m)^dotnet/project/", project_text):
        return "dotnet"

    try:
        root_files = os.listdir(project_dir)
    except OSError:
        root_files = []
    if any(name.lower().endswith((".csproj", ".sln")) for name in root_files):
        return "dotnet"
    return "standard"


def _safe_android_package_suffix(project_name):
    suffix = re.sub(r"[^a-z0-9]+", "", (project_name or "").lower())[:40]
    if not suffix:
        return "project"
    if not suffix[0].isalpha():
        suffix = "app" + suffix
    return suffix[:40]


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


def _resolve_release_sync(version_hint, dotnet=False):
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
        is_mono = "mono" in lower
        if "export_templates.tpz" in lower and is_mono == bool(dotnet):
            template_candidates.append((name, url))
            continue
        if not lower.endswith(".zip") or is_mono != bool(dotnet):
            continue
        if major == 3:
            if not any(token in lower for token in ("x11", "linux", "headless", "server")):
                continue
        elif "linux" not in lower:
            continue
        if not any(token in lower for token in (
            "x86_64", "x11_64", "x11.64", "linux.64", "headless.64", "server.64"
        )):
            continue
        if any(token in lower for token in ("arm64", "arm32", "web_editor")):
            continue
        engine_candidates.append((name, url))

    if not engine_candidates:
        variant = " .NET/Mono" if dotnet else ""
        raise RuntimeError(f"Binary Linux x86_64 Godot{variant} {normalized} tidak ditemui")
    if not template_candidates:
        variant = " .NET/Mono" if dotnet else ""
        raise RuntimeError(f"Export templates Godot{variant} {normalized} tidak ditemui")

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
        "dotnet": bool(dotnet),
        "engine_name": engine_candidates[0][0],
        "engine_url": engine_candidates[0][1],
        "templates_name": template_candidates[0][0],
        "templates_url": template_candidates[0][1],
    }


async def _resolve_release(version_hint, dotnet=False):
    return await asyncio.to_thread(_resolve_release_sync, version_hint, dotnet)


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
            if lower.endswith((
                ".dll", ".so", ".dylib", ".pdb", ".xml", ".json", ".cs", ".deps", ".runtimeconfig"
            )):
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


def _godot_template_version_key(version, dotnet=False):
    return f"{version}.stable" + (".mono" if dotnet else "")


def _install_templates(extracted_dir, version, dotnet=False):
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

    version_key = _godot_template_version_key(version, dotnet)
    data_home = os.path.expanduser(os.environ.get("XDG_DATA_HOME", "~/.local/share"))
    destinations = [
        os.path.join(data_home, "godot", "export_templates", version_key),
        os.path.join(data_home, "godot", "templates", version_key),
    ]
    for destination in destinations:
        if os.path.isdir(destination):
            shutil.rmtree(destination, ignore_errors=True)
        _copy_template_contents(templates_dir, destination)


async def _setup_godot(project_dir, logs, dotnet=False):
    version_hint = _detect_version_hint(project_dir)
    logs.append(f"Godot version hint: {version_hint}")
    release = await _resolve_release(version_hint, dotnet=dotnet)
    variant = " .NET/Mono" if dotnet else ""
    logs.append(f"Godot{variant} stable selected: {release['version']}")

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

    _install_templates(template_extract, release["version"], dotnet=dotnet)
    logs.append(f"Godot{variant} export templates installed")
    return engine, release["version"], release["major"]


def _extract_cfg_value(section, key):
    pattern = rf'^{re.escape(key)}\s*=\s*["\x27](.*?)["\x27]\s*$'
    match = re.search(pattern, section, re.MULTILINE)
    if match:
        return match.group(1)
    bare = re.search(rf'^{re.escape(key)}\s*=\s*([^\r\n#;]+)', section, re.MULTILINE)
    return bare.group(1).strip() if bare else ""


def _cfg_bool(section, key, default=False):
    value = _extract_cfg_value(section, key).strip().lower()
    if value in ("true", "1", "yes", "on"):
        return True
    if value in ("false", "0", "no", "off"):
        return False
    return default


def _preset_has_release_signing(section):
    env_values = [
        os.environ.get("GODOT_ANDROID_KEYSTORE_RELEASE_PATH", "").strip(),
        os.environ.get("GODOT_ANDROID_KEYSTORE_RELEASE_USER", "").strip(),
        os.environ.get("GODOT_ANDROID_KEYSTORE_RELEASE_PASSWORD", "").strip(),
    ]
    if all(env_values):
        return True

    keys = (
        "keystore/release",
        "keystore/release_user",
        "keystore/release_password",
    )
    return all(_extract_cfg_value(section, key).strip() for key in keys)


def _resolve_project_file(project_dir, value):
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("res://"):
        return os.path.abspath(os.path.join(project_dir, *value[6:].split("/")))
    if os.path.isabs(value):
        return value
    project_relative = os.path.abspath(os.path.join(project_dir, value))
    if os.path.exists(project_relative):
        return project_relative
    return os.path.abspath(value)


def _prepare_release_signing_environment(project_dir, logs):
    """Map optional secure runner secrets to Godot's official export env vars.

    The keystore can be supplied either as an existing path (including a
    project-relative/res:// path) or as a Base64 GitHub secret so projects do
    not need to commit signing material.
    """
    path_value = (
        os.environ.get("GODOT_ANDROID_KEYSTORE_RELEASE_PATH", "").strip()
        or os.environ.get("GODOT_RELEASE_KEYSTORE_PATH", "").strip()
    )
    base64_value = os.environ.get("GODOT_RELEASE_KEYSTORE_BASE64", "").strip()
    user_value = (
        os.environ.get("GODOT_ANDROID_KEYSTORE_RELEASE_USER", "").strip()
        or os.environ.get("GODOT_RELEASE_KEYSTORE_USER", "").strip()
    )
    password_value = (
        os.environ.get("GODOT_ANDROID_KEYSTORE_RELEASE_PASSWORD", "").strip()
        or os.environ.get("GODOT_RELEASE_KEYSTORE_PASSWORD", "").strip()
    )
    if not any((path_value, base64_value, user_value, password_value)):
        return False
    if not user_value or not password_value or not (path_value or base64_value):
        logs.append("WARNING: Godot release signing environment tidak lengkap")
        return False

    generated = False
    if path_value:
        resolved_path = _resolve_project_file(project_dir, path_value)
        if not os.path.exists(resolved_path):
            logs.append("WARNING: Godot release keystore daripada environment tidak ditemui")
            return False
    else:
        try:
            compact = re.sub(r"\s+", "", base64_value)
            decoded = base64.b64decode(compact, validate=True)
        except Exception:
            logs.append("WARNING: GODOT_RELEASE_KEYSTORE_BASE64 tidak sah")
            return False
        if not decoded:
            logs.append("WARNING: GODOT_RELEASE_KEYSTORE_BASE64 kosong")
            return False
        fd, resolved_path = tempfile.mkstemp(
            prefix="earlxz-godot-release-", suffix=".keystore"
        )
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(decoded)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.remove(resolved_path)
            except OSError:
                pass
            raise
        os.chmod(resolved_path, stat.S_IRUSR | stat.S_IWUSR)
        generated = True

    os.environ["GODOT_ANDROID_KEYSTORE_RELEASE_PATH"] = resolved_path
    os.environ["GODOT_ANDROID_KEYSTORE_RELEASE_USER"] = user_value
    os.environ["GODOT_ANDROID_KEYSTORE_RELEASE_PASSWORD"] = password_value
    logs.append("Godot release signing configured from secure environment")
    return {"path": resolved_path, "generated": generated}


def _godot_cfg_quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _inject_godot3_release_signing(project_dir, selected_presets, logs):
    """Temporarily bridge secure signing env values into Godot 3 presets.

    Godot 3's Android exporter reads release signing credentials directly
    from ``export_presets.cfg``.  Modern Godot environment variables are not
    consumed by that exporter, so CI secrets are injected only for the build
    window and the caller restores the original bytes in ``finally``.
    """
    values = {
        "keystore/release": os.environ.get("GODOT_ANDROID_KEYSTORE_RELEASE_PATH", "").strip(),
        "keystore/release_user": os.environ.get("GODOT_ANDROID_KEYSTORE_RELEASE_USER", "").strip(),
        "keystore/release_password": os.environ.get("GODOT_ANDROID_KEYSTORE_RELEASE_PASSWORD", "").strip(),
    }
    if not all(values.values()):
        return None

    preset_path = os.path.join(project_dir, "export_presets.cfg")
    try:
        with open(preset_path, "rb") as source:
            original = source.read()
        content = original.decode("utf-8", errors="replace")
    except OSError:
        return None

    changed = False
    for preset in selected_presets:
        index = preset.get("index")
        if index is None:
            continue
        header = rf"(?m)^\[preset\.{int(index)}\.options\]\s*$"
        header_match = re.search(header, content)
        if header_match:
            section_start = header_match.end()
            next_header = re.search(r"(?m)^\[", content[section_start:])
            section_end = section_start + (next_header.start() if next_header else len(content[section_start:]))
            section = content[section_start:section_end]
        else:
            if content and not content.endswith("\n"):
                content += "\n"
            content += f"\n[preset.{int(index)}.options]\n"
            section_start = len(content)
            section_end = len(content)
            section = ""

        updated = section
        for key, value in values.items():
            line = f"{key}={_godot_cfg_quote(value)}"
            pattern = rf"(?m)^{re.escape(key)}\s*=.*$"
            if re.search(pattern, updated):
                updated = re.sub(pattern, lambda _m, replacement=line: replacement, updated, count=1)
            else:
                if updated and not updated.endswith("\n"):
                    updated += "\n"
                updated += line + "\n"
        content = content[:section_start] + updated + content[section_end:]
        changed = True

    if not changed:
        return None

    with open(preset_path, "w", encoding="utf-8", newline="") as output:
        output.write(content)
    logs.append("Godot 3 release signing bridged securely for this build")
    return original


def _list_android_presets(project_dir):
    path = os.path.join(project_dir, "export_presets.cfg")
    content = _read_text(path).lstrip("\ufeff")
    if not content:
        return []

    sections = re.split(r"(?=^\[preset\.\d+\]\s*$)", content, flags=re.MULTILINE)
    presets = []
    for section in sections:
        index_match = re.search(r'^\[preset\.(\d+)\]\s*$', section, re.MULTILINE)
        if not index_match:
            continue
        if not re.search(r'^platform\s*=\s*["\x27]Android["\x27]\s*$', section, re.MULTILINE):
            continue
        name_match = re.search(r'^name\s*=\s*["\x27](.*?)["\x27]\s*$', section, re.MULTILINE)
        if not name_match:
            continue
        export_format = "aab" if re.search(
            r'^(?:gradle_build|custom_build)/export_format\s*=\s*1\s*$', section, re.MULTILINE
        ) else "apk"
        gradle_enabled = bool(re.search(
            r'^(?:gradle_build/use_gradle_build|custom_build/use_custom_build)\s*=\s*true\s*$',
            section,
            re.MULTILINE | re.IGNORECASE,
        ))
        presets.append({
            "index": int(index_match.group(1)),
            "name": name_match.group(1),
            "format": export_format,
            "runnable": _cfg_bool(section, "runnable", False),
            "gradle": gradle_enabled or export_format == "aab",
            "has_release_signing": _preset_has_release_signing(section),
            "section": section,
        })
    return presets


def _select_android_presets(presets, requested=None, build_all=False):
    if not presets:
        return []
    requested = (requested or "").strip()
    if requested:
        exact = [preset for preset in presets if preset["name"] == requested]
        if not exact:
            exact = [preset for preset in presets if preset["name"].lower() == requested.lower()]
        if not exact:
            available = ", ".join(preset["name"] for preset in presets)
            raise ValueError(f"Godot Android preset '{requested}' tidak ditemui. Tersedia: {available}")
        return exact
    if build_all:
        return list(presets)
    runnable = [preset for preset in presets if preset.get("runnable")]
    return [runnable[0] if runnable else presets[0]]


def _find_android_preset(project_dir):
    """Compatibility wrapper for callers/tests that expect the old API."""
    presets = _list_android_presets(project_dir)
    if not presets:
        return None, "apk"
    return presets[0]["name"], presets[0]["format"]


def _godot_cli_prefix(engine, major):
    quoted_engine = shlex.quote(engine)
    if major >= 4:
        return f"{quoted_engine} --headless"

    binary_name = os.path.basename(engine).lower()
    if "server" in binary_name or "headless" in binary_name:
        return quoted_engine

    xvfb = shutil.which("xvfb-run")
    if xvfb:
        return f"{shlex.quote(xvfb)} -a {quoted_engine}"
    return quoted_engine


def _godot_export_flag(major, variant):
    if variant == "debug":
        return "--export-debug"
    if variant == "release":
        return "--export" if major <= 3 else "--export-release"
    raise ValueError(f"Godot export variant tidak sah: {variant}")


def _export_variants(mode, has_release_signing):
    normalized = (mode or "auto").strip().lower()
    if normalized == "auto":
        return ["debug", "release"] if has_release_signing else ["debug"]
    if normalized == "debug":
        return ["debug"]
    if normalized == "release":
        return ["release"]
    if normalized == "both":
        return ["debug", "release"]
    raise ValueError("GODOT_EXPORT_MODE mesti auto, debug, release atau both")


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _write_fallback_preset(project_dir, major):
    path = os.path.join(project_dir, "export_presets.cfg")
    project_name = os.path.basename(os.path.abspath(project_dir)) or "project"
    package_suffix = _safe_android_package_suffix(project_name)

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


def _godot_android_profile(version):
    parsed = _version_tuple(version) or (4, 0, 0)
    major, minor, patch = parsed

    if major <= 3:
        if minor <= 2:
            return {
                "java": "8",
                "compile_sdk": "30",
                "build_tools": "30.0.3",
                "cmake": "3.10.2.4988404",
                "ndk": "21.4.7075529",
            }
        if minor <= 5:
            return {
                "java": "11",
                "compile_sdk": "31",
                "build_tools": "30.0.3",
                "cmake": "3.10.2.4988404",
                "ndk": "21.4.7075529",
            }
        if minor == 6 and patch >= 2:
            return {
                "java": "17",
                "compile_sdk": "35",
                "build_tools": "35.0.1",
                "cmake": "3.10.2.4988404",
                "ndk": "28.1.13356709",
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
    if minor <= 7:
        return {
            "java": "17",
            "compile_sdk": "35",
            "build_tools": "35.0.0" if minor == 5 else "35.0.1",
            "cmake": "3.10.2.4988404",
            "ndk": "28.1.13356709",
        }
    return {
        "java": "17",
        "compile_sdk": "36",
        "build_tools": "36.1.0",
        "cmake": "3.22.1",
        "ndk": "29.0.14206865",
    }


def _godot_dotnet_sdk_major(version):
    parsed = _version_tuple(version) or (4, 0, 0)
    major, minor, _ = parsed
    if major >= 4 and minor >= 5:
        return 9
    return 8


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


async def _active_dotnet_major():
    code, output, error = await run_cmd("dotnet --version", timeout=60)
    if code != 0:
        return None
    match = re.match(r"\s*(\d+)", output or error or "")
    return int(match.group(1)) if match else None


async def _setup_dotnet_sdk(version, logs):
    required = _godot_dotnet_sdk_major(version)
    current = await _active_dotnet_major()
    if current is not None and current >= required:
        logs.append(f".NET SDK {current} ready for Godot {version}")
        os.environ.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "1")
        os.environ.setdefault("DOTNET_NOLOGO", "1")
        os.environ.setdefault("NUGET_XMLDOC_MODE", "skip")
        return current

    install_dir = os.path.join(tempfile.gettempdir(), f"earlxz-dotnet-{required}")
    dotnet_bin = os.path.join(install_dir, "dotnet")
    if not os.path.exists(dotnet_bin):
        script_path = os.path.join(tempfile.gettempdir(), "earlxz-dotnet-install.sh")
        if not os.path.exists(script_path):
            await _download("https://dot.net/v1/dotnet-install.sh", script_path)
            os.chmod(script_path, os.stat(script_path).st_mode | stat.S_IXUSR)
        os.makedirs(install_dir, exist_ok=True)
        command = (
            f"bash {shlex.quote(script_path)} --channel {required}.0 "
            f"--install-dir {shlex.quote(install_dir)} --no-path"
        )
        code, output, error = await run_cmd(command, timeout=900)
        if code != 0 or not os.path.exists(dotnet_bin):
            raise RuntimeError(
                f"Gagal memasang .NET SDK {required}: "
                + (error or output or "unknown dotnet-install error")[-2000:]
            )

    os.environ["DOTNET_ROOT"] = install_dir
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    if install_dir not in path_entries:
        os.environ["PATH"] = install_dir + os.pathsep + os.environ.get("PATH", "")
    os.environ.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "1")
    os.environ.setdefault("DOTNET_NOLOGO", "1")
    os.environ.setdefault("NUGET_XMLDOC_MODE", "skip")

    current = await _active_dotnet_major()
    if current is None or current < required:
        raise RuntimeError(
            f"Godot {version} memerlukan .NET SDK {required}+, tetapi SDK aktif tidak sah"
        )
    logs.append(f".NET SDK {current} ready for Godot {version}")
    return current


async def _ensure_godot_debug_keystore(logs):
    configured = os.environ.get("GODOT_ANDROID_KEYSTORE_DEBUG_PATH", "").strip()
    if configured and os.path.exists(configured):
        logs.append("Godot debug keystore: menggunakan konfigurasi environment")
        return configured

    keystore = os.path.join(tempfile.gettempdir(), "earlxz-godot-debug.keystore")
    if os.path.exists(keystore):
        return keystore

    command = (
        f"keytool -genkeypair -keystore {shlex.quote(keystore)} "
        "-storepass android -alias androiddebugkey -keypass android "
        "-keyalg RSA -keysize 2048 -validity 10000 "
        "-dname 'CN=Godot Debug,OU=Earlxz Builder,O=Godot,C=MY'"
    )
    code, output, error = await run_cmd(command, timeout=60)
    if code != 0 or not os.path.exists(keystore):
        raise RuntimeError(
            "Gagal menjana Godot debug keystore: "
            + (error or output or "unknown keytool error")[-1000:]
        )
    logs.append("Godot debug keystore generated")
    return keystore


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


def _android_build_template_present(project_dir):
    build_dir = os.path.join(project_dir, "android", "build")
    if not os.path.isdir(build_dir):
        return False
    markers = (
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "gradlew",
    )
    return any(os.path.exists(os.path.join(build_dir, marker)) for marker in markers)


def _find_android_source_template(version, dotnet=False):
    data_home = os.path.expanduser(os.environ.get("XDG_DATA_HOME", "~/.local/share"))
    version_keys = [_godot_template_version_key(version, dotnet)]
    if dotnet:
        # Some Godot releases share android_source.zip with standard templates.
        version_keys.append(_godot_template_version_key(version, False))
    for version_key in version_keys:
        for base in ("export_templates", "templates"):
            candidate = os.path.join(data_home, "godot", base, version_key, "android_source.zip")
            if os.path.exists(candidate):
                return candidate
    return None


async def _ensure_android_build_template(engine, project_dir, version, major, dotnet, logs):
    if _android_build_template_present(project_dir):
        logs.append("Godot Android Gradle build template already present")
        return

    build_dir = os.path.join(project_dir, "android", "build")
    if os.path.isdir(build_dir) and os.listdir(build_dir):
        raise RuntimeError(
            "Folder android/build wujud tetapi tidak kelihatan seperti Godot Gradle template; "
            "builder tidak akan menindih fail custom"
        )

    if major >= 4:
        command = (
            f"{shlex.quote(engine)} --headless --path {shlex.quote(project_dir)} "
            "--install-android-build-template --quit"
        )
        code, output, error = await run_cmd(command, timeout=300)
        if code == 0 and _android_build_template_present(project_dir):
            logs.append("Godot Android Gradle build template installed by engine")
            return
        logs.append(
            "Godot CLI build-template install tidak lengkap; mencuba android_source.zip fallback"
        )

    source = _find_android_source_template(version, dotnet=dotnet)
    if not source:
        raise RuntimeError(
            "android_source.zip tidak ditemui dalam export templates; Gradle/AAB tidak dapat disediakan"
        )

    os.makedirs(build_dir, exist_ok=True)
    with zipfile.ZipFile(source, "r") as archive:
        archive.extractall(build_dir)
    marker_dir = os.path.dirname(build_dir)
    with open(os.path.join(marker_dir, ".build_version"), "w", encoding="utf-8") as marker:
        marker.write(_godot_template_version_key(version, dotnet) + "\n")
    if not _android_build_template_present(project_dir):
        raise RuntimeError("Godot Gradle template diekstrak tetapi struktur android/build tidak sah")
    logs.append("Godot Android Gradle build template installed from android_source.zip")


def _validate_android_native_extensions(project_dir):
    warnings = []
    ignored = {".git", ".godot", ".mono", "godot_build", "build"}
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [name for name in dirs if name not in ignored]
        for filename in files:
            lower = filename.lower()
            if not lower.endswith((".gdextension", ".gdnlib")):
                continue
            path = os.path.join(root, filename)
            content = _read_text(path)
            if not content:
                continue
            library_lines = []
            in_libraries = False
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    in_libraries = stripped.lower() in ("[libraries]", "[entry]")
                    continue
                if in_libraries and "=" in stripped and not stripped.startswith(("#", ";")):
                    library_lines.append(stripped)

            android_lines = [line for line in library_lines if "android" in line.split("=", 1)[0].lower()]
            relative = os.path.relpath(path, project_dir)
            if not android_lines:
                warnings.append(
                    f"Native extension {relative} tidak mengisytiharkan library Android; eksport mungkin gagal"
                )
                continue
            for line in android_lines:
                value_match = re.search(r'["\x27](res://.*?)["\x27]', line)
                if not value_match:
                    continue
                resource = value_match.group(1)[6:]
                resource_path = os.path.join(project_dir, *resource.split("/"))
                if not os.path.exists(resource_path):
                    warnings.append(
                        f"Native extension {relative}: library Android hilang ({value_match.group(1)})"
                    )
    return warnings


def _validate_android_plugins(project_dir, preset):
    plugin_artifacts = []
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [name for name in dirs if name not in {".git", ".godot", "godot_build", "build"}]
        for filename in files:
            if filename.lower().endswith((".aar", ".gdap")):
                plugin_artifacts.append(os.path.relpath(os.path.join(root, filename), project_dir))
    if plugin_artifacts and not preset.get("gradle"):
        sample = ", ".join(plugin_artifacts[:3])
        return [
            "Android plugin ditemui tetapi preset tidak mengaktifkan Gradle build "
            f"({sample}). Plugin Android moden memerlukan Gradle."
        ]
    return []


async def _configure_android_paths(engine, project_dir, logs, major, debug_keystore=None):
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
    if android_home != "":
        settings.set("export/android/android_sdk_path", android_home)
    if java_home != "":
        settings.set("export/android/java_sdk_path", java_home)
    var debug_keystore = OS.get_environment("EARLXZ_GODOT_DEBUG_KEYSTORE")
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
    if android_home != "":
        settings.set_setting("export/android/android_sdk_path", android_home)
    if java_home != "":
        settings.set_setting("export/android/java_sdk_path", java_home)
    var debug_keystore = OS.get_environment("EARLXZ_GODOT_DEBUG_KEYSTORE")
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
        "EARLXZ_GODOT_SETUP_SENTINEL": sentinel_path,
    }
    if debug_keystore:
        managed_env["EARLXZ_GODOT_DEBUG_KEYSTORE"] = debug_keystore
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
        command = (
            f"{_godot_cli_prefix(engine, major)} --editor "
            f"--path {shlex.quote(project_dir)}"
        )
        code, output, error = await run_cmd(command, timeout=180)
        if code != 0:
            raise RuntimeError(
                "Godot gagal menyimpan tetapan Android SDK/JDK: "
                + (error or output or "unknown Godot editor error")[-2000:]
            )
        if not os.path.exists(sentinel_path):
            raise RuntimeError(
                "Plugin konfigurasi Android Godot tidak dijalankan; tetapan eksport tidak disahkan"
            )
        logs.append("Godot Android SDK/JDK configured tanpa mengubah signing projek")
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

def _godot_output_slug(name):
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "android").lower()).strip("-")
    return slug[:48] or "android"


def _godot_output_path(output_dir, preset, variant, multiple_presets=False):
    preset_slug = _godot_output_slug(preset["name"])
    prefix = f"app-{preset_slug}" if multiple_presets or preset_slug != "android" else "app"
    return os.path.abspath(os.path.join(output_dir, f"{prefix}-{variant}.{preset['format']}"))


def _root_csproj_files(project_dir):
    try:
        return sorted(
            os.path.join(project_dir, name)
            for name in os.listdir(project_dir)
            if name.lower().endswith(".csproj")
        )
    except OSError:
        return []


async def build_godot(project_dir, config):
    logs = []
    if not os.path.exists(os.path.join(project_dir, "project.godot")):
        return {"success": False, "error": "project.godot tidak ditemui", "logs": logs}

    project_kind = _detect_godot_project_kind(project_dir)
    dotnet = project_kind == "dotnet"
    logs.append(f"Godot project kind: {project_kind}")

    requested_preset = (config.get("godot_preset") or os.environ.get("GODOT_PRESET", "")).strip()
    export_mode = (config.get("godot_export_mode") or os.environ.get("GODOT_EXPORT_MODE", "auto")).strip().lower()
    build_all_value = config.get("godot_build_all_presets")
    if build_all_value is None:
        build_all_presets = _env_bool("GODOT_BUILD_ALL_PRESETS", False)
    elif isinstance(build_all_value, str):
        build_all_presets = build_all_value.strip().lower() in ("1", "true", "yes", "on")
    else:
        build_all_presets = bool(build_all_value)

    created_preset = False
    preset_path = os.path.join(project_dir, "export_presets.cfg")
    original_preset = None
    signing_preset_original = None
    generated_release_keystore = None
    debug_env_names = (
        "GODOT_ANDROID_KEYSTORE_DEBUG_PATH",
        "GODOT_ANDROID_KEYSTORE_DEBUG_USER",
        "GODOT_ANDROID_KEYSTORE_DEBUG_PASSWORD",
    )
    release_env_names = (
        "GODOT_ANDROID_KEYSTORE_RELEASE_PATH",
        "GODOT_ANDROID_KEYSTORE_RELEASE_USER",
        "GODOT_ANDROID_KEYSTORE_RELEASE_PASSWORD",
    )
    previous_debug_env = {name: os.environ.get(name) for name in debug_env_names}
    previous_release_env = {name: os.environ.get(name) for name in release_env_names}

    try:
        engine, version, major = await _setup_godot(project_dir, logs, dotnet=dotnet)
        parsed_version = _version_tuple(version) or (major, 0, 0)

        if dotnet:
            if major == 4 and parsed_version[1] < 2:
                raise RuntimeError(
                    f"Godot {version} C# tidak menyokong eksport Android; gunakan Godot 4.2+ atau Godot 3.x Mono"
                )
            csproj_files = _root_csproj_files(project_dir)
            if not csproj_files:
                raise RuntimeError(
                    "Projek Godot C# dikesan tetapi fail .csproj di root projek tidak ditemui. "
                    "Sertakan fail .csproj/.sln dalam ZIP."
                )
            await _setup_dotnet_sdk(version, logs)

        await _setup_godot_android_requirements(version, logs)
        debug_keystore = await _ensure_godot_debug_keystore(logs)
        os.environ["GODOT_ANDROID_KEYSTORE_DEBUG_PATH"] = debug_keystore
        os.environ["GODOT_ANDROID_KEYSTORE_DEBUG_USER"] = "androiddebugkey"
        os.environ["GODOT_ANDROID_KEYSTORE_DEBUG_PASSWORD"] = "android"
        await _configure_android_paths(
            engine,
            project_dir,
            logs,
            major,
            debug_keystore=debug_keystore,
        )
        signing_info = _prepare_release_signing_environment(project_dir, logs)
        if isinstance(signing_info, dict) and signing_info.get("generated"):
            generated_release_keystore = signing_info.get("path")

        for warning in _validate_android_native_extensions(project_dir):
            logs.append("WARNING: " + warning)

        presets = _list_android_presets(project_dir)
        if not presets:
            if os.path.exists(preset_path):
                with open(preset_path, "rb") as source:
                    original_preset = source.read()
            _write_fallback_preset(project_dir, major)
            created_preset = True
            logs.append("Android export preset generated temporarily")
            presets = _list_android_presets(project_dir)

        selected_presets = _select_android_presets(
            presets,
            requested=requested_preset,
            build_all=build_all_presets,
        )
        if not selected_presets:
            raise RuntimeError("Tiada Android export preset Godot yang boleh digunakan")

        if major == 3:
            signing_preset_original = _inject_godot3_release_signing(
                project_dir, selected_presets, logs
            )

        logs.append(
            "Godot Android preset selected: "
            + ", ".join(preset["name"] for preset in selected_presets)
        )
        logs.append(f"Godot export mode: {export_mode}")

        output_dir = os.path.join(project_dir, "godot_build")
        os.makedirs(output_dir, exist_ok=True)
        files = []
        release_failures = []
        multiple_presets = len(selected_presets) > 1

        for preset in selected_presets:
            for warning in _validate_android_plugins(project_dir, preset):
                logs.append("WARNING: " + warning)

            if preset.get("gradle"):
                await _ensure_android_build_template(
                    engine,
                    project_dir,
                    version,
                    major,
                    dotnet,
                    logs,
                )

            variants = _export_variants(export_mode, preset.get("has_release_signing", False))
            preset_file_count_before = len(files)

            for variant in variants:
                if variant == "release" and not preset.get("has_release_signing", False):
                    details = (
                        f"Preset '{preset['name']}' tidak mempunyai release keystore/user/password. "
                        "Konfigurasikan signing release dalam export_presets.cfg atau environment Godot."
                    )
                    if len(files) > preset_file_count_before:
                        logs.append("Godot release export skipped: release signing tidak dikonfigurasi")
                        release_failures.append(f"[{preset['name']}] {details}")
                        continue
                    return {
                        "success": False,
                        "error": f"Godot Android release export gagal: {details}",
                        "logs": logs,
                    }

                output_path = _godot_output_path(
                    output_dir,
                    preset,
                    variant,
                    multiple_presets=multiple_presets,
                )
                export_flag = _godot_export_flag(major, variant)
                command = (
                    f"{_godot_cli_prefix(engine, major)} --path {shlex.quote(project_dir)} "
                    f"{export_flag} {shlex.quote(preset['name'])} {shlex.quote(output_path)}"
                )
                code, output, error = await run_cmd(command, timeout=1800)
                success = code == 0 and os.path.exists(output_path)
                logs.append(
                    f"Godot {version} {preset['name']} {variant} export: {'OK' if success else 'FAIL'}"
                )

                if success:
                    files.append(output_path)
                    logs.append(f"Godot output: {os.path.basename(output_path)}")
                    continue

                details = (error or output or "Unknown Godot export error")[-8000:]
                if variant == "release" and len(files) > preset_file_count_before:
                    release_failures.append(
                        f"[{preset['name']}] Godot Android release export gagal\n{details}"
                    )
                    continue
                return {
                    "success": False,
                    "error": (
                        f"Godot Android {variant} export gagal untuk preset '{preset['name']}'\n{details}"
                    ),
                    "logs": logs,
                }

        if not files:
            return {
                "success": False,
                "error": "Godot export tamat tanpa menghasilkan APK/AAB",
                "logs": logs,
            }
        return {
            "success": True,
            "files": files,
            "logs": logs,
            "release_failures": release_failures,
        }
    except Exception as error:
        logger.exception("Godot build setup failed")
        return {"success": False, "error": f"Godot build gagal: {error}", "logs": logs}
    finally:
        for name, previous_value in previous_debug_env.items():
            if previous_value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous_value
        for name, previous_value in previous_release_env.items():
            if previous_value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous_value
        if signing_preset_original is not None:
            try:
                with open(preset_path, "wb") as output:
                    output.write(signing_preset_original)
            except OSError:
                pass
        if created_preset:
            try:
                if original_preset is None:
                    os.remove(preset_path)
                else:
                    with open(preset_path, "wb") as output:
                        output.write(original_preset)
            except OSError:
                pass
        if generated_release_keystore:
            try:
                os.remove(generated_release_keystore)
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



PROJECT_TYPE_DISPLAY = {
    "native": "Android Native",
    "flutter": "Flutter",
    "smali": "Smali (APKTool)",
    "react_native": "React Native",
    "cordova": "Cordova",
    "ionic": "Ionic",
    "capacitor": "Capacitor",
    "godot": "Godot",
}


def _project_name_hint(project_dir, project_type):
    if project_type == "godot":
        content = _read_text(os.path.join(project_dir, "project.godot"))
        match = re.search(r'(?m)^config/name\s*=\s*["\x27](.*?)["\x27]\s*$', content)
        if match:
            return match.group(1).strip()

    if project_type == "flutter":
        content = _read_text(os.path.join(project_dir, "pubspec.yaml"))
        match = re.search(r'(?m)^name\s*:\s*["\x27]?([^\s#"\x27]+)', content)
        if match:
            return match.group(1).strip()

    if project_type in ("react_native", "cordova", "ionic", "capacitor"):
        try:
            with open(os.path.join(project_dir, "package.json"), "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            name = str(data.get("name", "")).strip()
            if name:
                return name
        except Exception:
            pass

    if project_type == "native":
        for filename in ("settings.gradle", "settings.gradle.kts"):
            content = _read_text(os.path.join(project_dir, filename))
            match = re.search(r'rootProject\.name\s*=\s*["\x27]([^"\x27]+)', content)
            if match:
                return match.group(1).strip()

    return ""


def make_project_detection_message(
    project_dir,
    build_dir,
    project_type,
    target_file,
    java_version=None,
    flutter_version=None,
):
    """Create the early project-information notification sent before compilation."""
    display = PROJECT_TYPE_DISPLAY.get(project_type, project_type or "Unknown")
    relative = os.path.relpath(project_dir, build_dir)
    root_label = "/" if relative == "." else relative.replace(os.sep, "/")
    name = _project_name_hint(project_dir, project_type)

    lines = [
        "<blockquote><b>PROJECT DETECTED</b>",
        "",
        "━━━━━━━━━━━━━━━━━━",
        f"◆ File: <code>{html.escape(str(target_file))}</code>",
        f"◆ Framework: <b>{html.escape(str(display))}</b>",
        f"◆ Root: <code>{html.escape(root_label)}</code>",
    ]
    if name:
        lines.append(f"◆ Project: <code>{html.escape(name)}</code>")

    if project_type == "godot":
        version = _detect_version_hint(project_dir)
        kind = "C# / .NET" if _detect_godot_project_kind(project_dir) == "dotnet" else "GDScript / Standard"
        lines.append(f"◆ Godot: <code>{html.escape(version)}</code>")
        lines.append(f"◆ Runtime: <code>{kind}</code>")
    elif project_type == "flutter":
        lines.append(f"◆ Flutter: <code>{html.escape(str(flutter_version or 'stable'))}</code>")
        if java_version:
            lines.append(f"◆ Java: <code>{html.escape(str(java_version))}</code>")
    elif java_version and project_type != "smali":
        lines.append(f"◆ Java: <code>{html.escape(str(java_version))}</code>")

    lines.extend([
        "━━━━━━━━━━━━━━━━━━",
        "",
        "🚀 Detection selesai. Build diteruskan...",
        "</blockquote>",
    ])
    return "\n".join(lines)


def make_project_not_detected_message(target_file):
    return "\n".join([
        "<blockquote><b>PROJECT NOT DETECTED</b>",
        "",
        "━━━━━━━━━━━━━━━━━━",
        f"◆ File: <code>{html.escape(str(target_file))}</code>",
        "◆ Status: <b>Jenis projek tidak dapat dikesan</b>",
        "━━━━━━━━━━━━━━━━━━",
        "",
        "Framework yang disokong:",
        "• Android Native",
        "• Flutter",
        "• Smali (APKTool)",
        "• React Native",
        "• Cordova",
        "• Ionic",
        "• Capacitor",
        "• Godot",
        "",
        "Build dihentikan sebelum proses compile.",
        "</blockquote>",
    ])


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
            release_failures = result.get("release_failures") or []
            if release_failures:
                release_log = (
                    "=== RELEASE BUILD PARTIAL FAILURE ===\n\n"
                    "Debug output berjaya dan disertakan dalam ZIP ini.\n"
                    "Release output yang gagal tidak dianggap sebagai kegagalan keseluruhan build.\n\n"
                    + "\n\n".join(release_failures)
                    + "\n\n=== BUILD STEPS ===\n\n"
                    + "\n".join(result.get("logs", []))
                )
                archive.writestr("RELEASE_BUILD_ERROR.txt", release_log)
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
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
    }
    host = (urlparse(download_url).hostname or "").lower()
    methods = ("POST",) if host == "temp.sh" else ("GET", "POST")
    last_error = None

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for method in methods:
            try:
                async with session.request(method, download_url, allow_redirects=True) as response:
                    if response.status != 200:
                        raise RuntimeError(f"Download gagal: HTTP {response.status}")
                    with open(destination, "wb") as f:
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            f.write(chunk)
                if os.path.exists(destination) and os.path.getsize(destination) > 0:
                    return
                raise RuntimeError("Fail pautan kosong atau tidak dapat dimuat turun")
            except Exception as error:
                last_error = error
                try:
                    if os.path.exists(destination):
                        os.remove(destination)
                except OSError:
                    pass

    raise last_error or RuntimeError("Fail pautan kosong atau tidak dapat dimuat turun")


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

    source_mode = os.getenv("SOURCE_MODE", "repo").strip().lower() or "repo"
    submitted_file = os.path.join("temp", target_file)
    source_zip = submitted_file
    downloaded_zip = None
    direct_source_dir = None
    build_dir = "build_area"

    if source_mode == "telegram":
        direct_source_dir = tempfile.mkdtemp(prefix="builder_source_")
        downloaded_zip = os.path.join(direct_source_dir, target_file)
        try:
            logger.info("Downloading project directly from Telegram on GitHub runner")
            await download_telegram_document_reference(
                bot_token=bot_token,
                document_id=os.getenv("TELEGRAM_DOCUMENT_ID", ""),
                access_hash=os.getenv("TELEGRAM_ACCESS_HASH", ""),
                file_reference_b64=os.getenv("TELEGRAM_FILE_REFERENCE", ""),
                dc_id=os.getenv("TELEGRAM_DC_ID", ""),
                file_size=os.getenv("TELEGRAM_FILE_SIZE", ""),
                destination=downloaded_zip,
            )
            source_zip = downloaded_zip
            logger.info("Direct Telegram project download completed")
        except Exception as error:
            result = {
                "success": False,
                "error": f"Gagal memuat turun projek terus daripada Telegram: {error}",
                "logs": [],
            }
            await send_failure(
                bot_token, chat_id, user_display, "unknown", result, build_dir, target_file
            )
            if direct_source_dir:
                shutil.rmtree(direct_source_dir, ignore_errors=True)
            return 1
    else:
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
            try:
                await send_telegram_notification(
                    bot_token, chat_id, make_project_not_detected_message(target_file)
                )
            except Exception:
                logger.exception("Gagal menghantar status project-not-detected")
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
            try:
                await send_telegram_notification(
                    bot_token,
                    chat_id,
                    make_project_detection_message(
                        project_dir,
                        build_dir,
                        final_type,
                        target_file,
                        java_version=java_version,
                        flutter_version=flutter_version,
                    ),
                )
            except Exception:
                logger.exception("Gagal menghantar maklumat projek sebelum build")

            if final_type == "godot":
                result = await build_godot(project_dir, config)
            else:
                result = await build_project(project_dir, {"type": final_type, "config": config})
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
            release_failures = result.get("release_failures") or []
            if release_failures:
                output_note = (
                    "⚠️ Release build tidak lengkap/gagal. Debug APK yang berjaya tetap disertakan.\n"
                    "Semak RELEASE_BUILD_ERROR.txt dalam ZIP untuk log release.\n"
                    "Signing output tidak diubah oleh builder; ia kekal mengikut konfigurasi projek."
                )
            else:
                output_note = (
                    "ℹ️ Signing output tidak diubah oleh builder; "
                    "ia kekal mengikut konfigurasi projek."
                )

            user_caption = "<blockquote>" + (
                "<b>Build Successful!</b>\n\n"
                f"Project: {output_zip}\n"
                f"Type: {final_type.upper()}\n\n"
                f"{output_note}\n\n"
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
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
    if direct_source_dir:
        shutil.rmtree(direct_source_dir, ignore_errors=True)

    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
