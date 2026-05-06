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
    homes = {
        "17": "/usr/lib/jvm/java-17-openjdk-amd64",
        "11": "/usr/lib/jvm/java-11-openjdk-amd64",
        "8":  "/usr/lib/jvm/java-8-openjdk-amd64",
    }
    home = homes.get(version, homes["17"])
    if os.path.exists(home):
        os.environ["JAVA_HOME"] = home
        os.environ["PATH"] = f"{home}/bin:{os.environ['PATH']}"
        return True
    
    # Try install if not found (especially for Java 8)
    code, _, _ = await run_cmd(f"sudo add-apt-repository -y ppa:openjdk-r/ppa && sudo apt-get update -qq && sudo apt-get install -y -qq openjdk-{version}-jdk")
    if code == 0 and os.path.exists(home):
        os.environ["JAVA_HOME"] = home
        os.environ["PATH"] = f"{home}/bin:{os.environ['PATH']}"
        return True
    return False


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
    """Pastikan Node.js tersedia. Guna versi yang dah ada dalam runner dulu,
    install via nvm kalau tak jumpa atau versi terlalu lama."""
    # Check kalau node dah ada dan versi mencukupi
    code, out, _ = await run_cmd("node --version")
    if code == 0:
        try:
            current = int(out.strip().lstrip("v").split(".")[0])
            if current >= int(version):
                logger.info(f"Node.js {out.strip()} already available")
                return True
        except Exception:
            pass

    # Cuba install via nvm (paling reliable dalam GitHub Actions)
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

    # Update PATH supaya node baru accessible
    node_bin = os.path.expanduser(f"~/.nvm/versions/node/v{version}/bin")
    if not os.path.isdir(node_bin):
        # Cari versi yang betul-betul dipasang
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
    """Detect sama ada project guna npm atau yarn."""
    if os.path.exists(os.path.join(project_dir, "yarn.lock")):
        return "yarn"
    return "npm"


async def _install_node_deps(project_dir, logs):
    """Install node dependencies. Cuba yarn dulu kalau ada yarn.lock, fallback ke npm."""
    pm = await _detect_package_manager(project_dir)
    if pm == "yarn":
        code, out, err = await run_cmd("yarn install --frozen-lockfile || yarn install", cwd=project_dir, timeout=600)
    else:
        code, out, err = await run_cmd("npm install --legacy-peer-deps", cwd=project_dir, timeout=600)

    if code != 0:
        logs.append(f"Warning: {pm} install ada warning, cuba teruskan...")
        # Cuba sekali lagi tanpa flag ketat
        pm_cmd = "yarn install" if pm == "yarn" else "npm install --force"
        code, out, err = await run_cmd(pm_cmd, cwd=project_dir, timeout=600)

    logs.append(f"{pm} install: {'OK' if code == 0 else 'FAIL'}")
    return code == 0


def _collect_apks(search_dirs):
    """Collect semua .apk dan .aab dari senarai directory."""
    files = []
    for d in search_dirs:
        if os.path.exists(d):
            for root, _, fnames in os.walk(d):
                for fn in fnames:
                    if fn.endswith((".apk", ".aab")):
                        files.append(os.path.join(root, fn))
    return files


# ================================================================
# SHARED HELPERS — digunakan oleh fix_common_issues DAN fix_flutter_versions
# ================================================================

def _ver_tuple(v):
    """Convert version string '1.2.3' ke tuple (1, 2, 3) untuk comparison."""
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return (0,)


def _agp_to_min_gradle(agp_ver: str) -> str:
    """Pulangkan minimum Gradle version yang diperlukan untuk sesuatu AGP version.
    Covers semua AGP dari 1.x sampai 9.x berdasarkan official Android documentation.
    AGP 5.x dan 6.x tidak pernah di-release secara stable — treated as 7.0 baseline."""
    try:
        parts = agp_ver.split(".")
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return "8.7"

    if major <= 2:
        return "2.14.1"
    elif major == 3:
        if minor == 0:   return "4.1"
        elif minor == 1: return "4.4"
        elif minor == 2: return "4.6"
        elif minor == 3: return "4.10.1"
        elif minor == 4: return "5.1.1"
        elif minor == 5: return "5.4.1"
        else:            return "5.6.4"    # 3.6.x
    elif major == 4:
        if minor == 0:   return "6.1.1"
        elif minor == 1: return "6.5"
        else:            return "6.7.1"    # 4.2.x+
    elif major in (5, 6):
        # Tidak pernah stable release — fallback ke 7.0
        return "7.0"
    elif major == 7:
        if minor == 0:   return "7.0"
        elif minor == 1: return "7.2"
        elif minor == 2: return "7.3.3"
        elif minor == 3: return "7.4"
        else:            return "7.5"      # 7.4.x+
    elif major == 8:
        if minor <= 1:   return "8.0"
        elif minor == 2: return "8.2"
        elif minor == 3: return "8.4"
        elif minor == 4: return "8.6"
        elif minor <= 6: return "8.7"
        elif minor == 7: return "8.9"
        elif minor == 8: return "8.10.2"
        else:            return "8.11.1"   # 8.9.x+
    else:
        return "8.11.1"                    # AGP 9.x+


