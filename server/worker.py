import os
import sys
import asyncio
import zipfile
import shutil
import logging
import re
import json
import aiohttp
from builder import build_project
from upload_handler import upload_gofile, send_telegram_notification, send_telegram_document

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def get_flutter_version_from_pubspec(project_dir):
    """Detect minimum Flutter version dari pubspec.yaml.
    Return versi string (e.g. '3.13.0') atau 'stable' kalau tak jumpa."""
    pubspec_path = os.path.join(project_dir, "pubspec.yaml")
    if not os.path.exists(pubspec_path):
        return "stable"
    try:
        with open(pubspec_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        # Cari: flutter: ">=3.x.x" dalam environment block
        m = re.search(r'flutter\s*:\s*["\']?>=\s*([0-9]+\.[0-9]+\.[0-9]+)', content)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "stable"

def is_old_flutter_style(project_dir):
    """Detect sama ada projek guna cara lama (apply from:) atau baru (plugins{} block)."""
    settings_path = os.path.join(project_dir, "android", "settings.gradle")
    if not os.path.exists(settings_path):
        return False
    try:
        with open(settings_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return "app_plugin_loader.gradle" in content
    except Exception:
        return False

def get_java_version_for_project(project_dir):
    """
    Detect versi Java yang diperlukan untuk projek ini.

    Scan (ikut keutamaan):
    1. Versi Java yang ditulis dalam kod projek (compileOptions, jvmTarget, JavaVersion, toolchain)
    2. Versi minimum berdasarkan AGP yang digunakan
    3. Default: 17 (minimum moden)

    Ambil nilai TERTINGGI antara semua sumber — tak pernah turunkan.
    """
    skip_dirs = {"build", ".gradle", "node_modules", ".dart_tool"}
    candidates = []

    def extract_java_vers(content):
        found = []
        for m in re.finditer(r'\bJavaVersion\.VERSION_(?:1_(\d)|(\d{1,2}))\b', content):
            found.append(int(m.group(1) or m.group(2)))
        for m in re.finditer(r'jvmTarget\s*=\s*["\x27](1\.(\d)|(\d{1,2}))["\x27]', content):
            found.append(int(m.group(2) or m.group(3)))
        for m in re.finditer(r'JavaLanguageVersion\.of\((\d+)\)', content):
            found.append(int(m.group(1)))
        for m in re.finditer(r'(?:source|target)Compatibility\s*=\s*(\d+)\b', content):
            found.append(int(m.group(1)))
        return found

    agp_ver = None

    for root, dirs, fnames in os.walk(project_dir):
        dirs[:] = [d for d in dirs if os.path.basename(d) not in skip_dirs]
        for fn in fnames:
            if fn not in ("build.gradle", "build.gradle.kts",
                          "settings.gradle", "settings.gradle.kts"):
                continue
            try:
                with open(os.path.join(root, fn), "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                candidates.extend(extract_java_vers(content))
                if agp_ver is None:
                    m = re.search(r'com\.android\.tools\.build:gradle:([0-9.]+)', content)
                    if m:
                        agp_ver = m.group(1)
                    m2 = re.search(
                        r'id\s*\(?\s*["\x27]com\.android\.(?:application|library)["\x27]\s*\)?\s+version\s+["\x27]([0-9.]+)["\x27]',
                        content
                    )
                    if m2:
                        agp_ver = m2.group(1)
            except Exception:
                pass

    agp_min = 17
    if agp_ver:
        try:
            major = int(agp_ver.split(".")[0])
            if major >= 8:   agp_min = 17
            elif major >= 7: agp_min = 11
            else:            agp_min = 8
        except Exception:
            pass

    result = max(candidates + [agp_min])
    return str(max(result, 17))  # floor global 17


def detect_project_type(project_dir, hint=""):
    """Auto-detect jenis project dari struktur fail.

    Priority:
    1. hint dari user (Telegram input) — kalau jelas
    2. Detect dari fail dalam project_dir
    """

    hint = hint.lower().strip()

    # ── Mapping hint dari user ──────────────────────────────────────
    if any(x in hint for x in ["react_native", "react native", "reactnative", "rn"]):
        return "react_native"
    if any(x in hint for x in ["capacitor"]):
        return "capacitor"
    if any(x in hint for x in ["ionic"]):
        return "ionic"
    if any(x in hint for x in ["cordova"]):
        return "cordova"
    if "flutter" in hint:
        return "flutter"
    if "smali" in hint:
        return "smali"
    if any(x in hint for x in ["native", "gradle"]):
        return "native"

    # ── Auto-detect dari struktur fail ──────────────────────────────

    # Smali: ada apktool.yml
    if os.path.exists(os.path.join(project_dir, "apktool.yml")):
        return "smali"

    # Flutter: ada pubspec.yaml
    if os.path.exists(os.path.join(project_dir, "pubspec.yaml")):
        return "flutter"

    # Node-based projects — baca package.json
    pkg_path = os.path.join(project_dir, "package.json")
    if os.path.exists(pkg_path):
        try:
            with open(pkg_path, "r", encoding="utf-8", errors="replace") as f:
                pkg = json.load(f)
            deps = {}
            deps.update(pkg.get("dependencies", {}))
            deps.update(pkg.get("devDependencies", {}))
            dep_keys = " ".join(deps.keys()).lower()

            # Capacitor standalone (tanpa Ionic)
            has_capacitor = "@capacitor/core" in dep_keys or "@capacitor/android" in dep_keys
            has_ionic = "@ionic" in dep_keys or "ionic" in pkg.get("name", "").lower()

            if has_ionic and has_capacitor:
                return "ionic"      # Ionic + Capacitor
            if has_ionic:
                return "ionic"      # Ionic + Cordova
            if has_capacitor:
                return "capacitor"  # Capacitor standalone

            # React Native
            if "react-native" in dep_keys:
                return "react_native"

            # Cordova — ada config.xml
            if os.path.exists(os.path.join(project_dir, "config.xml")):
                return "cordova"

        except Exception:
            pass

    # Capacitor config tanpa package.json yang jelas
    if (os.path.exists(os.path.join(project_dir, "capacitor.config.json")) or
            os.path.exists(os.path.join(project_dir, "capacitor.config.ts"))):
        return "capacitor"

    # Cordova: ada config.xml sahaja
    if os.path.exists(os.path.join(project_dir, "config.xml")):
        return "cordova"

    # Native Android: ada settings.gradle atau build.gradle
    for f in ("settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts"):
        if os.path.exists(os.path.join(project_dir, f)):
            return "native"

    return "native"  # fallback



def package_result(project_dir, result, target_file):
    if result.get("success"):
        # Kalau target_file ialah .txt (laluan fail besar), hasil mesti .zip — bukan .txt
        base_name, ext = os.path.splitext(target_file)
        hasil_zip = f"{base_name}.zip" if ext.lower() == ".txt" else target_file
    else:
        hasil_zip = f"{os.path.splitext(target_file)[0]}_error.zip"

    with zipfile.ZipFile(hasil_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        if result.get("success"):
            for fp in result.get("files", []):
                if os.path.exists(fp):
                    zf.write(fp, os.path.basename(fp))
        else:
            err_log = os.path.join(project_dir, "build_error.log")
            with open(err_log, "w", encoding="utf-8") as f:
                f.write("=== BUILD ERROR ===\n\n")
                f.write(result.get("error", "Unknown error") + "\n\n")
                if "logs" in result:
                    f.write("=== BUILD LOGS ===\n\n")
                    f.write("\n".join(result["logs"]))
            zf.write(err_log, "ERROR_LOG.txt")
    return hasil_zip

async def main():
    bot_token = os.getenv("BOT_TOKEN")
    target_file = os.getenv("TARGET_FILE", "source.zip")
    chat_id = os.getenv("CHAT_ID")
    channel_id = os.getenv("CHANNEL_ID")
    p_type = os.getenv("PROJECT_TYPE", "Unknown").lower()
    user_display = os.getenv("USER_DISPLAY", "Unknown")
    in_progress = os.getenv("IN_PROGRESS", "1")

    
    source_zip = f"temp/{target_file}"
    build_dir = "build_area"

    if not os.path.exists(source_zip):
        logger.error("Source zip not found")
        return

    # Kalau ia .txt → ia adalah link fail besar, download dulu
    if target_file.endswith('.txt'):
        with open(source_zip, 'r') as f:
            download_url = f.read().strip()
        real_zip = source_zip.replace('.txt', '.zip')
        logger.info(f"Fail besar, download dari: {download_url}")
        try:
            async with aiohttp.ClientSession() as session:
                is_tempsh = "temp.sh" in download_url
                if is_tempsh:
                    req = session.post(download_url, timeout=aiohttp.ClientTimeout(total=600))
                else:
                    req = session.get(download_url, timeout=aiohttp.ClientTimeout(total=600))
                async with req as r:
                    if r.status != 200:
                        logger.error(f"Download gagal: HTTP {r.status}")
                        return
                    with open(real_zip, 'wb') as f:
                        async for chunk in r.content.iter_chunked(8192):
                            f.write(chunk)
        except Exception as dl_err:
            logger.error(f"Download error: {dl_err}")
            return
        source_zip = real_zip
        logger.info(f"Download selesai: {real_zip}")

    os.makedirs(build_dir, exist_ok=True)
    with zipfile.ZipFile(source_zip, "r") as zf: zf.extractall(build_dir)
    
    # Deteksi project_dir (handle nested folder)
    project_dir = build_dir
    # Prioritaskan folder yang ada markers utama
    main_markers = [
        "settings.gradle", "settings.gradle.kts",  # Native / React Native / Capacitor
        "pubspec.yaml",                              # Flutter
        "apktool.yml",                               # Smali
        "package.json",                              # React Native / Ionic / Cordova / Capacitor
        "config.xml",                                # Cordova / Ionic Cordova
        "capacitor.config.json",                     # Capacitor
        "capacitor.config.ts",                       # Capacitor
    ]
    found = False
    for root, dirs, files in os.walk(build_dir):
        if any(m in files for m in main_markers):
            project_dir = root
            found = True
            break
    if not found:
        # Fallback to any folder with build.gradle
        for root, dirs, files in os.walk(build_dir):
            if "build.gradle" in files or "build.gradle.kts" in files:
                project_dir = root
                break

    # Deteksi info proyek
    final_type = detect_project_type(project_dir, hint=p_type)
    logger.info(f"Project type detected: {final_type} (hint: '{p_type}')")

    java_ver = get_java_version_for_project(project_dir)

    # Node-based projects guna Java 17 secara default
    if final_type in ("react_native", "ionic", "cordova", "capacitor"):
        java_ver = "17"

    # Flutter: detect versi dan style projek
    flutter_version = "stable"
    old_flutter_style = False
    if final_type == "flutter":
        flutter_version = get_flutter_version_from_pubspec(project_dir)
        old_flutter_style = is_old_flutter_style(project_dir)
        logger.info(f"Flutter version required: {flutter_version}, old style: {old_flutter_style}")

    info = {"type": final_type, "config": {
        "java_version": java_ver,
        "flutter_version": flutter_version,
        "old_flutter_style": old_flutter_style,
    }}
    
    logger.info(f"Targeting Java {java_ver} for {final_type} project")
    result = await build_project(project_dir, info)
    if not result["success"]: logger.error(f"Build Failed: {result.get('error')}")

    hasil_zip = package_result(project_dir, result, target_file)
    
    if result["success"]:
        # FORMAT USER
        user_caption = "<blockquote>" + (
            "<b>Build Successful!</b>\n\n"
            f"Project: {hasil_zip}\n"
            f"Type: {final_type.upper()}\n\n"
            "⚠️ Release APK/AAB is unsigned.\n\n"
            "BUILD BY @Earlxz"
        ) + "</blockquote>"

        # FORMAT CHANNEL
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
        
        # Hantar ke User (Cuba Document dulu)
        ok_user = await send_telegram_document(bot_token, chat_id, hasil_zip, user_caption)
        if not ok_user:
            # Hanya hantar link jika fail > 2GB (2147483648 bytes)
            file_size = os.path.getsize(hasil_zip)
            if file_size > 2147483648:
                link = await upload_gofile(hasil_zip)
                user_msg = user_caption + f"\n\n🔗 Link: {link}"
            else:
                user_msg = user_caption + "\n\n❌ Gagal menghantar fail. Saiz fail mungkin terlalu besar atau ralat teknikal."
            
            await send_telegram_notification(bot_token, chat_id, user_msg)
        
        # Hantar ke Channel (Hanya Notifikasi Teks)
        if channel_id and channel_id.strip():
            await send_telegram_notification(bot_token, channel_id, channel_caption)
            
    else:
        # FORMAT GAGAL (USER SAHAJA)
        user_fail_caption = "<blockquote>" + (
            "<b>BUILD FAILED</b>\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"◆ User: {user_display}\n"
            f"◆ Type: {final_type.upper()}\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Sila semak log ralat yang dilampirkan.\n"
            "BUILD BY @Earlxz"
        ) + "</blockquote>"
        # Hantar fail log ke user
        ok = await send_telegram_document(bot_token, chat_id, hasil_zip, user_fail_caption)
        if not ok:
            # Tiada link untuk log ralat (AMARAN KERAS)
            await send_telegram_notification(bot_token, chat_id, user_fail_caption)
            
        # CHANNEL SILENT JIKA GAGAL (TIADA NOTIF DI SINI)
        
    shutil.rmtree(build_dir, ignore_errors=True)
    if os.path.exists(source_zip): os.remove(source_zip)

if __name__ == "__main__":
    asyncio.run(main())
