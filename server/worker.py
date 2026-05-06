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

def get_java_version_from_agp(project_dir):
    """Mendeteksi versi Java yang dibutuhkan berdasarkan AGP di build.gradle."""
    for root, _, fnames in os.walk(project_dir):
        for fn in fnames:
            if fn == "build.gradle" or fn == "build.gradle.kts":
                try:
                    with open(os.path.join(root, fn), "r") as f:
                        content = f.read()
                    m = re.search(r"com\.android\.tools\.build:gradle:(\d+)\.", content)
                    if m:
                        major = int(m.group(1))
                        if major >= 8: return "17"
                        if major >= 7: return "11"
                        return "8"
                except: pass
    return "17"


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
    hasil_zip = target_file if result.get("success") else f"{os.path.splitext(target_file)[0]}_error.zip"

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

    java_ver = get_java_version_from_agp(project_dir)

    # Node-based projects guna Java 17 secara default
    if final_type in ("react_native", "ionic", "cordova", "capacitor"):
        java_ver = "17"

    info = {"type": final_type, "config": {"java_version": java_ver}}
    
    logger.info(f"Targeting Java {java_ver} for {final_type} project")
    result = await build_project(project_dir, info)
    if not result["success"]: logger.error(f"Build Failed: {result.get('error')}")

    hasil_zip = package_result(project_dir, result, target_file)
    
    if result["success"]:
        # FORMAT USER
        user_caption = "<blockquote>" + (
            "<b>Build Successful!</b>\n\n"
            f"Project: {target_file}\n"
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