def _fix_repositories_in_file(file_path, logs):
    """Ganti jcenter() yang sudah mati dengan mavenCentral() dalam satu fail gradle."""
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
    """Scan semua .gradle dan .gradle.kts dalam directory dan fix jcenter."""
    skip_dirs = {".git", "build", ".gradle", "node_modules", ".dart_tool"}
    for dirpath, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if fn.endswith((".gradle", ".gradle.kts")):
                _fix_repositories_in_file(os.path.join(dirpath, fn), logs)


def _fix_version_catalog(root_dir, logs, min_agp, min_kotlin, min_compile, min_target, min_min_sdk):
    """Fix versi dalam libs.versions.toml (Gradle Version Catalog).
    Supports kedua-dua android/ dan project root level."""
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

            def fix_toml_str_ver(text, key, min_ver):
                """Fix string version: agp = "7.3.0" -> "8.1.1" """
                pat = rf'(?m)^(\s*{key}\s*=\s*["\x27])([0-9][0-9.]*[0-9])(["\x27])'
                def replacer(m):
                    cur = m.group(2)
                    try:
                        if _ver_tuple(cur) < _ver_tuple(min_ver):
                            return m.group(1) + min_ver + m.group(3)
                    except Exception:
                        pass
                    return m.group(0)
                return re.sub(pat, replacer, text, flags=re.IGNORECASE)

            def fix_toml_int_ver(text, key, min_val):
                """Fix integer version: compileSdk = 31 -> 34 """
                pat = rf'(?m)^(\s*{key}\s*=\s*)(\d+)'
                def replacer(m):
                    val = int(m.group(2))
                    return m.group(1) + str(max(val, min_val))
                return re.sub(pat, replacer, text, flags=re.IGNORECASE)

            # Fix semua versi yang berkaitan
            content = fix_toml_str_ver(content, "agp",            min_agp)
            content = fix_toml_str_ver(content, "kotlin",         min_kotlin)
            content = fix_toml_str_ver(content, "kotlin.version", min_kotlin)
            content = fix_toml_str_ver(content, "kotlinVersion",  min_kotlin)
            content = fix_toml_int_ver(content, "compileSdk",     min_compile)
            content = fix_toml_int_ver(content, "targetSdk",      min_target)
            content = fix_toml_int_ver(content, "minSdk",         min_min_sdk)

            if content != original:
                with open(toml_path, "w", encoding="utf-8") as f:
                    f.write(content)
                logs.append(f"Auto-fix: versions updated dalam {os.path.basename(toml_path)}")
        except Exception:
            pass


def _bump_sdk_in_file(file_path, logs, min_compile, min_target, min_min_sdk):
    """Bump compileSdk/targetSdk/minSdk dalam mana-mana build.gradle / build.gradle.kts.
    Skip kalau nilai adalah variable reference (bukan literal integer)."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        original = content

        def bump(text, key, min_val):
            pat = rf'(\b{key}\s*[=:]?\s*)(\d+)'
            def replacer(m):
                val = int(m.group(2))
                return m.group(1) + str(max(val, min_val))
            return re.sub(pat, replacer, text)

        content = bump(content, "compileSdk",        min_compile)
        content = bump(content, "compileSdkVersion", min_compile)
        content = bump(content, "targetSdk",         min_target)
        content = bump(content, "targetSdkVersion",  min_target)
        content = bump(content, "minSdk",            min_min_sdk)
        content = bump(content, "minSdkVersion",     min_min_sdk)

        if content != original:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            logs.append(f"Auto-fix: SDK versions updated dalam {os.path.basename(file_path)}")
    except Exception:
        pass


def _fix_kotlin_in_file(file_path, logs, min_kotlin):
    """Fix Kotlin version dalam mana-mana gradle file."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        original = content

        def replacer(m):
            cur = m.group(2)
            try:
                if _ver_tuple(cur) < _ver_tuple(min_kotlin):
                    return m.group(1) + min_kotlin + m.group(3)
            except Exception:
                pass
            return m.group(0)

        # kotlin_version = "x.x.x" (Groovy style)
        content = re.sub(
            r'(kotlin[_\-]version\s*=\s*["\x27])([0-9.]+)(["\x27])',
            replacer, content, flags=re.IGNORECASE
        )
        # id("org.jetbrains.kotlin.android") version "x.x.x" (KTS plugin block)
        content = re.sub(
            r'(id\s*\(?\s*["\x27]org\.jetbrains\.kotlin\.[^"\']+["\x27]\s*\)?\s+version\s+["\x27])([0-9.]+)(["\x27])',
            replacer, content
        )
        # kotlin("android") version "x.x.x"
        content = re.sub(
            r'(kotlin\s*\(\s*["\x27][^"\']+["\x27]\s*\)\s+version\s+["\x27])([0-9.]+)(["\x27])',
            replacer, content
        )

        if content != original:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            logs.append(f"Auto-fix: Kotlin version updated dalam {os.path.basename(file_path)}")
    except Exception:
        pass


