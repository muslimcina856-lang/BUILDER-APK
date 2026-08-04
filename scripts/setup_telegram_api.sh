#!/bin/bash
set -e
echo "[*] Building Telegram Bot API Local Server..."

# Install build deps
sudo apt-get update -qq
sudo apt-get install -y -qq make git zlib1g-dev libssl-dev gperf cmake g++

# Clone & build
git clone --recursive https://github.com/tdlib/telegram-bot-api.git /tmp/tg-bot-api
cd /tmp/tg-bot-api
mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX:PATH=/usr/local ..
cmake --build . --target install -j$(nproc)

# Pastikan symlink wujud
sudo ln -sf /usr/local/bin/telegram-bot-api /usr/bin/telegram-bot-api


echo "[OK] Telegram Bot API Server built & installed"
echo ""
echo "Start with:"
echo "  telegram-bot-api --api-id=YOUR_API_ID --api-hash=YOUR_API_HASH --local &"
