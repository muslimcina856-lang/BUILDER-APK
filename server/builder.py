import asyncio
import os
import re
import shutil
import logging
import zipfile
import aiohttp



logger = logging.getLogger(__name__)


async def run_cmd(cmd, cwd=None, timeout=1200):
    env = os.environ.copy()
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd, env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", "Build timeout exceeded"
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def setup_java(version):
    """
    Setup Java untuk mana-mana versi — 8 hingga 26+.

    Strategi download (ikut priority):
    1. Guna yang dah ada dalam sistem
    2. apt-get (Java 8, 11, 17, 21, 25 — bergantung pada Ubuntu version)
    3. Eclipse Temurin API (api.adoptium.net) — cover semua versi termasuk 21-26
    4. Fallback ke versi tertinggi yang ada dalam sistem

    Temurin API URL format:
      https://api.adoptium.net/v3/binary/latest/{version}/ga/linux/x64/jdk/hotspot/normal/eclipse
    """
    version = str(version)

    def find_java_home(ver):
        """Scan /usr/lib/jvm/ untuk cari path Java version yang diminta."""
        candidates = [
            f"/usr/lib/jvm/java-{ver}-openjdk-amd64",
            f"/usr/lib/jvm/java-{ver}-openjdk",
            f"/usr/lib/jvm/temurin-{ver}",
            f"/usr/lib/jvm/jdk-{ver}",
            f"/usr/lib/jvm/adoptopenjdk-{ver}-hotspot-amd64",
        ]
        for c in candidates:
            if os.path.isdir(c) and os.path.exists(os.path.join(c, "bin", "java")):
                return c
        jvm_base = "/usr/lib/jvm"
        if os.path.isdir(jvm_base):
            for entry in sorted(os.listdir(jvm_base), reverse=True):
                if str(ver) in entry and os.path.exists(os.path.join(jvm_base, entry, "bin", "java")):
                    return os.path.join(jvm_base, entry)
        # Scan /opt/jdk/ — lokasi install manual/temurin tarball
        for base in ["/opt/jdk", "/opt/java"]:
            if os.path.isdir(base):
                for entry in sorted(os.listdir(base), reverse=True):
                    if str(ver) in entry and os.path.exists(os.path.join(base, entry, "bin", "java")):
                        return os.path.join(base, entry)
        return None

    def set_java_env(home):
        os.environ["JAVA_HOME"] = home
        os.environ["PATH"] = f"{home}/bin:{os.environ['PATH']}"
        logger.info(f"Java {version} ready at {home}")

    # ── 1. Guna yang dah ada ────────────────────────────────────────────────────
    home = find_java_home(version)
    if home:
        set_java_env(home)
        return True

    # ── 2. Cuba apt-get ─────────────────────────────────────────────────────────
    logger.info(f"Java {version} not found, trying apt-get...")
    code, _, _ = await run_cmd(
        f"sudo apt-get update -qq 2>/dev/null && "
        f"sudo apt-get install -y -qq openjdk-{version}-jdk 2>/dev/null || "
        f"(sudo add-apt-repository -y ppa:openjdk-r/ppa 2>/dev/null && "
        f"sudo apt-get update -qq 2>/dev/null && "
        f"sudo apt-get install -y -qq openjdk-{version}-jdk 2>/dev/null)",
        timeout=300,
    )
    home = find_java_home(version)
    if home:
        set_java_env(home)
        return True

    # ── 3. Eclipse Temurin API — cover Java 21, 22, 23, 24, 25, 26 ─────────────
    # LTS versions: 21, 25 → guna "ga" (general availability)
    # Non-LTS: 22, 23, 24, 26 → guna "ga" jugak kalau ada, otherwise latest EA
    logger.info(f"Trying Temurin API for Java {version}...")

    # Temurin hanya ada LTS secara rasmi: 8, 11, 17, 21, 25
    # Non-LTS (22, 23, 24, 26) ada tapi mungkin short-lived
    temurin_url = (
        f"https://api.adoptium.net/v3/binary/latest/{version}/ga/"
        f"linux/x64/jdk/hotspot/normal/eclipse"
    )
    install_dir = f"/opt/jdk/jdk-{version}"
    os.makedirs(install_dir, exist_ok=True)

    code, _, err = await run_cmd(
        f"curl -fsSL --retry 3 '{temurin_url}' -o /tmp/jdk-{version}.tar.gz && "
        f"tar -xzf /tmp/jdk-{version}.tar.gz -C '{install_dir}' --strip-components=1 && "
        f"rm -f /tmp/jdk-{version}.tar.gz",
        timeout=300,
    )

    if code == 0:
        # Verify binary ada
        java_bin = os.path.join(install_dir, "bin", "java")
        if os.path.exists(java_bin):
            # Register dengan update-alternatives supaya sistem kenal
            await run_cmd(
                f"sudo update-alternatives --install /usr/bin/java java '{java_bin}' 100 2>/dev/null || true",
                timeout=30,
            )
            set_java_env(install_dir)
            logger.info(f"Java {version} installed via Temurin at {install_dir}")
            return True

    logger.warning(f"Temurin download failed for Java {version}: {(err or '')[:100]}")

    # ── 4. Fallback — guna versi tertinggi yang ada ──────────────────────────────
    logger.warning(f"Java {version} install failed, scanning for best available...")
    best_home = None
    best_ver = 0
    jvm_base = "/usr/lib/jvm"
    if os.path.isdir(jvm_base):
        for entry in os.listdir(jvm_base):
            java_bin = os.path.join(jvm_base, entry, "bin", "java")
            if not os.path.exists(java_bin):
                continue
            # Extract version number dari nama folder
            m = re.search(r'(?:java|jdk|temurin)[_\-](\d+)', entry)
            if m:
                v = int(m.group(1))
                if v >= best_ver:
                    best_ver = v
                    best_home = os.path.join(jvm_base, entry)

    if best_home:
        logger.warning(f"Using Java {best_ver} as fallback (requested {version})")
        set_java_env(best_home)
        return True

    logger.error(f"No suitable Java found for version {version}")
    return False


def _agp_to_min_java(agp_ver: str) -> int:
    """
    Map AGP version → minimum Java version yang diperlukan.

    Berdasarkan official Android/Gradle documentation:
    - AGP 9.x → Java 21 minimum (Gradle 9.x perlukan Java 17+, AGP 9.x require Java 21)
    - AGP 8.x → Java 17 minimum
    - AGP 7.x → Java 11 minimum
    - AGP < 7.x → Java 8 minimum

    Note: Gradle 9.0+ sendiri perlukan JVM 17+ untuk run daemon,
    tapi AGP 9.x menaikkan keperluan ke Java 21.
    """
    try:
        major = int(agp_ver.split(".")[0])
    except (ValueError, IndexError):
        return 17
    if major >= 9:
        return 21   # AGP 9.x + Gradle 9.x ecosystem
    elif major >= 8:
        return 17   # AGP 8.x standard
    elif major >= 7:
        return 11   # AGP 7.x
    else:
        return 8    # AGP < 7.x (projek lama)


async def setup_android_sdk(compile_sdk=None, build_tools=None):
    ah = os.environ.get("ANDROID_HOME", "/usr/local/lib/android/sdk")
    sm = f"{ah}/cmdline-tools/latest/bin/sdkmanager"
    if not os.path.exists(sm):
        sm = f"{ah}/tools/bin/sdkmanager"
    cmds = ['echo "y" | ' + sm + " --licenses 2>/dev/null || true"]
    if compile_sdk:
        cmds.append(f'echo "y" | {sm} "platforms;android-{compile_sdk}"')
    if build_tools:
        cmds.append(f'echo "y" | {sm} "build-tools;{build_tools}"')
    for c in cmds:
        await run_cmd(c, timeout=300)


async def setup_flutter(version):
    fdir = "/tmp/flutter_sdk"
    if os.path.exists(fdir):
        shutil.rmtree(fdir, ignore_errors=True)
    branch = "stable" if (version == "stable" or not re.match(r"\d+\.\d+\.\d+", version)) else version
    code, _, err = await run_cmd(
        f"git clone https://github.com/flutter/flutter.git -b {branch} --depth 1 {fdir}",
        timeout=300,
    )
    if code != 0:
        logger.error(f"Flutter clone failed: {err}")
        return False
    os.environ["PATH"] = f"{fdir}/bin:{os.environ['PATH']}"
    await run_cmd("flutter precache --android", timeout=300)
    await run_cmd("yes | flutter doctor --android-licenses 2>/dev/null || true", timeout=120)
    return True


async def setup_node(version="20"):
    code, out, _ = await run_cmd("node --version")
    if code == 0:
        try:
            current = int(out.strip().lstrip("v").split(".")[0])
            if current >= int(version):
                logger.info(f"Node.js {out.strip()} already available")
                return True
        except Exception:
            pass

    nvm_dir = os.path.expanduser("~/.nvm")
    if not os.path.exists(nvm_dir):
        await run_cmd(
            "curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash",
            timeout=120,
        )

    code, _, _ = await run_cmd(
        f'export NVM_DIR="$HOME/.nvm" && [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" '
        f'&& nvm install {version} && nvm use {version} && '
        f'echo "export NVM_DIR=$HOME/.nvm" >> ~/.bashrc && '
        f'echo \'[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"\' >> ~/.bashrc',
        timeout=300,
    )

    node_bin = os.path.expanduser(f"~/.nvm/versions/node/v{version}/bin")
    if not os.path.isdir(node_bin):
        nvm_versions = os.path.expanduser("~/.nvm/versions/node")
        if os.path.isdir(nvm_versions):
            versions = sorted(os.listdir(nvm_versions), reverse=True)
            for v in versions:
                candidate = os.path.join(nvm_versions, v, "bin")
                if os.path.isdir(candidate):
                    node_bin = candidate
                    break

    if os.path.isdir(node_bin):
        os.environ["PATH"] = f"{node_bin}:{os.environ['PATH']}"
        logger.info(f"Node.js setup: PATH updated to {node_bin}")
        return True

    logger.error("Node.js setup failed")
    return False


async def _detect_package_manager(project_dir):
    if os.path.exists(os.path.join(project_dir, "yarn.lock")):
        return "yarn"
    return "npm"


async def _install_node_deps(project_dir, logs):
    pm = await _detect_package_manager(project_dir)
    if pm == "yarn":
        code, out, err = await run_cmd("yarn install --frozen-lockfile || yarn install", cwd=project_dir, timeout=600)
    else:
        code, out, err = await run_cmd("npm install --legacy-peer-deps", cwd=project_dir, timeout=600)

    if code != 0:
        logs.append(f"Warning: {pm} install ada warning, cuba teruskan...")
        pm_cmd = "yarn install" if pm == "yarn" else "npm install --force"
        code, out, err = await run_cmd(pm_cmd, cwd=project_dir, timeout=600)

    logs.append(f"{pm} install: {'OK' if code == 0 else 'FAIL'}")
    return code == 0


def _collect_apks(search_dirs):
    files = []
    for d in search_dirs:
        if os.path.exists(d):
            for root, _, fnames in os.walk(d):
                for fn in fnames:
                    if fn.endswith((".apk", ".aab")):
                        files.append(os.path.join(root, fn))
    return files


# ================================================================
# SHARED HELPERS
# ================================================================