def _fix_java_compat_in_file(file_path, logs):
    """Fix Java compatibility settings ke Java 17 dalam build.gradle / build.gradle.kts."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        original = content

        # Fix JavaVersion enum: VERSION_1_6, VERSION_1_7, VERSION_1_8, VERSION_11 -> VERSION_17
        content = re.sub(
            r'\bJavaVersion\.VERSION_(1_[5678]|11|1[0-6])\b',
            'JavaVersion.VERSION_17',
            content
        )

        # Fix jvmTarget string: "1.6", "1.7", "1.8", "11" -> "17"
        content = re.sub(
            r'(jvmTarget\s*=\s*["\x27])(1\.[5-9]|1[0-6])(["\x27])',
            r'\g<1>17\3',
            content
        )

        # Fix Java toolchain: JavaLanguageVersion.of(8/11) -> of(17)
        def toolchain_replacer(m):
            ver = int(m.group(1))
            return f'JavaLanguageVersion.of({max(ver, 17)})'
        content = re.sub(
            r'JavaLanguageVersion\.of\((\d+)\)',
            toolchain_replacer, content
        )

        # Fix sourceCompatibility / targetCompatibility integer style: 8 -> 17
        content = re.sub(
            r'((?:source|target)Compatibility\s*=\s*)(1[0-6]|[1-9])\b(?!\s*\.)',
            lambda m: m.group(1) + '17',
            content
        )

        if content != original:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            logs.append(f"Auto-fix: Java compat → 17 dalam {os.path.basename(file_path)}")
    except Exception:
        pass


async def _ensure_ndk(android_dir, logs):
    """Detect ndkVersion dalam project dan cuba install via sdkmanager kalau belum ada."""
    ndk_ver = None
    for bg_name in ("app/build.gradle", "app/build.gradle.kts"):
        bg_path = os.path.join(android_dir, bg_name)
        if not os.path.exists(bg_path):
            continue
        try:
            with open(bg_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            m = re.search(r'ndkVersion\s*[=:]\s*["\x27]([0-9.]+)["\x27]', content)
            if m:
                ndk_ver = m.group(1)
                break
        except Exception:
            pass

    if not ndk_ver:
        return

    ah = os.environ.get("ANDROID_HOME", "/usr/local/lib/android/sdk")
    ndk_path = os.path.join(ah, "ndk", ndk_ver)
    if os.path.isdir(ndk_path):
        return  # Dah ada, skip

    sm = os.path.join(ah, "cmdline-tools", "latest", "bin", "sdkmanager")
    if not os.path.exists(sm):
        sm = os.path.join(ah, "tools", "bin", "sdkmanager")
    if not os.path.exists(sm):
        logs.append(f"Auto-fix: NDK {ndk_ver} diperlukan tapi sdkmanager tidak jumpa")
        return

    logs.append(f"Auto-fix: Memasang NDK {ndk_ver}...")
    code, _, err = await run_cmd(
        f'echo "y" | "{sm}" "ndk;{ndk_ver}"',
        timeout=600
    )
    if code == 0:
        logs.append(f"Auto-fix: NDK {ndk_ver} berjaya dipasang")
    else:
        logs.append(f"Auto-fix: NDK {ndk_ver} gagal dipasang — {(err or '')[:150]}")


# ================================================================
# MAIN FIX FUNCTIONS
# ================================================================

async def fix_common_issues(project_dir, logs, gradle_subdir=""):
    """Auto-fix isu biasa untuk Native/Smali build sebelum kompilasi.
    Covers: CRLF, Gradle wrapper (semua AGP 1.x-9.x), jcenter, local.properties."""
    gdir = os.path.join(project_dir, gradle_subdir) if gradle_subdir else project_dir
    gradlew = os.path.join(gdir, "gradlew")

    # ---------------------------------------------------------------
    # 1. Fix Windows line endings (CRLF → LF)
    # ---------------------------------------------------------------
    crlf_fixed = 0
    skip_dirs = {".git", "build", ".gradle", "node_modules"}
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

    # ---------------------------------------------------------------
    # 2. Fix jcenter() → mavenCentral() (semua gradle files)
    # ---------------------------------------------------------------
    _fix_repositories_in_dir(gdir, logs)

    # ---------------------------------------------------------------
    # 3. Detect Gradle version yang betul untuk project ini
    # ---------------------------------------------------------------
    gradle_ver = None
    gradle_dist_url = None
    props_path = os.path.join(gdir, "gradle", "wrapper", "gradle-wrapper.properties")

    # Cuba baca dari wrapper dulu
    if os.path.exists(props_path):
        try:
            with open(props_path, "r") as f:
                for line in f:
                    if "distributionUrl" in line:
                        url = line.split("=", 1)[1].strip().replace("\\:", ":")
                        gradle_dist_url = url
                        m = re.search(r"gradle-([0-9.]+)-", url)
                        if m:
                            gradle_ver = m.group(1)
                        break
        except Exception:
            pass

    # Kalau tak jumpa dalam wrapper, detect dari AGP
    if not gradle_ver:
        # Cuba dari libs.versions.toml dulu (version catalog)
        toml_path = os.path.join(gdir, "gradle", "libs.versions.toml")
        if os.path.exists(toml_path):
            try:
                with open(toml_path, "r") as f:
                    toml_content = f.read()
                m = re.search(r'(?im)^\s*agp\s*=\s*["\x27]([0-9.]+)["\x27]', toml_content)
                if m:
                    gradle_ver = _agp_to_min_gradle(m.group(1))
                    logs.append(f"Auto-fix: detected AGP {m.group(1)} dari toml → Gradle {gradle_ver}")
            except Exception:
                pass

    if not gradle_ver:
        for bg_name in ("build.gradle", "build.gradle.kts"):
            bg_path = os.path.join(gdir, bg_name)
            if not os.path.exists(bg_path):
                continue
            try:
                with open(bg_path, "r") as f:
                    content = f.read()
                # classpath style: com.android.tools.build:gradle:x.x.x
                m = re.search(r"com\.android\.tools\.build:gradle:([0-9.]+)", content)
                if m:
                    agp = m.group(1)
                    gradle_ver = _agp_to_min_gradle(agp)
                    logs.append(f"Auto-fix: detected AGP {agp} → Gradle {gradle_ver}")
                    break
                # plugins block style: id("com.android.application") version "x.x.x"
                m = re.search(
                    r'id\s*\(?\s*["\x27]com\.android\.application["\x27]\s*\)?\s+version\s+["\x27]([0-9.]+)["\x27]',
                    content
                )
                if m:
                    agp = m.group(1)
                    gradle_ver = _agp_to_min_gradle(agp)
                    logs.append(f"Auto-fix: detected AGP {agp} → Gradle {gradle_ver}")
                    break
            except Exception:
                pass

    if not gradle_ver:
        gradle_ver = "8.7"  # Safe modern default

    if not gradle_dist_url:
        gradle_dist_url = f"https://services.gradle.org/distributions/gradle-{gradle_ver}-bin.zip"

    # ---------------------------------------------------------------
    # 4. Generate gradle wrapper kalau gradlew tak ada
    # ---------------------------------------------------------------
    if not os.path.exists(gradlew):
        logs.append(f"Auto-fix: gradlew missing, downloading Gradle {gradle_ver}...")
        dl_dir = f"/tmp/gradle-inst/gradle-{gradle_ver}"
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
                logs.append(f"Auto-fix: gradle wrapper generated (v{gradle_ver})")
            else:
                logs.append("Auto-fix: wrapper generation failed, akan guna gradle binary terus")
        else:
            logs.append("Auto-fix: gradle download failed")

    # ---------------------------------------------------------------
    # 5. Create local.properties kalau tak ada (sdk.dir)
    # ---------------------------------------------------------------
    lp = os.path.join(gdir, "local.properties")
    if not os.path.exists(lp):
        ah = os.environ.get("ANDROID_HOME", "/usr/local/lib/android/sdk")
        with open(lp, "w") as f:
            f.write(f"sdk.dir={ah}\n")
        logs.append("Auto-fix: created local.properties (sdk.dir)")


async def fix_flutter_versions(project_dir, logs):
    """Auto-fix SEMUA dependency versions dalam android/ supaya lulus Flutter validation.

    Covers (tak kira versi berapa):
      - AGP (semua versi 1.x - 9.x)
      - Gradle wrapper
      - libs.versions.toml (Version Catalog)
      - compileSdk / targetSdk / minSdk (semua modul)
      - Kotlin version (semua gradle files)
      - Java compileOptions + toolchain + jvmTarget
      - NDK auto-install kalau diperlukan
      - jcenter() → mavenCentral()

    Menggantikan keperluan --android-skip-build-dependency-validation sepenuhnya."""

    android_dir = os.path.join(project_dir, "android")
    if not os.path.isdir(android_dir):
        return

    # Flutter minimum requirements
    MIN_AGP     = "8.1.1"
    MIN_GRADLE  = "8.7"
    MIN_COMPILE = 34
    MIN_TARGET  = 33
    MIN_MIN_SDK = 21
    MIN_KOTLIN  = "1.8.0"

    # ---------------------------------------------------------------
    # Step 1: Fix jcenter() → mavenCentral() (semua gradle files dalam android/)
    # ---------------------------------------------------------------
    _fix_repositories_in_dir(android_dir, logs)

    # ---------------------------------------------------------------
    # Step 2: Fix libs.versions.toml (Version Catalog) kalau ada
    # ---------------------------------------------------------------
    _fix_version_catalog(project_dir, logs, MIN_AGP, MIN_KOTLIN, MIN_COMPILE, MIN_TARGET, MIN_MIN_SDK)

    # ---------------------------------------------------------------
    # Step 3: Detect AGP version dari semua sumber yang mungkin
    # ---------------------------------------------------------------
    cur_agp = None
    agp_file = None
    agp_match_start = None
    agp_match_end = None

    # Sumber 1: settings.gradle / settings.gradle.kts (plugins block — cara moden)
    for sg_name in ("settings.gradle", "settings.gradle.kts"):
        sg_path = os.path.join(android_dir, sg_name)
        if not os.path.exists(sg_path):
            continue
        try:
            with open(sg_path, "r", encoding="utf-8", errors="replace") as f:
                sg_content = f.read()
            # id("com.android.application") version "x.x.x"
            m = re.search(
                r'id\s*\(?\s*["\x27]com\.android\.(?:application|library)["\x27]\s*\)?\s+version\s+["\x27]([0-9.]+)["\x27]',
                sg_content
            )
            if m:
                cur_agp = m.group(1)
                agp_file = sg_path
                agp_match_start = m.start(1)
                agp_match_end = m.end(1)
                break
        except Exception:
            pass

    # Sumber 2: build.gradle / build.gradle.kts (classpath style — cara lama)
    if not cur_agp:
        for bg_name in ("build.gradle", "build.gradle.kts"):
            bg_path = os.path.join(android_dir, bg_name)
            if not os.path.exists(bg_path):
                continue
            try:
                with open(bg_path, "r", encoding="utf-8", errors="replace") as f:
                    bg_content = f.read()
                m = re.search(r'com\.android\.tools\.build:gradle:([0-9.]+)', bg_content)
                if m:
                    cur_agp = m.group(1)
                    agp_file = bg_path
                    agp_match_start = m.start(1)
                    agp_match_end = m.end(1)
                    break
            except Exception:
                pass

    # Sumber 3: libs.versions.toml (version catalog — cara paling moden)
    if not cur_agp:
        for toml_path in [
            os.path.join(project_dir, "gradle", "libs.versions.toml"),
            os.path.join(android_dir, "gradle", "libs.versions.toml"),
        ]:
            if not os.path.exists(toml_path):
                continue
            try:
                with open(toml_path, "r", encoding="utf-8", errors="replace") as f:
                    toml_content = f.read()
                m = re.search(r'(?im)^\s*agp\s*=\s*["\x27]([0-9.]+)["\x27]', toml_content)
                if m:
                    cur_agp = m.group(1)
                    # AGP dalam toml sudah di-fix dalam Step 2, cuma perlu nilai untuk Gradle mapping
                    break
            except Exception:
                pass

    # ---------------------------------------------------------------
    # Step 4: Upgrade AGP kalau bawah Flutter minimum
    # ---------------------------------------------------------------
    final_agp = cur_agp if cur_agp else MIN_AGP
    if cur_agp and _ver_tuple(cur_agp) < _ver_tuple(MIN_AGP):
        final_agp = MIN_AGP
        if agp_file and agp_match_start is not None:
            try:
                with open(agp_file, "r", encoding="utf-8", errors="replace") as f:
                    fc = f.read()
                new_fc = fc[:agp_match_start] + MIN_AGP + fc[agp_match_end:]
                with open(agp_file, "w", encoding="utf-8") as f:
                    f.write(new_fc)
                logs.append(f"Auto-fix: AGP {cur_agp} → {MIN_AGP} dalam {os.path.basename(agp_file)}")
            except Exception:
                pass

    # ---------------------------------------------------------------
    # Step 5: Update Gradle wrapper version
    # ---------------------------------------------------------------
    target_gradle = _agp_to_min_gradle(final_agp)
    # Floor pada Flutter minimum
    if _ver_tuple(target_gradle) < _ver_tuple(MIN_GRADLE):
        target_gradle = MIN_GRADLE

    props_path = os.path.join(android_dir, "gradle", "wrapper", "gradle-wrapper.properties")
    if os.path.exists(props_path):
        try:
            with open(props_path, "r", encoding="utf-8", errors="replace") as f:
                pc = f.read()
            m = re.search(r"gradle-([0-9.]+)-", pc)
            if m:
                cur_gradle = m.group(1)
                if _ver_tuple(cur_gradle) < _ver_tuple(target_gradle):
                    new_url = f"https\\://services.gradle.org/distributions/gradle-{target_gradle}-bin.zip"
                    new_pc = re.sub(r"distributionUrl=.*", f"distributionUrl={new_url}", pc)
                    with open(props_path, "w", encoding="utf-8") as f:
                        f.write(new_pc)
                    logs.append(f"Auto-fix: Gradle {cur_gradle} → {target_gradle}")
        except Exception:
            pass
    else:
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

    # ---------------------------------------------------------------
    # Step 6: Fix compileSdk / targetSdk / minSdk
    # SEMUA modul (app/ + submodule lain), bukan app/ sahaja
    # ---------------------------------------------------------------
    skip_dirs = {".git", "build", ".gradle", "node_modules", ".dart_tool"}
    for dirpath, dirs, files in os.walk(android_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if fn in ("build.gradle", "build.gradle.kts"):
                fp = os.path.join(dirpath, fn)
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    # Hanya sentuh fail yang ada android {} block
                    if re.search(r'\bandroid\s*\{', content):
                        _bump_sdk_in_file(fp, logs, MIN_COMPILE, MIN_TARGET, MIN_MIN_SDK)
                except Exception:
                    pass

    # ---------------------------------------------------------------
    # Step 7: Fix Kotlin version (semua gradle files dalam android/)
    # ---------------------------------------------------------------
    for dirpath, dirs, files in os.walk(android_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if fn.endswith((".gradle", ".gradle.kts")):
                _fix_kotlin_in_file(os.path.join(dirpath, fn), logs, MIN_KOTLIN)

    # ---------------------------------------------------------------
    # Step 8: Fix Java compileOptions + toolchain + jvmTarget
    # Semua modul yang ada android {} block
    # ---------------------------------------------------------------
    for dirpath, dirs, files in os.walk(android_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if fn in ("build.gradle", "build.gradle.kts"):
                fp = os.path.join(dirpath, fn)
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    if re.search(r'\bandroid\s*\{', content):
                        _fix_java_compat_in_file(fp, logs)
                except Exception:
                    pass

    # ---------------------------------------------------------------
    # Step 9: NDK — detect & install kalau diperlukan
    # ---------------------------------------------------------------
    await _ensure_ndk(android_dir, logs)


async def build_native(project_dir, config):
    logs = []
    await setup_java(config.get("java_version", "11"))
    logs.append(f"Java {config.get('java_version','11')} ready")
    await setup_android_sdk(config.get("compile_sdk"), config.get("build_tools"))
    logs.append("Android SDK ready")

    await fix_common_issues(project_dir, logs)

    # Determine gradle command — gradlew preferred, fallback to downloaded gradle binary
    gradlew = os.path.join(project_dir, "gradlew")
    if os.path.exists(gradlew):
        await run_cmd(f"chmod +x {gradlew}")
        gcmd = "./gradlew"
    else:
        # Find any downloaded gradle binary in /tmp/gradle-inst/
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

    code, out, err = await run_cmd(f"{gcmd} assembleDebug --stacktrace", cwd=project_dir)
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
    ok = await setup_flutter(config.get("flutter_version", "stable"))
    if not ok:
        return {"success": False, "error": "Flutter SDK install failed", "logs": logs}
    logs.append(f"Flutter {config.get('flutter_version','stable')} ready")

    await fix_common_issues(project_dir, logs, "android")
    await fix_flutter_versions(project_dir, logs)

    gw = os.path.join(project_dir, "android", "gradlew")
    if os.path.exists(gw):
        await run_cmd(f"chmod +x {gw}")

    code, out, err = await run_cmd("flutter pub get", cwd=project_dir, timeout=300)
    logs.append(f"pub get: {'OK' if code == 0 else 'FAIL'}")
    if code != 0:
        return {"success": False, "error": f"flutter pub get failed\n{err}\n{out}", "logs": logs}

    code, out, err = await run_cmd("flutter build apk --debug", cwd=project_dir)
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
    """Find zipalign binary in Android SDK build-tools."""
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
    """Ensure native .so files won't be compressed in rebuilt APK.
    If extractNativeLibs=false (modern APKs), .so must be stored
    uncompressed and page-aligned or the APK won't install."""
    yml_path = os.path.join(project_dir, "apktool.yml")
    if not os.path.exists(yml_path):
        return False
    lib_dir = os.path.join(project_dir, "lib")
    if not os.path.isdir(lib_dir):
        return False
    # Check if any .so files exist
    has_so = any(
        f.endswith(".so")
        for _, _, files in os.walk(lib_dir)
        for f in files
    )
    if not has_so:
        return False
    with open(yml_path, "r") as f:
        content = f.read()
    # Already has .so in doNotCompress
    if re.search(r'-\s*["\']?\.?so["\']?\s*$', content, re.MULTILINE):
        return False
    # Add .so to doNotCompress list
    if "doNotCompress:" in content:
        content = re.sub(r'(doNotCompress:\n)', r'\1- .so\n', content)
    else:
        content += "\ndoNotCompress:\n- .so\n"
    with open(yml_path, "w") as f:
        f.write(content)
    return True


