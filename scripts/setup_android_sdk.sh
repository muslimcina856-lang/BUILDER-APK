#!/bin/bash
set -e
ANDROID_HOME="${ANDROID_HOME:-/usr/local/lib/android/sdk}"
SM="${ANDROID_HOME}/cmdline-tools/latest/bin/sdkmanager"
yes | $SM --licenses 2>/dev/null || true
$SM "platforms;android-34" "platforms;android-33" "platforms;android-31" "platforms;android-30" \
    "build-tools;34.0.0" "build-tools;33.0.2" "build-tools;30.0.3"
echo "[OK] Android SDK setup complete"
