#!/bin/bash
set -e
VERSION="${1:-stable}"
FDIR="/tmp/flutter_sdk"
[ -d "$FDIR" ] && rm -rf "$FDIR"
git clone https://github.com/flutter/flutter.git -b "$VERSION" --depth 1 "$FDIR"
export PATH="$FDIR/bin:$PATH"
flutter precache --android
yes | flutter doctor --android-licenses 2>/dev/null || true
flutter doctor
echo "[OK] Flutter $VERSION setup complete"
