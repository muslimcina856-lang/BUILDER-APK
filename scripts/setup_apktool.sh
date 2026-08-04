#!/bin/bash
set -e
APKTOOL_VERSION="${1:-2.9.3}"
echo "[*] Installing apktool v${APKTOOL_VERSION}..."
sudo wget -q "https://github.com/iBotPeaches/Apktool/releases/download/v${APKTOOL_VERSION}/apktool_${APKTOOL_VERSION}.jar" -O /usr/local/bin/apktool.jar
sudo wget -q "https://raw.githubusercontent.com/iBotPeaches/Apktool/master/scripts/linux/apktool" -O /usr/local/bin/apktool
sudo chmod +x /usr/local/bin/apktool
apktool --version
echo "[OK] apktool v${APKTOOL_VERSION} setup complete"