def _find_apksigner():
    """Find apksigner in Android SDK build-tools."""
    ah = os.environ.get("ANDROID_HOME", "/usr/local/lib/android/sdk")
    bt_dir = os.path.join(ah, "build-tools")
    if os.path.isdir(bt_dir):
        for ver in sorted(os.listdir(bt_dir), reverse=True):
            path = os.path.join(bt_dir, ver, "apksigner")
            if os.path.exists(path):
                return path
    return None


async def _ensure_debug_keystore():
    """Generate a debug keystore for signing. Returns path or None."""
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
    """Sign a zipaligned APK using apksigner."""
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
    """Remove META-INF/ (signatures) from an APK so it can be re-signed."""
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
    """Find directory containing split APK files.
    Searches: project_dir children, then parent dir children.
    Matches any folder containing split_*.apk files, regardless of folder name."""
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
    """Package rebuilt base.apk + original split APKs into .apks (ZIP) format.
    Strips original signatures, zipaligns, signs with debug key, then packages."""
    split_apks = sorted([
        os.path.join(splits_dir, f)
        for f in os.listdir(splits_dir)
        if f.lower().endswith('.apk')
    ])
    if not split_apks:
        logs.append("splits/: no APK files found, skipping APKS packaging")
        return None

    # Strip existing signatures from split APKs
    stripped = 0
    for sa in split_apks:
        if _strip_apk_signatures(sa):
            stripped += 1
    if stripped:
        logs.append(f"Stripped signatures from {stripped} split APK(s)")

    # Zipalign splits if possible
    if zipalign_bin:
        for sa in split_apks:
            aligned = sa + '.aligned'
            code, _, _ = await run_cmd(
                f'"{zipalign_bin}" -p -f 4 "{sa}" "{aligned}"',
                timeout=60,
            )
            if code == 0 and os.path.exists(aligned):
                os.replace(aligned, sa)

    # Sign splits with same debug key as base.apk
    if apksigner_bin and keystore:
        for sa in split_apks:
            await _sign_apk(sa, keystore, apksigner_bin, logs)

    # Create .apks file (ZIP with STORED compression — APKs are already compressed)
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

    # Pre-build: ensure .so in doNotCompress (fixes extractNativeLibs=false)
    if _fix_apktool_donotcompress(project_dir):
        logs.append("Auto-fix: added .so to doNotCompress (apktool.yml)")

    # Build with apktool (try --use-aapt2 first)
    code, out, err = await run_cmd("apktool b . --use-aapt2", cwd=project_dir)
    logs.append(f"apktool build (aapt2): {'OK' if code == 0 else 'FAIL'}")

    if code != 0:
        # Retry without --use-aapt2 (older decompiled projects)
        code, out, err = await run_cmd("apktool b .", cwd=project_dir)
        logs.append(f"apktool build (aapt1 fallback): {'OK' if code == 0 else 'FAIL'}")

    if code != 0:
        return {"success": False, "error": f"Smali build failed\n{err}\n{out}", "logs": logs}

    # Find output APK in dist/
    files = []
    dist_dir = os.path.join(project_dir, "dist")
    if os.path.exists(dist_dir):
        for fn in os.listdir(dist_dir):
            if fn.endswith(".apk"):
                files.append(os.path.join(dist_dir, fn))

    if not files:
        return {"success": False, "error": f"No output APK found in dist/\n{err}", "logs": logs}

    # Post-build: zipalign APKs (page-align native libs for compatibility)
    zipalign = _find_zipalign()
    if zipalign:
        for i, apk_path in enumerate(files):
            aligned_path = apk_path + ".aligned"
            zcode, _, zerr = await run_cmd(
                f'"{zipalign}" -p -f 4 "{apk_path}" "{aligned_path}"',
                timeout=120,
            )
            if zcode == 0 and os.path.exists(aligned_path):
                os.replace(aligned_path, apk_path)
                logs.append(f"zipalign: OK ({os.path.basename(apk_path)})")
            else:
                logs.append(f"zipalign: FAIL ({zerr[:200] if zerr else 'unknown'})")
    else:
        logs.append("zipalign: not found (skipped)")

    # Sign APK(s) with debug key
    apksigner = _find_apksigner()
    keystore = await _ensure_debug_keystore()
    signed = False
    if apksigner and keystore:
        sign_results = []
        for p in files:
            sign_results.append(await _sign_apk(p, keystore, apksigner, logs))
        signed = all(sign_results)
    else:
        if not apksigner:
            logs.append("apksigner: not found (signing skipped)")
        if not keystore:
            logs.append("keystore: generation failed (signing skipped)")

    # Check for split APK directory → package as APKS
    # Flexible detection: search for any folder containing split_*.apk files
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
    """Build React Native project → APK/AAB."""
    logs = []

    # Setup environment
    ok = await setup_node(config.get("node_version", "20"))
    if not ok:
        return {"success": False, "error": "Node.js setup gagal", "logs": logs}
    logs.append(f"Node.js {config.get('node_version','20')} ready")

    await setup_java(config.get("java_version", "17"))
    logs.append(f"Java {config.get('java_version','17')} ready")
    await setup_android_sdk()
    logs.append("Android SDK ready")

    # Install dependencies
    ok = await _install_node_deps(project_dir, logs)
    if not ok:
        return {"success": False, "error": "npm/yarn install gagal", "logs": logs}

    # Fix android/ gradle issues
    android_dir = os.path.join(project_dir, "android")
    if os.path.isdir(android_dir):
        await fix_common_issues(android_dir, logs)
    else:
        return {"success": False, "error": "Folder android/ tidak jumpa dalam projek React Native", "logs": logs}

    # Make gradlew executable
    gradlew = os.path.join(android_dir, "gradlew")
    if os.path.exists(gradlew):
        await run_cmd(f"chmod +x {gradlew}")

    # Build APK
    code, out, err = await run_cmd("./gradlew assembleDebug --stacktrace", cwd=android_dir)
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
    """Build Apache Cordova project → APK."""
    logs = []

    ok = await setup_node(config.get("node_version", "20"))
    if not ok:
        return {"success": False, "error": "Node.js setup gagal", "logs": logs}
    logs.append(f"Node.js {config.get('node_version','20')} ready")

    await setup_java(config.get("java_version", "17"))
    logs.append(f"Java {config.get('java_version','17')} ready")
    await setup_android_sdk()
    logs.append("Android SDK ready")

    # Install Cordova CLI globally
    code, _, err = await run_cmd("npm install -g cordova", timeout=300)
    logs.append(f"cordova install: {'OK' if code == 0 else 'FAIL'}")
    if code != 0:
        return {"success": False, "error": f"Cordova CLI install gagal\n{err}", "logs": logs}

    # Install project dependencies
    await _install_node_deps(project_dir, logs)

    # Add android platform kalau belum ada
    android_platform = os.path.join(project_dir, "platforms", "android")
    if not os.path.isdir(android_platform):
        code, out, err = await run_cmd("cordova platform add android", cwd=project_dir, timeout=300)
        logs.append(f"platform add android: {'OK' if code == 0 else 'FAIL'}")
        if code != 0:
            return {"success": False, "error": f"Gagal tambah platform android\n{err}\n{out}", "logs": logs}

    # Fix gradle issues dalam platforms/android
    if os.path.isdir(android_platform):
        await fix_common_issues(android_platform, logs)

    # Build
    code, out, err = await run_cmd("cordova build android --debug", cwd=project_dir, timeout=900)
    logs.append(f"cordova build debug: {'OK' if code == 0 else 'FAIL'}")
    if code != 0:
        return {"success": False, "error": f"Cordova debug build gagal\n{err}\n{out}", "logs": logs}

    code2, out2, err2 = await run_cmd("cordova build android --release", cwd=project_dir, timeout=900)
    logs.append(f"cordova build release: {'OK' if code2 == 0 else 'FAIL'}")

    # APK location untuk Cordova
    search_dirs = [
        os.path.join(project_dir, "platforms", "android", "app", "build", "outputs"),
        os.path.join(project_dir, "platforms", "android", "build", "outputs"),
    ]
    files = _collect_apks(search_dirs)
    if not files:
        return {"success": False, "error": f"Tiada output APK\n{err}\n{err2}", "logs": logs}
    return {"success": True, "files": files, "logs": logs}