def _detect_kotlin_version(root_dir):
    """
    Detect Kotlin version yang digunakan dalam projek.
    Scan semua gradle files dan libs.versions.toml.
    Return: string version (e.g. '1.9.0', '2.0.0') atau None.
    """
    skip_dirs = {".git", "build", ".gradle", "node_modules", ".dart_tool"}
    patterns = [
        r'kotlin[_\-]version\s*=\s*["\x27]([0-9]+\.[0-9.]+)["\x27]',
        r'id\s*\(?\s*["\x27]org\.jetbrains\.kotlin\.[^"\']+["\x27]\s*\)?\s+version\s+["\x27]([0-9]+\.[0-9.]+)["\x27]',
        r'kotlin\s*\(\s*["\x27][^"\']+["\x27]\s*\)\s+version\s+["\x27]([0-9]+\.[0-9.]+)["\x27]',
        r'org\.jetbrains\.kotlin:kotlin-gradle-plugin:([0-9]+\.[0-9.]+)',
        r'ext\.kotlin_version\s*=\s*["\x27]([0-9]+\.[0-9.]+)["\x27]',
    ]
    found = []
    for dirpath, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if fn.endswith((".gradle", ".gradle.kts", ".toml")):
                try:
                    with open(os.path.join(dirpath, fn), "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    for pat in patterns:
                        for m in re.finditer(pat, content, re.IGNORECASE):
                            found.append(m.group(1))
                except Exception:
                    pass
    if not found:
        return None
    return max(found, key=_ver_tuple)


def _detect_sdk_value(root_dir, key):
    """
    Detect nilai integer SDK (compileSdk, targetSdk, minSdk) tertinggi dalam projek.
    Return: int atau None.
    """
    skip_dirs = {".git", "build", ".gradle", "node_modules", ".dart_tool"}
    found = []
    variants = [key, key + "Version"]  # e.g. compileSdk + compileSdkVersion
    for dirpath, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if fn.endswith((".gradle", ".gradle.kts", ".toml")):
                try:
                    with open(os.path.join(dirpath, fn), "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    for v in variants:
                        for m in re.finditer(rf'\b{v}\s*[=:]?\s*(\d{{2,3}})\b', content):
                            found.append(int(m.group(1)))
                except Exception:
                    pass
    return max(found) if found else None


def _ver_tuple(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return (0,)


def _detect_project_java_version(android_dir):
    """
    Detect versi Java tertinggi yang digunakan dalam projek Android.
    Scan semua build.gradle / build.gradle.kts dalam android_dir.
    Return: int versi Java tertinggi yang dijumpai, atau None.
    """
    skip_dirs = {".git", "build", ".gradle", "node_modules", ".dart_tool"}
    highest = None
    for dirpath, dirs, files in os.walk(android_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if fn in ("build.gradle", "build.gradle.kts"):
                ver = _detect_java_version_in_file(os.path.join(dirpath, fn))
                if ver and (highest is None or ver > highest):
                    highest = ver
    return highest


def _agp_to_min_gradle(agp_ver: str) -> str:
    """Map AGP version → minimum Gradle version required. Covers AGP 1.x–9.x."""
    try:
        parts = agp_ver.split(".")
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return "8.11.1"

    if major <= 2:   return "2.14.1"
    elif major == 3:
        if minor == 0:   return "4.1"
        elif minor == 1: return "4.4"
        elif minor == 2: return "4.6"
        elif minor == 3: return "4.10.1"
        elif minor == 4: return "5.1.1"
        elif minor == 5: return "5.4.1"
        else:            return "5.6.4"
    elif major == 4:
        if minor == 0:   return "6.1.1"
        elif minor == 1: return "6.5"
        else:            return "6.7.1"
    elif major in (5, 6): return "7.0"
    elif major == 7:
        if minor == 0:   return "7.0"
        elif minor == 1: return "7.2"
        elif minor == 2: return "7.3.3"
        elif minor == 3: return "7.4"
        else:            return "7.5"
    elif major == 8:
        if minor <= 1:   return "8.0"
        elif minor == 2: return "8.2"
        elif minor == 3: return "8.4"
        elif minor == 4: return "8.6"
        elif minor <= 6: return "8.7"
        elif minor == 7: return "8.9"
        elif minor == 8: return "8.10.2"
        else:            return "8.11.1"
    else:
        return "8.11.1"


def _fix_repositories_in_file(file_path, logs):
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        original = content
        content = re.sub(r'\bjcenter\(\)', 'mavenCentral()', content)
        if content != original:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            logs.append(f"Auto-fix: jcenter() → mavenCentral() dalam {os.path.basename(file_path)}")
    except Exception:
        pass


def _fix_repositories_in_dir(root_dir, logs):
    skip_dirs = {".git", "build", ".gradle", "node_modules", ".dart_tool"}
    for dirpath, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if fn.endswith((".gradle", ".gradle.kts")):
                _fix_repositories_in_file(os.path.join(dirpath, fn), logs)


def _fix_version_catalog(root_dir, logs, min_agp, min_kotlin, min_compile, min_target, min_min_sdk):
    candidates = [
        os.path.join(root_dir, "gradle", "libs.versions.toml"),
        os.path.join(root_dir, "android", "gradle", "libs.versions.toml"),
        os.path.join(os.path.dirname(root_dir), "gradle", "libs.versions.toml"),
    ]
    for toml_path in candidates:
        if not os.path.exists(toml_path):
            continue
        try:
            with open(toml_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            original = content

            def fix_str(text, key, min_ver):
                """Fix string versi — naikkan kalau rendah, kekal kalau tinggi."""
                pat = rf'(?m)^(\s*{key}\s*=\s*["\x27])([0-9]+\.[0-9][0-9.]*)(["\x27])'
                def rep(m):
                    cur = m.group(2)
                    return (m.group(1) + min_ver + m.group(3)) if _ver_tuple(cur) < _ver_tuple(min_ver) else m.group(0)
                return re.sub(pat, rep, text, flags=re.IGNORECASE)

            def fix_int(text, key, min_val):
                """Fix integer versi — naikkan kalau rendah, kekal kalau tinggi."""
                pat = rf'(?m)^(\s*{key}\s*=\s*)(\d+)'
                return re.sub(pat, lambda m: m.group(1) + str(max(int(m.group(2)), min_val)), text, flags=re.IGNORECASE)

            # AGP
            content = fix_str(content, "agp", min_agp)

            # Kotlin — semua bentuk key yang biasa dipakai
            for kotlin_key in ("kotlin", "kotlin\\.version", "kotlinVersion", "kotlin-version", "kotlin_version"):
                content = fix_str(content, kotlin_key, min_kotlin)

            # SDK versions
            content = fix_int(content, "compileSdk",    min_compile)
            content = fix_int(content, "targetSdk",     min_target)
            content = fix_int(content, "minSdk",        min_min_sdk)
            # Juga cover variant names
            content = fix_int(content, "compile.sdk",   min_compile)
            content = fix_int(content, "target.sdk",    min_target)
            content = fix_int(content, "min.sdk",       min_min_sdk)
            content = fix_int(content, "compileSdkVersion", min_compile)
            content = fix_int(content, "targetSdkVersion",  min_target)
            content = fix_int(content, "minSdkVersion",     min_min_sdk)

            if content != original:
                with open(toml_path, "w", encoding="utf-8") as f:
                    f.write(content)
                logs.append(f"Auto-fix: versions updated dalam {os.path.basename(toml_path)}")
        except Exception:
            pass


def _bump_sdk_in_file(file_path, logs, min_compile, min_target, min_min_sdk):
    """
    Bump compileSdk / targetSdk / minSdk dalam mana-mana build.gradle / build.gradle.kts.
    Naikkan kalau rendah, kekal kalau dah tinggi.
    Skip kalau nilai adalah variable reference (bukan literal integer).
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        original = content

        def bump(text, keys, min_val):
            pattern = r'(\b(?:' + '|'.join(keys) + r')\s*[=:]?\s*)(\d+)'
            return re.sub(pattern, lambda m: m.group(1) + str(max(int(m.group(2)), min_val)), text)

        content = bump(content, ["compileSdk", "compileSdkVersion"],       min_compile)
        content = bump(content, ["targetSdk",  "targetSdkVersion"],        min_target)
        content = bump(content, ["minSdk",     "minSdkVersion"],           min_min_sdk)

        if content != original:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            logs.append(f"Auto-fix: SDK versions updated dalam {os.path.basename(file_path)}")
    except Exception:
        pass


def _fix_kotlin_in_file(file_path, logs, min_kotlin):
    """
    Fix Kotlin version dalam mana-mana gradle file.
    Support semua format termasuk Kotlin 1.x dan 2.x.
    Tak turunkan kalau projek dah guna versi lebih tinggi.
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        original = content

        def r(m):
            cur = m.group(2)
            return (m.group(1) + min_kotlin + m.group(3)) if _ver_tuple(cur) < _ver_tuple(min_kotlin) else m.group(0)

        # kotlin_version = "1.9.0" / kotlin-version = "2.0.0"
        content = re.sub(
            r'(kotlin[_\-]version\s*=\s*["\x27])([0-9]+\.[0-9.]+)(["\x27])',
            r, content, flags=re.IGNORECASE
        )
        # id("org.jetbrains.kotlin.android") version "2.0.0"
        content = re.sub(
            r'(id\s*\(?\s*["\x27]org\.jetbrains\.kotlin\.[^"\']+["\x27]\s*\)?\s+version\s+["\x27])([0-9]+\.[0-9.]+)(["\x27])',
            r, content
        )
        # kotlin("android") version "2.0.0"  /  kotlin("jvm") version "2.0.0"
        content = re.sub(
            r'(kotlin\s*\(\s*["\x27][^"\']+["\x27]\s*\)\s+version\s+["\x27])([0-9]+\.[0-9.]+)(["\x27])',
            r, content
        )
        # classpath "org.jetbrains.kotlin:kotlin-gradle-plugin:1.9.0"
        content = re.sub(
            r'(org\.jetbrains\.kotlin:kotlin-gradle-plugin:)([0-9]+\.[0-9.]+)',
            lambda m: m.group(1) + (min_kotlin if _ver_tuple(m.group(2)) < _ver_tuple(min_kotlin) else m.group(2)),
            content
        )
        # ext.kotlin_version = "1.9.0"
        content = re.sub(
            r'(ext\.kotlin_version\s*=\s*["\x27])([0-9]+\.[0-9.]+)(["\x27])',
            r, content
        )

        if content != original:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            logs.append(f"Auto-fix: Kotlin version → min {min_kotlin} dalam {os.path.basename(file_path)}")
    except Exception:
        pass


def _detect_java_version_in_file(file_path):
    """
    Detect versi Java tertinggi yang digunakan dalam satu gradle file.
    Scan: JavaVersion enum, jvmTarget, JavaLanguageVersion.of(), sourceCompatibility.
    Return: int versi (e.g. 17, 21) atau None kalau tak jumpa.
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        return None

    found = []

    # JavaVersion.VERSION_21, VERSION_17, VERSION_11, VERSION_1_8, etc.
    for m in re.finditer(r'\bJavaVersion\.VERSION_(?:1_(\d)|(1[0-9]|2[0-9]|\d{2}))\b', content):
        ver = int(m.group(1) or m.group(2))
        found.append(ver)

    # jvmTarget = "21" / "17" / "11" / "1.8"
    for m in re.finditer(r'jvmTarget\s*=\s*["\x27](1\.(\d)|(\d{2}))["\x27]', content):
        ver = int(m.group(2) or m.group(3))
        found.append(ver)

    # JavaLanguageVersion.of(21)
    for m in re.finditer(r'JavaLanguageVersion\.of\((\d+)\)', content):
        found.append(int(m.group(1)))

    # sourceCompatibility = JavaVersion.VERSION_21 (already caught above)
    # sourceCompatibility = 17 (integer)
    for m in re.finditer(r'(?:source|target)Compatibility\s*=\s*(\d+)\b', content):
        found.append(int(m.group(1)))

    return max(found) if found else None


def _fix_java_compat_in_file(file_path, logs, target_java="17"):
    """
    Fix Java compatibility settings dalam build.gradle / build.gradle.kts.

    PRINSIP:
    - Nilai integer yang lebih rendah dari target_java → naikkan ke target_java
    - Nilai yang sudah lebih tinggi → KEKAL (jangan turunkan)
    - Support semua versi: 8, 11, 17, 21, 23, dll

    target_java: minimum Java version yang diperlukan (string, e.g. "17", "21")
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        original = content
        min_ver = int(target_java)

        # ── JavaVersion enum ─────────────────────────────────────────────────────
        # Map: VERSION_1_5 → 5, VERSION_1_8 → 8, VERSION_11 → 11, VERSION_21 → 21
        def fix_java_version_enum(m):
            raw = m.group(1)
            if raw.startswith("1_"):
                ver = int(raw[2:])   # VERSION_1_8 → 8
            else:
                ver = int(raw)       # VERSION_17 → 17
            new_ver = max(ver, min_ver)
            if new_ver <= 8:
                return f"JavaVersion.VERSION_1_{new_ver}"
            return f"JavaVersion.VERSION_{new_ver}"

        content = re.sub(
            r'\bJavaVersion\.VERSION_(1_[0-9]|[0-9]{1,2})\b',
            fix_java_version_enum,
            content
        )

        # ── jvmTarget string ─────────────────────────────────────────────────────
        # "1.8" → java 8, "11" → java 11, "17" → java 17, "21" → java 21
        def fix_jvm_target(m):
            raw = m.group(2)
            if "." in raw:
                ver = int(raw.split(".")[1])  # "1.8" → 8
            else:
                ver = int(raw)
            new_ver = max(ver, min_ver)
            new_str = f"1.{new_ver}" if new_ver <= 8 else str(new_ver)
            return m.group(1) + new_str + m.group(3)

        content = re.sub(
            r'(jvmTarget\s*=\s*["\x27])(1\.[0-9]|[0-9]{1,2})(["\x27])',
            fix_jvm_target,
            content
        )

        # ── JavaLanguageVersion.of(N) ────────────────────────────────────────────
        content = re.sub(
            r'JavaLanguageVersion\.of\((\d+)\)',
            lambda m: f'JavaLanguageVersion.of({max(int(m.group(1)), min_ver)})',
            content
        )

        # ── sourceCompatibility / targetCompatibility = N (integer) ─────────────
        content = re.sub(
            r'((?:source|target)Compatibility\s*=\s*)(\d+)\b(?!\s*\.)',
            lambda m: m.group(1) + str(max(int(m.group(2)), min_ver)),
            content
        )

        if content != original:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            logs.append(f"Auto-fix: Java compat → min {target_java} dalam {os.path.basename(file_path)}")
    except Exception:
        pass


# ================================================================
# NDK SMART FIX
# ================================================================

def _get_sdkmanager():
    ah = os.environ.get("ANDROID_HOME", "/usr/local/lib/android/sdk")
    for candidate in [
        os.path.join(ah, "cmdline-tools", "latest", "bin", "sdkmanager"),
        os.path.join(ah, "tools", "bin", "sdkmanager"),
    ]:
        if os.path.exists(candidate):
            return candidate
    return None


async def _install_ndk_version(ndk_ver, logs):
    """Install NDK via sdkmanager. Return True kalau berjaya."""
    sm = _get_sdkmanager()
    if not sm:
        logs.append(f"Auto-fix: sdkmanager tidak jumpa, NDK {ndk_ver} tidak dapat dipasang")
        return False
    ah = os.environ.get("ANDROID_HOME", "/usr/local/lib/android/sdk")
    ndk_path = os.path.join(ah, "ndk", ndk_ver)
    if os.path.isdir(ndk_path):
        return True
    logs.append(f"Auto-fix: Memasang NDK {ndk_ver}...")
    code, _, err = await run_cmd(f'echo "y" | "{sm}" "ndk;{ndk_ver}"', timeout=600)
    if code == 0 and os.path.isdir(ndk_path):
        logs.append(f"Auto-fix: NDK {ndk_ver} berjaya dipasang")
        return True
    logs.append(f"Auto-fix: NDK {ndk_ver} gagal dipasang — {(err or '')[:150]}")
    return False


def _write_ndk_in_gradle(android_dir, new_ver, logs):
    """Tulis semula ndkVersion dalam build.gradle / build.gradle.kts."""
    for bg_name in ("app/build.gradle.kts", "app/build.gradle"):
        bg_path = os.path.join(android_dir, bg_name)
        if not os.path.exists(bg_path):
            continue
        try:
            with open(bg_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            original = content
            # Replace existing ndkVersion
            new_content = re.sub(
                r'(ndkVersion\s*=\s*["\x27])([0-9.]+)(["\x27])',
                lambda m: m.group(1) + new_ver + m.group(3),
                content
            )
            # Kalau tak ada ndkVersion langsung, insert selepas namespace
            if new_content == content and 'ndkVersion' not in content:
                new_content = re.sub(
                    r'(namespace\s*=\s*["\x27][^"\']+["\x27])',
                    rf'\1\n    ndkVersion = "{new_ver}"',
                    content, count=1
                )
            if new_content != original:
                with open(bg_path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                logs.append(f"Auto-fix: ndkVersion → {new_ver} dalam {bg_name}")
                return True
        except Exception:
            pass
    return False


async def _ensure_ndk_smart(android_dir, logs, extra_required=None):
    """
    Smart NDK resolver — ambil versi TERTINGGI antara semua keperluan.

    Kenapa tertinggi? NDK adalah backward compatible — versi baru boleh
    compile kod yang designed untuk versi lama.

    extra_required: set of NDK version strings dari error message plugin
    """
    extra_required = extra_required or set()

    def ndk_key(v):
        try:
            return tuple(int(x) for x in v.split("."))
        except Exception:
            return (0,)

    # Baca NDK yang ditulis dalam project
    declared_ver = None
    for bg_name in ("app/build.gradle.kts", "app/build.gradle"):
        bg_path = os.path.join(android_dir, bg_name)
        if not os.path.exists(bg_path):
            continue
        try:
            with open(bg_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            m = re.search(r'ndkVersion\s*[=:]\s*["\x27]([0-9.]+)["\x27]', content)
            if m:
                declared_ver = m.group(1)
                break
        except Exception:
            pass

    # Kumpul semua keperluan
    all_required = set(extra_required)
    if declared_ver:
        all_required.add(declared_ver)

    if not all_required:
        return

    highest = max(all_required, key=ndk_key)

    # Update fail kalau declared version lebih rendah dari yang diperlukan
    if declared_ver and ndk_key(declared_ver) < ndk_key(highest):
        _write_ndk_in_gradle(android_dir, highest, logs)
    elif not declared_ver and extra_required:
        _write_ndk_in_gradle(android_dir, highest, logs)

    # Install NDK versi tertinggi
    await _install_ndk_version(highest, logs)


def _parse_ndk_from_error(error_text):
    """Parse versi NDK yang diperlukan plugin dari error message Gradle."""
    versions = set()
    for m in re.finditer(r'requires\s+Android\s+NDK\s+([0-9]+\.[0-9]+\.[0-9]+)', error_text, re.IGNORECASE):
        versions.add(m.group(1))
    for m in re.finditer(r'ndkVersion\s*=\s*["\x27]([0-9.]+)["\x27]', error_text):
        versions.add(m.group(1))
    return versions


def _parse_agp_from_error(error_text):
    """Parse versi AGP minimum yang diperlukan dependency dari error message Gradle."""
    highest = None
    for m in re.finditer(
        r'requires\s+Android\s+Gradle\s+plugin\s+([0-9]+\.[0-9]+\.[0-9]+)\s+or\s+higher',
        error_text, re.IGNORECASE
    ):
        ver = m.group(1)
        if highest is None or _ver_tuple(ver) > _ver_tuple(highest):
            highest = ver
    return highest


def _parse_gradle_dist_error(error_text):
    """
    Detect kegagalan muat turun Gradle Wrapper distribution (cth. distributionUrl
    projek tunjuk ke mirror pihak ketiga yang down/404/rosak).
    Pulangkan (versi, jenis) cth ("8.14.0", "all") kalau jumpa, atau None.
    Sengaja TAK trigger untuk kegagalan build/compile biasa — hanya untuk
    kegagalan MUAT TURUN gradle itu sendiri (sebelum build sempat bermula).
    """
    if ("FileNotFoundException" not in error_text
            and "Gradle threw an error while downloading artifacts" not in error_text):
        return None
    m = re.search(r'gradle-([0-9]+\.[0-9]+(?:\.[0-9]+)?)-(bin|all)\.zip', error_text)
    if m:
        return m.group(1), m.group(2)
    return None


def _force_official_gradle_url(props_path, version, dist_type, logs):
    """
    Tulis semula distributionUrl ke host RASMI services.gradle.org, dengan
    versi & jenis (bin/all) YANG SAMA seperti ditetapkan asal oleh developer
    projek — cuma host yang ditukar, bukan versi. Dipanggil sebagai FALLBACK
    sahaja selepas URL asal projek terbukti gagal dimuat turun.
    """
    try:
        with open(props_path, "r", encoding="utf-8", errors="replace") as f:
            pc = f.read()
        new_url = f"https\\://services.gradle.org/distributions/gradle-{version}-{dist_type}.zip"
        new_pc = re.sub(r"distributionUrl=.*", f"distributionUrl={new_url}", pc)
        with open(props_path, "w", encoding="utf-8") as f:
            f.write(new_pc)
        logs.append(f"Auto-fix: Gradle {version} gagal dimuat turun dari sumber asal projek → tukar ke services.gradle.org")
        return True
    except Exception:
        return False


# ================================================================
# KOTLIN/JAVA JVM-TARGET MISMATCH (plugin pihak ketiga belum migrate ke
# Flutter Built-in Kotlin — cth package_info_plus, share_plus, dll yang
# masih apply Kotlin Gradle Plugin sendiri secara berasingan)
# ================================================================

def _parse_jvm_kotlin_mismatch(error_text):
    """
    Detect ralat 'Inconsistent JVM Target Compatibility' antara task Java
    (cth compileDebugJavaWithJavac) dan task Kotlin (compileDebugKotlin).
    Pulangkan (java_ver, kotlin_ver) sebagai int, atau None kalau tak jumpa.
    """
    m = re.search(
        r"compileDebugJavaWithJavac['\x27]?\s*\((\d+)\)\s*and\s*['\x27]?compileDebugKotlin['\x27]?\s*\((\d+)\)",
        error_text
    )
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _has_kgp_plugin_warning(error_text):
    """Detect amaran 'plugins that apply Kotlin Gradle Plugin (KGP)' dari Flutter."""
    return "apply Kotlin Gradle Plugin (KGP)" in error_text


def _set_kotlin_jvm_validation_warning(android_dir, logs):
    """
    Set kotlin.jvm.target.validation.mode=warning dalam gradle.properties.

    Ini TIDAK menukar/paksa sebarang versi AGP/Kotlin/Java projek — cuma
    minta Gradle papar amaran (bukan fail build) bila ia jumpa task Java &
    Kotlin guna JVM-target berbeza across module (lazim berlaku bila plugin
    pihak ketiga macam package_info_plus/share_plus dsb apply Kotlin Gradle
    Plugin dia sendiri secara berasingan dari app, dan belum migrate ke
    Flutter Built-in Kotlin). Idempotent — skip kalau dah pernah di-set.
    """
    props_path = os.path.join(android_dir, "gradle.properties")
    try:
        content = ""
        if os.path.exists(props_path):
            with open(props_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        if "kotlin.jvm.target.validation.mode" in content:
            return False
        if content and not content.endswith("\n"):
            content += "\n"
        content += "kotlin.jvm.target.validation.mode=warning\n"
        with open(props_path, "w", encoding="utf-8") as f:
            f.write(content)
        logs.append(
            "Auto-fix: kotlin.jvm.target.validation.mode=warning "
            "(plugin pihak ketiga guna JVM-target berbeza dari app — bukan konflik binari sebenar)"
        )
        return True
    except Exception:
        return False


# Fallback generasi AGP terdahulu yang diketahui masih serasi dengan plugin
# lama yang apply Kotlin Gradle Plugin secara manual (sebelum "Built-in
# Kotlin" AGP 9 jadi wajib). Hanya digunakan sebagai LANGKAH TERAKHIR kalau
# kotlin.jvm.target.validation.mode=warning masih tak cukup untuk build lulus.
_AGP_MAJOR_FALLBACK = {
    9: "8.7.0",
    8: "7.4.2",
    7: "4.2.2",
}


def _detect_agp_version(android_dir, project_dir):
    """Detect versi AGP semasa projek (settings.gradle/.kts, build.gradle/.kts, libs.versions.toml)."""
    for sg_name in ("settings.gradle.kts", "settings.gradle"):
        sg_path = os.path.join(android_dir, sg_name)
        if os.path.exists(sg_path):
            try:
                with open(sg_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                m = re.search(
                    r'id\s*\(?\s*["\x27]com\.android\.(?:application|library)["\x27]\s*\)?\s+version\s+["\x27]([0-9.]+)["\x27]',
                    content
                )
                if m:
                    return m.group(1)
            except Exception:
                pass
    for bg_name in ("build.gradle.kts", "build.gradle"):
        bg_path = os.path.join(android_dir, bg_name)
        if os.path.exists(bg_path):
            try:
                with open(bg_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                m = re.search(r'com\.android\.tools\.build:gradle:([0-9.]+)', content)
                if m:
                    return m.group(1)
            except Exception:
                pass
    for toml_path in (os.path.join(project_dir, "gradle", "libs.versions.toml"),
                       os.path.join(android_dir, "gradle", "libs.versions.toml")):
        if os.path.exists(toml_path):
            try:
                with open(toml_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                m = re.search(r'(?im)^\s*agp\s*=\s*["\x27]([0-9.]+)["\x27]', content)
                if m:
                    return m.group(1)
            except Exception:
                pass
    return None


def _set_agp_version(project_dir, android_dir, new_agp, logs, reason=""):
    """
    Tulis semula versi AGP guna cara declare yang SAMA seperti projek asal
    (settings.gradle(.kts) / build.gradle(.kts) / libs.versions.toml) —
    cuma nilai versi yang ditukar, bukan cara declare dia.
    """
    patterns = [
        (os.path.join(android_dir, "settings.gradle.kts"),
         r'(id\s*\(?\s*["\x27]com\.android\.(?:application|library)["\x27]\s*\)?\s+version\s+["\x27])([0-9.]+)(["\x27])'),
        (os.path.join(android_dir, "settings.gradle"),
         r'(id\s*\(?\s*["\x27]com\.android\.(?:application|library)["\x27]\s*\)?\s+version\s+["\x27])([0-9.]+)(["\x27])'),
        (os.path.join(android_dir, "build.gradle.kts"),
         r'(com\.android\.tools\.build:gradle:)([0-9.]+)()'),
        (os.path.join(android_dir, "build.gradle"),
         r'(com\.android\.tools\.build:gradle:)([0-9.]+)()'),
    ]
    for path, pattern in patterns:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            m = re.search(pattern, content)
            if not m:
                continue
            new_content = content[:m.start()] + m.group(1) + new_agp + m.group(3) + content[m.end():]
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
            suffix = f" — {reason}" if reason else ""
            logs.append(f"Auto-fix: AGP → {new_agp} dalam {os.path.basename(path)}{suffix}")
            return True
        except Exception:
            pass
    for toml_path in (os.path.join(project_dir, "gradle", "libs.versions.toml"),
                       os.path.join(android_dir, "gradle", "libs.versions.toml")):
        if not os.path.exists(toml_path):
            continue
        try:
            with open(toml_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            new_content, n = re.subn(
                r'(?im)(^\s*agp\s*=\s*["\x27])([0-9.]+)(["\x27])',
                lambda m: m.group(1) + new_agp + m.group(3),
                content
            )
            if n:
                with open(toml_path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                suffix = f" — {reason}" if reason else ""
                logs.append(f"Auto-fix: AGP → {new_agp} dalam libs.versions.toml{suffix}")
                return True
        except Exception:
            pass
    return False


async def _fallback_downgrade_agp_for_legacy_plugins(project_dir, android_dir, logs):
    """
    LANGKAH TERAKHIR: kalau kotlin.jvm.target.validation.mode=warning tak
    cukup (masih gagal — bermakna konflik binari sebenar, bukan sekadar
    validation check), turunkan AGP ke generasi SEBELUMNYA yang serasi
    dengan plugin lama. Adaptif ikut AGP yang DIKESAN dalam projek — bukan
    versi tetap yang dipaksa untuk semua projek.
    """
    cur_agp = _detect_agp_version(android_dir, project_dir)
    if not cur_agp:
        return False
    try:
        major = int(cur_agp.split(".")[0])
    except (ValueError, IndexError):
        return False
    fallback_agp = _AGP_MAJOR_FALLBACK.get(major)
    if not fallback_agp:
        return False

    ok = _set_agp_version(
        project_dir, android_dir, fallback_agp, logs,
        reason=f"plugin pihak ketiga belum serasi dgn AGP {major}.x Built-in Kotlin"
    )
    if not ok:
        return False

    required_gradle = _agp_to_min_gradle(fallback_agp)
    props_path = os.path.join(android_dir, "gradle", "wrapper", "gradle-wrapper.properties")
    if os.path.exists(props_path):
        try:
            with open(props_path, "r", encoding="utf-8", errors="replace") as f:
                pc = f.read()
            new_url = f"https\\://services.gradle.org/distributions/gradle-{required_gradle}-bin.zip"
            new_pc = re.sub(r"distributionUrl=.*", f"distributionUrl={new_url}", pc)
            with open(props_path, "w", encoding="utf-8") as f:
                f.write(new_pc)
            logs.append(f"Auto-fix: Gradle → {required_gradle} (selaras dengan AGP {fallback_agp})")
        except Exception:
            pass

    required_java = _agp_to_min_java(fallback_agp)
    await setup_java(str(required_java))
    logs.append(f"Info: Java {required_java} diperlukan selepas AGP diturunkan ke {fallback_agp}")
    return True


# ================================================================
# MAIN FIX FUNCTIONS
# ================================================================

async def fix_common_issues(project_dir, logs, gradle_subdir=""):
    """
    Auto-fix untuk SEMUA jenis projek Android.

    FALSAFAH: Builder ikut projek, bukan projek ikut builder.
    - Detect semua versi DARI projek itu sendiri
    - Pasang tools (Gradle, Java, NDK) yang sesuai dengan projek
    - Hanya fix kalau ada conflict nyata (jcenter, CRLF, Gradle terlalu lama untuk AGP, dll)
    - TIDAK paksa upgrade AGP, Kotlin, SDK, atau Java ke versi tertentu
    """
    gdir = os.path.join(project_dir, gradle_subdir) if gradle_subdir else project_dir
    gradlew = os.path.join(gdir, "gradlew")
    skip_dirs = {".git", "build", ".gradle", "node_modules", ".dart_tool"}

    # ── 1. Fix CRLF ─────────────────────────────────────────────────────────────
    crlf_fixed = 0
    for root, dirs, fnames in os.walk(gdir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in fnames:
            if fn in ("gradlew",) or fn.endswith((".gradle", ".properties", ".xml", ".pro", ".kts")):
                fpath = os.path.join(root, fn)
                try:
                    with open(fpath, "rb") as f:
                        content = f.read()
                    if b"\r\n" in content:
                        with open(fpath, "wb") as f:
                            f.write(content.replace(b"\r\n", b"\n"))
                        crlf_fixed += 1
                except Exception:
                    pass
    if crlf_fixed > 0:
        logs.append(f"Auto-fix: line endings fixed ({crlf_fixed} files)")

    # ── 2. Fix jcenter → mavenCentral (deprecated, cause build fail) ─────────
    _fix_repositories_in_dir(gdir, logs)

    # ── 3. Detect AGP version dari projek ───────────────────────────────────────
    cur_agp = None
    for sg_name in ("settings.gradle", "settings.gradle.kts"):
        sg_path = os.path.join(gdir, sg_name)
        if not os.path.exists(sg_path):
            continue
        try:
            with open(sg_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            m = re.search(
                r'id\s*\(?\s*["\x27]com\.android\.(?:application|library)["\x27]\s*\)?\s+version\s+["\x27]([0-9.]+)["\x27]',
                content
            )
            if m:
                cur_agp = m.group(1)
                break
        except Exception:
            pass

    if not cur_agp:
        for bg_name in ("build.gradle", "build.gradle.kts"):
            bg_path = os.path.join(gdir, bg_name)
            if not os.path.exists(bg_path):
                continue
            try:
                with open(bg_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                m = re.search(r'com\.android\.tools\.build:gradle:([0-9.]+)', content)
                if m:
                    cur_agp = m.group(1)
                    break
            except Exception:
                pass

    if not cur_agp:
        for toml_path in [
            os.path.join(gdir, "gradle", "libs.versions.toml"),
            os.path.join(gdir, "app", "gradle", "libs.versions.toml"),
        ]:
            if not os.path.exists(toml_path):
                continue
            try:
                with open(toml_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                m = re.search(r'(?im)^\s*agp\s*=\s*["\x27]([0-9.]+)["\x27]', content)
                if m:
                    cur_agp = m.group(1)
                    break
            except Exception:
                pass

    if cur_agp:
        logs.append(f"Info: AGP {cur_agp} detected")

    # ── 4. Gradle wrapper — pastikan sesuai dengan AGP projek ───────────────────
    required_gradle = _agp_to_min_gradle(cur_agp) if cur_agp else None
    gradle_ver = None
    gradle_dist_url = None
    props_path = os.path.join(gdir, "gradle", "wrapper", "gradle-wrapper.properties")

    if os.path.exists(props_path):
        try:
            with open(props_path, "r", encoding="utf-8", errors="replace") as f:
                pc = f.read()
            for line in pc.splitlines():
                if "distributionUrl" in line:
                    url = line.split("=", 1)[1].strip().replace("\\:", ":")
                    gradle_dist_url = url
                    m = re.search(r"gradle-([0-9.]+)-", url)
                    if m:
                        cur_gradle = m.group(1)
                        gradle_ver = cur_gradle
                        # Upgrade Gradle HANYA kalau tak cukup untuk jalankan AGP projek
                        if required_gradle and _ver_tuple(cur_gradle) < _ver_tuple(required_gradle):
                            new_url = f"https\\://services.gradle.org/distributions/gradle-{required_gradle}-bin.zip"
                            new_pc = re.sub(r"distributionUrl=.*", f"distributionUrl={new_url}", pc)
                            with open(props_path, "w", encoding="utf-8") as f:
                                f.write(new_pc)
                            logs.append(f"Auto-fix: Gradle {cur_gradle} → {required_gradle} (diperlukan oleh AGP {cur_agp})")
                            gradle_ver = required_gradle
                            gradle_dist_url = None
                        else:
                            logs.append(f"Info: Gradle {cur_gradle} sesuai untuk projek ini")
                    break
        except Exception:
            pass
    else:
        target_gradle = required_gradle or "8.11.1"
        gradle_ver = target_gradle
        try:
            os.makedirs(os.path.join(gdir, "gradle", "wrapper"), exist_ok=True)
            with open(props_path, "w", encoding="utf-8") as f:
                f.write(
                    "distributionBase=GRADLE_USER_HOME\n"
                    "distributionPath=wrapper/dists\n"
                    f"distributionUrl=https\\://services.gradle.org/distributions/gradle-{target_gradle}-bin.zip\n"
                    "zipStoreBase=GRADLE_USER_HOME\n"
                    "zipStorePath=wrapper/dists\n"
                )
            logs.append(f"Auto-fix: created gradle-wrapper.properties (Gradle {target_gradle})")
        except Exception:
            pass

    if not gradle_dist_url:
        gradle_dist_url = f"https://services.gradle.org/distributions/gradle-{gradle_ver or '8.11.1'}-bin.zip"

    # ── 5. Generate gradlew kalau tak ada ───────────────────────────────────────
    if not os.path.exists(gradlew):
        dl_ver = gradle_ver or required_gradle or "8.11.1"
        logs.append(f"Auto-fix: gradlew missing, downloading Gradle {dl_ver}...")
        dl_dir = f"/tmp/gradle-inst/gradle-{dl_ver}"
        if not os.path.exists(os.path.join(dl_dir, "bin", "gradle")):
            await run_cmd(
                f"curl -fsSL '{gradle_dist_url}' -o /tmp/gradle-dl.zip && "
                f"rm -rf /tmp/gradle-inst && "
                f"unzip -qo /tmp/gradle-dl.zip -d /tmp/gradle-inst",
                timeout=300,
            )
        gradle_bin = os.path.join(dl_dir, "bin", "gradle")
        if os.path.exists(gradle_bin):
            code, _, _ = await run_cmd(f"{gradle_bin} wrapper", cwd=gdir, timeout=180)
            if os.path.exists(gradlew):
                await run_cmd(f"chmod +x {gradlew}")
                logs.append(f"Auto-fix: gradle wrapper generated (v{dl_ver})")
            else:
                logs.append("Auto-fix: wrapper generation failed, akan guna gradle binary terus")
        else:
            logs.append("Auto-fix: gradle download failed")

    # ── 6. local.properties (sdk.dir) ───────────────────────────────────────────
    lp = os.path.join(gdir, "local.properties")
    if not os.path.exists(lp):
        ah = os.environ.get("ANDROID_HOME", "/usr/local/lib/android/sdk")
        with open(lp, "w") as f:
            f.write(f"sdk.dir={ah}\n")
        logs.append("Auto-fix: created local.properties (sdk.dir)")

    # ── 7. libs.versions.toml — fix versi yang jelas salah/conflict ─────────────
    # Baca versi semasa dari projek, guna sebagai floor (bukan paksa nilai tertentu)
    proj_agp     = cur_agp or "1.0.0"
    proj_kotlin  = _detect_kotlin_version(gdir) or "1.0.0"
    proj_compile = _detect_sdk_value(gdir, "compileSdk") or 1
    proj_target  = _detect_sdk_value(gdir, "targetSdk") or 1
    proj_min_sdk = _detect_sdk_value(gdir, "minSdk") or 1
    _fix_version_catalog(gdir, logs, proj_agp, proj_kotlin, proj_compile, proj_target, proj_min_sdk)

    # ── 8. SDK versions — bump kalau ada modul lain dalam projek yang lebih rendah
    for dirpath, dirs, files in os.walk(gdir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if fn in ("build.gradle", "build.gradle.kts"):
                fp = os.path.join(dirpath, fn)
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    if re.search(r'\bandroid\s*\{', content):
                        # Guna nilai projek sendiri sebagai floor — selaraskan modul lain
                        _bump_sdk_in_file(fp, logs, proj_compile, proj_target, proj_min_sdk)
                except Exception:
                    pass

    # ── 9. Kotlin — selaraskan kalau ada modul yang guna versi berbeza ───────────
    for dirpath, dirs, files in os.walk(gdir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if fn.endswith((".gradle", ".gradle.kts")):
                _fix_kotlin_in_file(os.path.join(dirpath, fn), logs, proj_kotlin)

    # ── 10. Java compatibility — ikut keperluan projek ───────────────────────────
    java_in_proj = _detect_project_java_version(gdir)
    java_from_agp = _agp_to_min_java(cur_agp) if cur_agp else 8
    required_java = max(java_in_proj or 0, java_from_agp)
    if required_java > 0:
        logs.append(f"Info: Java {required_java} diperlukan oleh projek ini")
        await setup_java(str(required_java))
        # Fix java compat dalam gradle files supaya selaras
        for dirpath, dirs, files in os.walk(gdir):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for fn in files:
                if fn in ("build.gradle", "build.gradle.kts"):
                    fp = os.path.join(dirpath, fn)
                    try:
                        with open(fp, "r", encoding="utf-8", errors="replace") as f:
                            content = f.read()
                        if re.search(r'\bandroid\s*\{', content):
                            _fix_java_compat_in_file(fp, logs, target_java=str(required_java))
                    except Exception:
                        pass

    # ── 11. NDK — install versi yang projek perlukan ─────────────────────────────
    android_candidate = gdir if os.path.exists(os.path.join(gdir, "app")) else None
    if android_candidate:
        await _ensure_ndk_smart(android_candidate, logs)


async def fix_flutter_versions(project_dir, logs, required_agp_override=None):
    """
    Auto-fix untuk Flutter projek — ikut versi projek, hanya fix conflict nyata.

    FALSAFAH: Detect dari projek, fix hanya bila ada conflict.
    - Gradle wrapper disesuaikan dengan AGP projek (bukan paksa ke versi terbaru)
    - AGP hanya diubah kalau ada dependency conflict (required_agp_override)
    - Java version ikut keperluan AGP projek
    - NDK smart-resolve (install versi yang plugin perlukan)
    """
    android_dir = os.path.join(project_dir, "android")
    if not os.path.isdir(android_dir):
        return

    # ── Step 1: jcenter → mavenCentral ──────────────────────────────────────────
    _fix_repositories_in_dir(android_dir, logs)

    # ── Step 2: Detect AGP dari projek ──────────────────────────────────────────
    cur_agp = None
    agp_file = None
    agp_match_start = None
    agp_match_end = None

    for sg_name in ("settings.gradle", "settings.gradle.kts"):
        sg_path = os.path.join(android_dir, sg_name)
        if not os.path.exists(sg_path):
            continue
        try:
            with open(sg_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            m = re.search(
                r'id\s*\(?\s*["\x27]com\.android\.(?:application|library)["\x27]\s*\)?\s+version\s+["\x27]([0-9.]+)["\x27]',
                content
            )
            if m:
                cur_agp = m.group(1)
                agp_file = sg_path
                agp_match_start = m.start(1)
                agp_match_end = m.end(1)
                break
        except Exception:
            pass

    if not cur_agp:
        for bg_name in ("build.gradle", "build.gradle.kts"):
            bg_path = os.path.join(android_dir, bg_name)
            if not os.path.exists(bg_path):
                continue
            try:
                with open(bg_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                m = re.search(r'com\.android\.tools\.build:gradle:([0-9.]+)', content)
                if m:
                    cur_agp = m.group(1)
                    agp_file = bg_path
                    agp_match_start = m.start(1)
                    agp_match_end = m.end(1)
                    break
            except Exception:
                pass

    if not cur_agp:
        for toml_path in [
            os.path.join(project_dir, "gradle", "libs.versions.toml"),
            os.path.join(android_dir, "gradle", "libs.versions.toml"),
        ]:
            if not os.path.exists(toml_path):
                continue
            try:
                with open(toml_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                m = re.search(r'(?im)^\s*agp\s*=\s*["\x27]([0-9.]+)["\x27]', content)
                if m:
                    cur_agp = m.group(1)
                    break
            except Exception:
                pass

    if cur_agp:
        logs.append(f"Info: AGP {cur_agp} detected")

    # ── Step 3: Fix AGP HANYA kalau ada conflict dari dependency ────────────────
    final_agp = cur_agp
    if required_agp_override and cur_agp and _ver_tuple(cur_agp) < _ver_tuple(required_agp_override):
        final_agp = required_agp_override
        if agp_file and agp_match_start is not None:
            try:
                with open(agp_file, "r", encoding="utf-8", errors="replace") as f:
                    fc = f.read()
                new_fc = fc[:agp_match_start] + required_agp_override + fc[agp_match_end:]
                with open(agp_file, "w", encoding="utf-8") as f:
                    f.write(new_fc)
                logs.append(f"Auto-fix: AGP {cur_agp} → {required_agp_override} (conflict dari dependency)")
            except Exception:
                pass

    # ── Step 4: Gradle wrapper — sesuai dengan AGP projek ───────────────────────
    required_gradle = _agp_to_min_gradle(final_agp) if final_agp else None
    props_path = os.path.join(android_dir, "gradle", "wrapper", "gradle-wrapper.properties")

    if os.path.exists(props_path):
        try:
            with open(props_path, "r", encoding="utf-8", errors="replace") as f:
                pc = f.read()
            m = re.search(r"gradle-([0-9.]+)-", pc)
            if m:
                cur_gradle = m.group(1)
                if required_gradle and _ver_tuple(cur_gradle) < _ver_tuple(required_gradle):
                    new_url = f"https\\://services.gradle.org/distributions/gradle-{required_gradle}-bin.zip"
                    new_pc = re.sub(r"distributionUrl=.*", f"distributionUrl={new_url}", pc)
                    with open(props_path, "w", encoding="utf-8") as f:
                        f.write(new_pc)
                    logs.append(f"Auto-fix: Gradle {cur_gradle} → {required_gradle} (diperlukan oleh AGP {final_agp})")
                else:
                    logs.append(f"Info: Gradle {cur_gradle} sesuai untuk projek ini")
        except Exception:
            pass
    else:
        target_gradle = required_gradle or "8.11.1"
        try:
            os.makedirs(os.path.join(android_dir, "gradle", "wrapper"), exist_ok=True)
            with open(props_path, "w", encoding="utf-8") as f:
                f.write(
                    "distributionBase=GRADLE_USER_HOME\n"
                    "distributionPath=wrapper/dists\n"
                    f"distributionUrl=https\\://services.gradle.org/distributions/gradle-{target_gradle}-bin.zip\n"
                    "zipStoreBase=GRADLE_USER_HOME\n"
                    "zipStorePath=wrapper/dists\n"
                )
            logs.append(f"Auto-fix: created gradle-wrapper.properties (Gradle {target_gradle})")
        except Exception:
            pass

    # ── Step 5: libs.versions.toml — selaraskan ikut versi projek ───────────────
    proj_kotlin  = _detect_kotlin_version(android_dir) or _detect_kotlin_version(project_dir) or "1.0.0"
    proj_compile = _detect_sdk_value(android_dir, "compileSdk") or 1
    proj_target  = _detect_sdk_value(android_dir, "targetSdk") or 1
    proj_min_sdk = _detect_sdk_value(android_dir, "minSdk") or 1
    _fix_version_catalog(project_dir, logs, final_agp or "1.0.0", proj_kotlin, proj_compile, proj_target, proj_min_sdk)

    # ── Step 6: SDK versions — selaraskan modul lain ikut nilai projek ───────────
    skip_dirs = {".git", "build", ".gradle", "node_modules", ".dart_tool"}
    for dirpath, dirs, files in os.walk(android_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if fn in ("build.gradle", "build.gradle.kts"):
                fp = os.path.join(dirpath, fn)
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    if re.search(r'\bandroid\s*\{', content):
                        _bump_sdk_in_file(fp, logs, proj_compile, proj_target, proj_min_sdk)
                except Exception:
                    pass

    # ── Step 7: Kotlin version — selaraskan semua modul ─────────────────────────
    for dirpath, dirs, files in os.walk(android_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if fn.endswith((".gradle", ".gradle.kts")):
                _fix_kotlin_in_file(os.path.join(dirpath, fn), logs, proj_kotlin)

    # ── Step 8: Java — setup dan selaraskan ikut keperluan projek ───────────────
    java_in_proj = _detect_project_java_version(android_dir)
    java_from_agp = _agp_to_min_java(final_agp) if final_agp else 8
    required_java = max(java_in_proj or 0, java_from_agp)
    if required_java > 0:
        logs.append(f"Info: Java {required_java} diperlukan oleh projek ini")
        await setup_java(str(required_java))
        for dirpath, dirs, files in os.walk(android_dir):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for fn in files:
                if fn in ("build.gradle", "build.gradle.kts"):
                    fp = os.path.join(dirpath, fn)
                    try:
                        with open(fp, "r", encoding="utf-8", errors="replace") as f:
                            content = f.read()
                        if re.search(r'\bandroid\s*\{', content):
                            _fix_java_compat_in_file(fp, logs, target_java=str(required_java))
                    except Exception:
                        pass

    # ── Step 9: NDK smart-resolve ────────────────────────────────────────────────
    await _ensure_ndk_smart(android_dir, logs)

async def _build_flutter_with_retry(project_dir, logs, android_dir):
    """
    Build flutter apk --debug dengan auto-retry.

    Pass 1: Build terus.
    Pass 2 (kalau gagal): Detect NDK conflict dari error → fix → retry.
    Pass 3 (kalau masih gagal): Detect AGP conflict dari error → fix → pub get → retry.
    Pass 4 (kalau masih gagal): Detect Gradle distribution gagal dimuat turun
            (URL/mirror asal projek down/404) → tukar ke services.gradle.org → retry.
    """
    code, out, err = await run_cmd("flutter build apk --debug", cwd=project_dir)
    if code == 0:
        return code, out, err

    combined = (err or "") + (out or "")

    # ── Retry 1: NDK version conflict ──────────────────────────────────────────
    ndk_required = _parse_ndk_from_error(combined)
    if ndk_required:
        logs.append(f"Auto-fix: NDK conflict — plugin perlukan: {', '.join(sorted(ndk_required))}")
        await _ensure_ndk_smart(android_dir, logs, extra_required=ndk_required)
        code, out, err = await run_cmd("flutter build apk --debug", cwd=project_dir)
        if code == 0:
            return code, out, err
        combined = (err or "") + (out or "")

    # ── Retry 2: AGP terlalu rendah ────────────────────────────────────────────
    agp_required = _parse_agp_from_error(combined)
    if agp_required:
        logs.append(f"Auto-fix: AGP conflict — dependency perlukan AGP >= {agp_required}")
        await fix_flutter_versions(project_dir, logs, required_agp_override=agp_required)
        await run_cmd("flutter pub get", cwd=project_dir, timeout=300)
        code, out, err = await run_cmd("flutter build apk --debug", cwd=project_dir)
        if code == 0:
            return code, out, err
        combined = (err or "") + (out or "")

    # ── Retry 2.5: Kotlin/Java JVM-target conflict (plugin pihak ketiga cth
    # package_info_plus/share_plus belum migrate ke Flutter Built-in Kotlin) ───
    jvm_mismatch = _parse_jvm_kotlin_mismatch(combined)
    kgp_warning = _has_kgp_plugin_warning(combined)
    if jvm_mismatch or kgp_warning:
        # Percubaan 1 (paling ringan, tak paksa/tukar versi apa-apa):
        # benarkan Gradle papar amaran je untuk perbezaan JVM-target ni.
        if _set_kotlin_jvm_validation_warning(android_dir, logs):
            code, out, err = await run_cmd("flutter build apk --debug", cwd=project_dir)
            if code == 0:
                return code, out, err
            combined = (err or "") + (out or "")

        # Percubaan 2 (kalau masih gagal — bermakna konflik binari sebenar,
        # bukan sekadar validation check): turunkan AGP ke generasi
        # sebelumnya yang adaptif ikut AGP projek yang dikesan.
        jvm_mismatch = _parse_jvm_kotlin_mismatch(combined)
        kgp_warning = _has_kgp_plugin_warning(combined)
        if jvm_mismatch or kgp_warning:
            if await _fallback_downgrade_agp_for_legacy_plugins(project_dir, android_dir, logs):
                await run_cmd("flutter pub get", cwd=project_dir, timeout=300)
                code, out, err = await run_cmd("flutter build apk --debug", cwd=project_dir)
                if code == 0:
                    return code, out, err
                combined = (err or "") + (out or "")

    # ── Retry 3: Gradle distribution gagal dimuat turun (mirror projek rosak) ──
    gradle_dist_fail = _parse_gradle_dist_error(combined)
    if gradle_dist_fail:
        version, dist_type = gradle_dist_fail
        props_path = os.path.join(android_dir, "gradle", "wrapper", "gradle-wrapper.properties")
        if _force_official_gradle_url(props_path, version, dist_type, logs):
            code, out, err = await run_cmd("flutter build apk --debug", cwd=project_dir)

    return code, out, err


async def _gradle_build_with_retry(build_dir, gcmd, logs, project_dir=None):
    """
    Jalankan assembleDebug dengan auto-retry untuk semua jenis projek Native.
    Pass 1: Build terus.
    Pass 2: Detect NDK conflict → fix → retry.
    Pass 3: Detect AGP conflict → fix → retry.
    Pass 4: Detect Gradle distribution gagal dimuat turun (mirror projek down/404)
            → tukar ke services.gradle.org → retry.
    project_dir: root projek (untuk fix AGP dalam android/ subdir), default = build_dir
    """
    root = project_dir or build_dir

    code, out, err = await run_cmd(f"{gcmd} assembleDebug --stacktrace", cwd=build_dir)
    if code == 0:
        return code, out, err

    combined = (err or "") + (out or "")

    # Retry 1: NDK conflict
    ndk_required = _parse_ndk_from_error(combined)
    if ndk_required:
        logs.append(f"Auto-fix: NDK conflict — perlukan: {', '.join(sorted(ndk_required))}")
        # android_dir boleh jadi build_dir atau parent/android
        android_candidate = build_dir if os.path.exists(os.path.join(build_dir, "app")) else root
        await _ensure_ndk_smart(android_candidate, logs, extra_required=ndk_required)
        code, out, err = await run_cmd(f"{gcmd} assembleDebug --stacktrace", cwd=build_dir)
        if code == 0:
            return code, out, err
        combined = (err or "") + (out or "")

    # Retry 2: AGP conflict
    agp_required = _parse_agp_from_error(combined)
    if agp_required:
        logs.append(f"Auto-fix: AGP conflict — dependency perlukan AGP >= {agp_required}")
        await fix_common_issues(root, logs)  # re-run fix dengan AGP baru
        code, out, err = await run_cmd(f"{gcmd} assembleDebug --stacktrace", cwd=build_dir)
        if code == 0:
            return code, out, err
        combined = (err or "") + (out or "")

    # Retry 3: Gradle distribution gagal dimuat turun (mirror projek rosak)
    gradle_dist_fail = _parse_gradle_dist_error(combined)
    if gradle_dist_fail:
        version, dist_type = gradle_dist_fail
        android_candidate = build_dir if os.path.exists(os.path.join(build_dir, "app")) else root
        props_path = os.path.join(android_candidate, "gradle", "wrapper", "gradle-wrapper.properties")
        if _force_official_gradle_url(props_path, version, dist_type, logs):
            code, out, err = await run_cmd(f"{gcmd} assembleDebug --stacktrace", cwd=build_dir)

    return code, out, err


async def build_native(project_dir, config):
    logs = []
    await setup_java(config.get("java_version", "11"))
    logs.append(f"Java {config.get('java_version','11')} ready")
    await setup_android_sdk(config.get("compile_sdk"), config.get("build_tools"))
    logs.append("Android SDK ready")

    await fix_common_issues(project_dir, logs)

    gradlew = os.path.join(project_dir, "gradlew")
    if os.path.exists(gradlew):
        await run_cmd(f"chmod +x {gradlew}")
        gcmd = "./gradlew"
    else:
        gcmd = None
        inst_dir = "/tmp/gradle-inst"
        if os.path.isdir(inst_dir):
            for entry in os.listdir(inst_dir):
                candidate = os.path.join(inst_dir, entry, "bin", "gradle")
                if os.path.exists(candidate):
                    gcmd = candidate
                    logs.append(f"Auto-fix: using {entry} binary (gradlew unavailable)")
                    break
        if not gcmd:
            return {"success": False, "error": "No gradlew found and could not install gradle.", "logs": logs}

    code, out, err = await _gradle_build_with_retry(project_dir, gcmd, logs)
    logs.append(f"assembleDebug: {'OK' if code == 0 else 'FAIL'}")
    if code != 0:
        return {"success": False, "error": f"Debug build failed\n{err}\n{out}", "logs": logs}

    code2, out2, err2 = await run_cmd(f"{gcmd} assembleRelease --stacktrace", cwd=project_dir)
    logs.append(f"assembleRelease: {'OK' if code2 == 0 else 'FAIL'}")
    code3, out3, err3 = await run_cmd(f"{gcmd} bundleRelease --stacktrace", cwd=project_dir)
    logs.append(f"bundleRelease: {'OK' if code3 == 0 else 'FAIL'}")

    files = []
    out_base = os.path.join(project_dir, "app", "build", "outputs")
    if os.path.exists(out_base):
        for root, _, fnames in os.walk(out_base):
            for fn in fnames:
                if fn.endswith((".apk", ".aab")):
                    files.append(os.path.join(root, fn))
    if not files:
        return {"success": False, "error": f"No output files found.\n{err}\n{err2}\n{err3}", "logs": logs}
    return {"success": True, "files": files, "logs": logs}


async def build_flutter(project_dir, config):
    logs = []
    await setup_java(config.get("java_version", "17"))
    logs.append(f"Java {config.get('java_version','17')} ready")

    flutter_version = config.get("flutter_version", "stable")
    ok = await setup_flutter(flutter_version)
    if not ok:
        return {"success": False, "error": "Flutter SDK install failed", "logs": logs}
    logs.append(f"Flutter {flutter_version} ready")

    # ── Step 0: scaffold folder android/ kalau tiada / tak lengkap ─────────────
    # Sesetengah projek (source-only zip) sengaja tak sertakan folder android/,
    # ios/ dsb (generated locally by convention). Kalau terus biar fix_common_issues
    # jalan atas folder android/ yang tiada/tak lengkap, ia cuma patch sebahagian
    # fail (gradle-wrapper.properties, local.properties) tanpa MainActivity/
    # AndroidManifest/build.gradle yang betul — punca error "deleted v1 embedding".
    android_dir = os.path.join(project_dir, "android")
    manifest_path = os.path.join(android_dir, "app", "src", "main", "AndroidManifest.xml")
    build_gradle_path = os.path.join(android_dir, "app", "build.gradle")
    build_gradle_kts_path = os.path.join(android_dir, "app", "build.gradle.kts")
    settings_gradle_path = os.path.join(android_dir, "settings.gradle")
    settings_gradle_kts_path = os.path.join(android_dir, "settings.gradle.kts")
    android_incomplete = (
        not os.path.isdir(android_dir)
        or not os.path.exists(manifest_path)
        or not (os.path.exists(build_gradle_path) or os.path.exists(build_gradle_kts_path))
        or not (os.path.exists(settings_gradle_path) or os.path.exists(settings_gradle_kts_path))
    )
    if android_incomplete:
        logs.append("Auto-fix: folder android/ tiada atau tak lengkap, generate guna 'flutter create .'")
        code0, out0, err0 = await run_cmd("flutter create .", cwd=project_dir, timeout=180)
        if code0 != 0:
            return {"success": False, "error": f"flutter create . gagal\n{err0}\n{out0}", "logs": logs}

    await fix_common_issues(project_dir, logs, "android")

    old_style = config.get("old_flutter_style", False)

    if not old_style:
        await fix_flutter_versions(project_dir, logs)
    else:
        logs.append("Info: old Flutter style detected, skipping AGP upgrade")
        # Walaupun old style, tetap fix NDK kalau ada conflict
        if os.path.isdir(android_dir):
            await _ensure_ndk_smart(android_dir, logs)

    gw = os.path.join(android_dir, "gradlew")
    if os.path.exists(gw):
        await run_cmd(f"chmod +x {gw}")

    code, out, err = await run_cmd("flutter pub get", cwd=project_dir, timeout=300)
    logs.append(f"pub get: {'OK' if code == 0 else 'FAIL'}")
    if code != 0:
        return {"success": False, "error": f"flutter pub get failed\n{err}\n{out}", "logs": logs}

    # Build dengan auto-retry untuk NDK dan AGP conflicts
    code, out, err = await _build_flutter_with_retry(project_dir, logs, android_dir)
    logs.append(f"apk debug: {'OK' if code == 0 else 'FAIL'}")
    if code != 0:
        return {"success": False, "error": f"Debug build failed\n{err}\n{out}", "logs": logs}

    code2, out2, err2 = await run_cmd("flutter build apk --release", cwd=project_dir)
    logs.append(f"apk release: {'OK' if code2 == 0 else 'FAIL'}")
    code3, out3, err3 = await run_cmd("flutter build appbundle --release", cwd=project_dir)
    logs.append(f"appbundle: {'OK' if code3 == 0 else 'FAIL'}")

    files = []
    for search_root in [
        os.path.join(project_dir, "build", "app", "outputs"),
        os.path.join(project_dir, "build", "outputs"),
    ]:
        if os.path.exists(search_root):
            for root, _, fnames in os.walk(search_root):
                for fn in fnames:
                    if fn.endswith((".apk", ".aab")):
                        files.append(os.path.join(root, fn))
    if not files:
        return {"success": False, "error": f"No output files found.\n{err}\n{err2}\n{err3}", "logs": logs}
    return {"success": True, "files": files, "logs": logs}


def _find_zipalign():
    if shutil.which("zipalign"):
        return "zipalign"
    ah = os.environ.get("ANDROID_HOME", "/usr/local/lib/android/sdk")
    bt_dir = os.path.join(ah, "build-tools")
    if os.path.isdir(bt_dir):
        for ver in sorted(os.listdir(bt_dir), reverse=True):
            za = os.path.join(bt_dir, ver, "zipalign")
            if os.path.exists(za):
                return za
    return None


def _fix_apktool_donotcompress(project_dir):
    yml_path = os.path.join(project_dir, "apktool.yml")
    if not os.path.exists(yml_path):
        return False
    lib_dir = os.path.join(project_dir, "lib")
    if not os.path.isdir(lib_dir):
        return False
    has_so = any(f.endswith(".so") for _, _, files in os.walk(lib_dir) for f in files)
    if not has_so:
        return False
    with open(yml_path, "r") as f:
        content = f.read()
    if re.search(r'-\s*["\']?\.?so["\']?\s*$', content, re.MULTILINE):
        return False
    if "doNotCompress:" in content:
        content = re.sub(r'(doNotCompress:\n)', r'\1- .so\n', content)
    else:
        content += "\ndoNotCompress:\n- .so\n"
    with open(yml_path, "w") as f:
        f.write(content)
    return True


def _find_apksigner():
    ah = os.environ.get("ANDROID_HOME", "/usr/local/lib/android/sdk")
    bt_dir = os.path.join(ah, "build-tools")
    if os.path.isdir(bt_dir):
        for ver in sorted(os.listdir(bt_dir), reverse=True):
            path = os.path.join(bt_dir, ver, "apksigner")
            if os.path.exists(path):
                return path
    return None


async def _ensure_debug_keystore():
    ks_path = "/tmp/debug-sign.jks"
    if os.path.exists(ks_path):
        return ks_path
    code, _, _ = await run_cmd(
        'keytool -genkeypair -v -keystore ' + ks_path +
        ' -alias debug -keyalg RSA -keysize 2048 -validity 10000'
        ' -storepass android -keypass android'
        ' -dname "CN=Debug,O=Debug,C=US"',
        timeout=30,
    )
    return ks_path if code == 0 and os.path.exists(ks_path) else None


async def _sign_apk(apk_path, keystore, apksigner_bin, logs):
    code, _, err = await run_cmd(
        f'"{apksigner_bin}" sign --ks "{keystore}" --ks-key-alias debug'
        f' --ks-pass pass:android --key-pass pass:android "{apk_path}"',
        timeout=120,
    )
    if code == 0:
        logs.append(f"Signed: {os.path.basename(apk_path)}")
        return True
    logs.append(f"Sign FAIL: {os.path.basename(apk_path)} — {(err or '')[:200]}")
    return False


def _strip_apk_signatures(apk_path):
    tmp_path = apk_path + '.unsigned'
    try:
        with zipfile.ZipFile(apk_path, 'r') as zin:
            with zipfile.ZipFile(tmp_path, 'w') as zout:
                for item in zin.infolist():
                    if not item.filename.upper().startswith('META-INF/'):
                        zout.writestr(item, zin.read(item.filename))
        os.replace(tmp_path, apk_path)
        return True
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False


def _find_splits_dir(project_dir):
    search_roots = [project_dir, os.path.dirname(project_dir)]
    for root in search_roots:
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            candidate = os.path.join(root, name)
            if not os.path.isdir(candidate):
                continue
            has_split_apks = any(
                f.lower().startswith("split_") and f.lower().endswith(".apk")
                for f in os.listdir(candidate)
            )
            if has_split_apks:
                return candidate
    return None


async def _package_as_apks(base_apk, splits_dir, output_path, zipalign_bin, apksigner_bin, keystore, logs):
    split_apks = sorted([os.path.join(splits_dir, f) for f in os.listdir(splits_dir) if f.lower().endswith('.apk')])
    if not split_apks:
        logs.append("splits/: no APK files found, skipping APKS packaging")
        return None
    stripped = sum(1 for sa in split_apks if _strip_apk_signatures(sa))
    if stripped:
        logs.append(f"Stripped signatures from {stripped} split APK(s)")
    if zipalign_bin:
        for sa in split_apks:
            aligned = sa + '.aligned'
            code, _, _ = await run_cmd(f'"{zipalign_bin}" -p -f 4 "{sa}" "{aligned}"', timeout=60)
            if code == 0 and os.path.exists(aligned):
                os.replace(aligned, sa)
    if apksigner_bin and keystore:
        for sa in split_apks:
            await _sign_apk(sa, keystore, apksigner_bin, logs)
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_STORED) as zf:
        zf.write(base_apk, 'base.apk')
        for sa in split_apks:
            zf.write(sa, os.path.basename(sa))
    logs.append(f"APKS packaged: base.apk + {len(split_apks)} split(s)")
    return output_path


async def build_smali(project_dir, config):
    logs = []
    await setup_java(config.get("java_version", "17"))
    logs.append(f"Java {config.get('java_version', '17')} ready")

    if _fix_apktool_donotcompress(project_dir):
        logs.append("Auto-fix: added .so to doNotCompress (apktool.yml)")

    code, out, err = await run_cmd("apktool b . --use-aapt2", cwd=project_dir)
    logs.append(f"apktool build (aapt2): {'OK' if code == 0 else 'FAIL'}")
    if code != 0:
        code, out, err = await run_cmd("apktool b .", cwd=project_dir)
        logs.append(f"apktool build (aapt1 fallback): {'OK' if code == 0 else 'FAIL'}")
    if code != 0:
        return {"success": False, "error": f"Smali build failed\n{err}\n{out}", "logs": logs}

    files = [os.path.join(os.path.join(project_dir, "dist"), fn)
             for fn in os.listdir(os.path.join(project_dir, "dist"))
             if fn.endswith(".apk")] if os.path.exists(os.path.join(project_dir, "dist")) else []
    if not files:
        return {"success": False, "error": f"No output APK found in dist/\n{err}", "logs": logs}

    zipalign = _find_zipalign()
    if zipalign:
        for apk_path in files:
            aligned_path = apk_path + ".aligned"
            zcode, _, zerr = await run_cmd(f'"{zipalign}" -p -f 4 "{apk_path}" "{aligned_path}"', timeout=120)
            if zcode == 0 and os.path.exists(aligned_path):
                os.replace(aligned_path, apk_path)
                logs.append(f"zipalign: OK ({os.path.basename(apk_path)})")
            else:
                logs.append(f"zipalign: FAIL ({zerr[:200] if zerr else 'unknown'})")
    else:
        logs.append("zipalign: not found (skipped)")

    apksigner = _find_apksigner()
    keystore = await _ensure_debug_keystore()
    signed = False
    if apksigner and keystore:
        signed = all([await _sign_apk(p, keystore, apksigner, logs) for p in files])
    else:
        logs.append("apksigner/keystore: tidak tersedia (signing skipped)")

    splits_dir = _find_splits_dir(project_dir)
    if splits_dir:
        logs.append(f"Found split APKs in: {os.path.basename(splits_dir)}/")
        apks_name = os.path.splitext(os.path.basename(files[0]))[0] + ".apks"
        apks_path = os.path.join(os.path.dirname(files[0]), apks_name)
        packaged = await _package_as_apks(files[0], splits_dir, apks_path, zipalign, apksigner, keystore, logs)
        if packaged:
            return {"success": True, "files": [packaged], "logs": logs, "output_format": "apks", "signed": signed}

    return {"success": True, "files": files, "logs": logs, "signed": signed}


async def build_react_native(project_dir, config):
    logs = []
    ok = await setup_node(config.get("node_version", "20"))
    if not ok:
        return {"success": False, "error": "Node.js setup gagal", "logs": logs}
    logs.append(f"Node.js {config.get('node_version','20')} ready")
    await setup_java(config.get("java_version", "17"))
    logs.append(f"Java {config.get('java_version','17')} ready")
    await setup_android_sdk()
    logs.append("Android SDK ready")
    ok = await _install_node_deps(project_dir, logs)
    if not ok:
        return {"success": False, "error": "npm/yarn install gagal", "logs": logs}
    android_dir = os.path.join(project_dir, "android")
    if not os.path.isdir(android_dir):
        return {"success": False, "error": "Folder android/ tidak jumpa dalam projek React Native", "logs": logs}
    await fix_common_issues(android_dir, logs)
    gradlew = os.path.join(android_dir, "gradlew")
    if os.path.exists(gradlew):
        await run_cmd(f"chmod +x {gradlew}")
    code, out, err = await _gradle_build_with_retry(android_dir, "./gradlew", logs, project_dir=project_dir)
    logs.append(f"assembleDebug: {'OK' if code == 0 else 'FAIL'}")
    if code != 0:
        return {"success": False, "error": f"Debug build gagal\n{err}\n{out}", "logs": logs}
    code2, out2, err2 = await run_cmd("./gradlew assembleRelease --stacktrace", cwd=android_dir)
    logs.append(f"assembleRelease: {'OK' if code2 == 0 else 'FAIL'}")
    code3, out3, err3 = await run_cmd("./gradlew bundleRelease --stacktrace", cwd=android_dir)
    logs.append(f"bundleRelease: {'OK' if code3 == 0 else 'FAIL'}")
    files = _collect_apks([os.path.join(android_dir, "app", "build", "outputs")])
    if not files:
        return {"success": False, "error": f"Tiada output APK/AAB\n{err}\n{err2}", "logs": logs}
    return {"success": True, "files": files, "logs": logs}


async def build_cordova(project_dir, config):
    logs = []
    ok = await setup_node(config.get("node_version", "20"))
    if not ok:
        return {"success": False, "error": "Node.js setup gagal", "logs": logs}
    logs.append(f"Node.js {config.get('node_version','20')} ready")
    await setup_java(config.get("java_version", "17"))
    logs.append(f"Java {config.get('java_version','17')} ready")
    await setup_android_sdk()
    logs.append("Android SDK ready")
    code, _, err = await run_cmd("npm install -g cordova", timeout=300)
    logs.append(f"cordova install: {'OK' if code == 0 else 'FAIL'}")
    if code != 0:
        return {"success": False, "error": f"Cordova CLI install gagal\n{err}", "logs": logs}
    await _install_node_deps(project_dir, logs)
    android_platform = os.path.join(project_dir, "platforms", "android")
    if not os.path.isdir(android_platform):
        code, out, err = await run_cmd("cordova platform add android", cwd=project_dir, timeout=300)
        logs.append(f"platform add android: {'OK' if code == 0 else 'FAIL'}")
        if code != 0:
            return {"success": False, "error": f"Gagal tambah platform android\n{err}\n{out}", "logs": logs}
    if os.path.isdir(android_platform):
        await fix_common_issues(android_platform, logs)
    code, out, err = await run_cmd("cordova build android --debug", cwd=project_dir, timeout=900)
    logs.append(f"cordova build debug: {'OK' if code == 0 else 'FAIL'}")
    if code != 0:
        return {"success": False, "error": f"Cordova debug build gagal\n{err}\n{out}", "logs": logs}
    code2, out2, err2 = await run_cmd("cordova build android --release", cwd=project_dir, timeout=900)
    logs.append(f"cordova build release: {'OK' if code2 == 0 else 'FAIL'}")
    files = _collect_apks([
        os.path.join(project_dir, "platforms", "android", "app", "build", "outputs"),
        os.path.join(project_dir, "platforms", "android", "build", "outputs"),
    ])
    if not files:
        return {"success": False, "error": f"Tiada output APK\n{err}\n{err2}", "logs": logs}
    return {"success": True, "files": files, "logs": logs}


async def build_ionic(project_dir, config):
    logs = []
    ok = await setup_node(config.get("node_version", "20"))
    if not ok:
        return {"success": False, "error": "Node.js setup gagal", "logs": logs}
    logs.append(f"Node.js {config.get('node_version','20')} ready")
    await setup_java(config.get("java_version", "17"))
    logs.append(f"Java {config.get('java_version','17')} ready")
    await setup_android_sdk()
    logs.append("Android SDK ready")
    await run_cmd("npm install -g @ionic/cli", timeout=300)
    logs.append("Ionic CLI ready")
    ok = await _install_node_deps(project_dir, logs)
    if not ok:
        return {"success": False, "error": "npm/yarn install gagal", "logs": logs}

    is_capacitor = any(os.path.exists(os.path.join(project_dir, f)) for f in [
        "capacitor.config.json", "capacitor.config.ts", "capacitor.config.js"
    ])

    if is_capacitor:
        logs.append("Detected: Ionic + Capacitor")
        code, out, err = await run_cmd("ionic build --prod || ionic build", cwd=project_dir, timeout=600)
        logs.append(f"ionic build: {'OK' if code == 0 else 'FAIL'}")
        if code != 0:
            code, out, err = await run_cmd("npx ng build --configuration production || npx ng build", cwd=project_dir, timeout=600)
            logs.append(f"ng build fallback: {'OK' if code == 0 else 'FAIL'}")
            if code != 0:
                return {"success": False, "error": f"Web build gagal\n{err}\n{out}", "logs": logs}
        code, out, err = await run_cmd("npx cap sync android", cwd=project_dir, timeout=300)
        logs.append(f"cap sync: {'OK' if code == 0 else 'FAIL'}")
        android_dir = os.path.join(project_dir, "android")
        if not os.path.isdir(android_dir):
            code, out, err = await run_cmd("npx cap add android", cwd=project_dir, timeout=300)
            logs.append(f"cap add android: {'OK' if code == 0 else 'FAIL'}")
        if os.path.isdir(android_dir):
            await fix_common_issues(android_dir, logs)
            gradlew = os.path.join(android_dir, "gradlew")
            if os.path.exists(gradlew):
                await run_cmd(f"chmod +x {gradlew}")
            code, out, err = await _gradle_build_with_retry(android_dir, "./gradlew", logs, project_dir=project_dir)
            logs.append(f"assembleDebug: {'OK' if code == 0 else 'FAIL'}")
            if code != 0:
                return {"success": False, "error": f"Gradle build gagal\n{err}\n{out}", "logs": logs}
            code2, _, _ = await run_cmd("./gradlew assembleRelease --stacktrace", cwd=android_dir)
            logs.append(f"assembleRelease: {'OK' if code2 == 0 else 'FAIL'}")
            code3, _, _ = await run_cmd("./gradlew bundleRelease --stacktrace", cwd=android_dir)
            logs.append(f"bundleRelease: {'OK' if code3 == 0 else 'FAIL'}")
        search_dirs = [os.path.join(project_dir, "android", "app", "build", "outputs")]
    else:
        logs.append("Detected: Ionic + Cordova")
        await run_cmd("npm install -g cordova", timeout=300)
        android_platform = os.path.join(project_dir, "platforms", "android")
        if not os.path.isdir(android_platform):
            code, out, err = await run_cmd("ionic cordova platform add android", cwd=project_dir, timeout=300)
            logs.append(f"platform add: {'OK' if code == 0 else 'FAIL'}")
            if code != 0:
                return {"success": False, "error": f"Gagal tambah platform\n{err}\n{out}", "logs": logs}
        if os.path.isdir(android_platform):
            await fix_common_issues(android_platform, logs)
        code, out, err = await run_cmd("ionic cordova build android --prod", cwd=project_dir, timeout=900)
        logs.append(f"ionic cordova build: {'OK' if code == 0 else 'FAIL'}")
        if code != 0:
            return {"success": False, "error": f"Ionic Cordova build gagal\n{err}\n{out}", "logs": logs}
        search_dirs = [
            os.path.join(project_dir, "platforms", "android", "app", "build", "outputs"),
            os.path.join(project_dir, "platforms", "android", "build", "outputs"),
        ]

    files = _collect_apks(search_dirs)
    if not files:
        return {"success": False, "error": "Tiada output APK/AAB dijumpai", "logs": logs}
    return {"success": True, "files": files, "logs": logs}


async def build_capacitor(project_dir, config):
    logs = []
    ok = await setup_node(config.get("node_version", "20"))
    if not ok:
        return {"success": False, "error": "Node.js setup gagal", "logs": logs}
    logs.append(f"Node.js {config.get('node_version','20')} ready")
    await setup_java(config.get("java_version", "17"))
    logs.append(f"Java {config.get('java_version','17')} ready")
    await setup_android_sdk()
    logs.append("Android SDK ready")
    ok = await _install_node_deps(project_dir, logs)
    if not ok:
        return {"success": False, "error": "npm/yarn install gagal", "logs": logs}
    code, out, err = await run_cmd("npx cap sync android", cwd=project_dir, timeout=300)
    logs.append(f"cap sync: {'OK' if code == 0 else 'FAIL'}")
    android_dir = os.path.join(project_dir, "android")
    if not os.path.isdir(android_dir):
        code, out, err = await run_cmd("npx cap add android", cwd=project_dir, timeout=300)
        logs.append(f"cap add android: {'OK' if code == 0 else 'FAIL'}")
        if code != 0:
            return {"success": False, "error": f"Gagal add platform android\n{err}\n{out}", "logs": logs}
    await fix_common_issues(android_dir, logs)
    gradlew = os.path.join(android_dir, "gradlew")
    if os.path.exists(gradlew):
        await run_cmd(f"chmod +x {gradlew}")
    code, out, err = await _gradle_build_with_retry(android_dir, "./gradlew", logs, project_dir=project_dir)
    logs.append(f"assembleDebug: {'OK' if code == 0 else 'FAIL'}")
    if code != 0:
        return {"success": False, "error": f"Gradle build gagal\n{err}\n{out}", "logs": logs}
    code2, _, err2 = await run_cmd("./gradlew assembleRelease --stacktrace", cwd=android_dir)
    logs.append(f"assembleRelease: {'OK' if code2 == 0 else 'FAIL'}")
    code3, _, err3 = await run_cmd("./gradlew bundleRelease --stacktrace", cwd=android_dir)
    logs.append(f"bundleRelease: {'OK' if code3 == 0 else 'FAIL'}")
    files = _collect_apks([os.path.join(android_dir, "app", "build", "outputs")])
    if not files:
        return {"success": False, "error": f"Tiada output APK/AAB\n{err}\n{err2}", "logs": logs}
    return {"success": True, "files": files, "logs": logs}


async def build_project(project_dir, project_info):
    t = project_info["type"]
    c = project_info["config"]
    if t == "native":    return await build_native(project_dir, c)
    if t == "flutter":   return await build_flutter(project_dir, c)
    if t == "smali":     return await build_smali(project_dir, c)
    if t == "react_native": return await build_react_native(project_dir, c)
    if t == "cordova":   return await build_cordova(project_dir, c)
    if t == "ionic":     return await build_ionic(project_dir, c)
    if t == "capacitor": return await build_capacitor(project_dir, c)
    return {"success": False, "error": f"Unknown project type: {t}"}


async def upload_to_gofile(filepath):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.gofile.io/servers") as r:
                data = await r.json()
                server = data["data"]["servers"][0]["name"]
            url = f"https://{server}.gofile.io/contents/uploadfile"
            form = aiohttp.FormData()
            form.add_field("file", open(filepath, "rb"), filename=os.path.basename(filepath))
            async with s.post(url, data=form) as r:
                res = await r.json()
                if res.get("status") == "ok":
                    return res["data"]["downloadPage"]
    except Exception as e:
        logger.error(f"GoFile upload failed: {e}")
    return None