async def build_ionic(project_dir, config):
    """Build Ionic project (Capacitor atau Cordova-based) → APK/AAB."""
    logs = []

    ok = await setup_node(config.get("node_version", "20"))
    if not ok:
        return {"success": False, "error": "Node.js setup gagal", "logs": logs}
    logs.append(f"Node.js {config.get('node_version','20')} ready")

    await setup_java(config.get("java_version", "17"))
    logs.append(f"Java {config.get('java_version','17')} ready")
    await setup_android_sdk()
    logs.append("Android SDK ready")

    # Install Ionic CLI
    await run_cmd("npm install -g @ionic/cli", timeout=300)
    logs.append("Ionic CLI ready")

    # Install project dependencies
    ok = await _install_node_deps(project_dir, logs)
    if not ok:
        return {"success": False, "error": "npm/yarn install gagal", "logs": logs}

    # Detect sama ada guna Capacitor atau Cordova
    is_capacitor = (
        os.path.exists(os.path.join(project_dir, "capacitor.config.json")) or
        os.path.exists(os.path.join(project_dir, "capacitor.config.ts")) or
        os.path.exists(os.path.join(project_dir, "capacitor.config.js"))
    )

    if is_capacitor:
        logs.append("Detected: Ionic + Capacitor")

        # Build web assets
        code, out, err = await run_cmd("ionic build --prod || ionic build", cwd=project_dir, timeout=600)
        logs.append(f"ionic build: {'OK' if code == 0 else 'FAIL'}")
        if code != 0:
            # Cuba tanpa ionic CLI — guna npm/ng build terus
            code, out, err = await run_cmd("npx ng build --configuration production || npx ng build", cwd=project_dir, timeout=600)
            logs.append(f"ng build fallback: {'OK' if code == 0 else 'FAIL'}")
            if code != 0:
                return {"success": False, "error": f"Web build gagal\n{err}\n{out}", "logs": logs}

        # Sync ke android
        code, out, err = await run_cmd("npx cap sync android", cwd=project_dir, timeout=300)
        logs.append(f"cap sync: {'OK' if code == 0 else 'FAIL'}")

        # Build gradle (sama macam Native)
        android_dir = os.path.join(project_dir, "android")
        if not os.path.isdir(android_dir):
            # Cuba tambah platform
            code, out, err = await run_cmd("npx cap add android", cwd=project_dir, timeout=300)
            logs.append(f"cap add android: {'OK' if code == 0 else 'FAIL'}")

        if os.path.isdir(android_dir):
            await fix_common_issues(android_dir, logs)
            gradlew = os.path.join(android_dir, "gradlew")
            if os.path.exists(gradlew):
                await run_cmd(f"chmod +x {gradlew}")
            code, out, err = await run_cmd("./gradlew assembleDebug --stacktrace", cwd=android_dir)
            logs.append(f"assembleDebug: {'OK' if code == 0 else 'FAIL'}")
            if code != 0:
                return {"success": False, "error": f"Gradle build gagal\n{err}\n{out}", "logs": logs}
            code2, _, err2 = await run_cmd("./gradlew assembleRelease --stacktrace", cwd=android_dir)
            logs.append(f"assembleRelease: {'OK' if code2 == 0 else 'FAIL'}")
            code3, _, err3 = await run_cmd("./gradlew bundleRelease --stacktrace", cwd=android_dir)
            logs.append(f"bundleRelease: {'OK' if code3 == 0 else 'FAIL'}")

        search_dirs = [
            os.path.join(project_dir, "android", "app", "build", "outputs"),
        ]

    else:
        logs.append("Detected: Ionic + Cordova")

        # Install Cordova
        await run_cmd("npm install -g cordova", timeout=300)

        # Add android platform kalau belum ada
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
    """Build Capacitor standalone project (tanpa Ionic CLI) → APK/AAB."""
    logs = []

    ok = await setup_node(config.get("node_version", "20"))
    if not ok:
        return {"success": False, "error": "Node.js setup gagal", "logs": logs}
    logs.append(f"Node.js {config.get('node_version','20')} ready")

    await setup_java(config.get("java_version", "17"))
    logs.append(f"Java {config.get('java_version','17')} ready")
    await setup_android_sdk()
    logs.append("Android SDK ready")

    # Install dependencies
    ok = await _install_node_deps(project_dir, logs)
    if not ok:
        return {"success": False, "error": "npm/yarn install gagal", "logs": logs}

    # Sync web → android
    code, out, err = await run_cmd("npx cap sync android", cwd=project_dir, timeout=300)
    logs.append(f"cap sync: {'OK' if code == 0 else 'FAIL'}")

    android_dir = os.path.join(project_dir, "android")
    if not os.path.isdir(android_dir):
        code, out, err = await run_cmd("npx cap add android", cwd=project_dir, timeout=300)
        logs.append(f"cap add android: {'OK' if code == 0 else 'FAIL'}")
        if code != 0:
            return {"success": False, "error": f"Gagal add platform android\n{err}\n{out}", "logs": logs}

    # Fix gradle + build
    await fix_common_issues(android_dir, logs)
    gradlew = os.path.join(android_dir, "gradlew")
    if os.path.exists(gradlew):
        await run_cmd(f"chmod +x {gradlew}")

    code, out, err = await run_cmd("./gradlew assembleDebug --stacktrace", cwd=android_dir)
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
    if t == "native":
        return await build_native(project_dir, c)
    if t == "flutter":
        return await build_flutter(project_dir, c)
    if t == "smali":
        return await build_smali(project_dir, c)
    if t == "react_native":
        return await build_react_native(project_dir, c)
    if t == "cordova":
        return await build_cordova(project_dir, c)
    if t == "ionic":
        return await build_ionic(project_dir, c)
    if t == "capacitor":
        return await build_capacitor(project_dir, c)
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
