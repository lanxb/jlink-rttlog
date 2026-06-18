#!/usr/bin/env bash
set -euo pipefail

echo "========================================"
echo "  J-Link RTT Logger - Build"
echo "========================================"
echo

# Activate venv if it exists
if [ -d "venv" ]; then
    source venv/bin/activate
else
    echo "[!] Virtual environment not found. Run setup.sh first."
    exit 1
fi

# Clean
echo "[*] Cleaning old build..."
rm -rf build/ jlink-rttlog.spec jlink-rttlog jlink-rttlog.exe

# Build
echo "[*] Building jlink-rttlog..."
python -m PyInstaller --onefile --name jlink-rttlog --distpath . jlink_rttlog.py

# Clean artifacts
echo "[*] Cleaning build artifacts..."
rm -rf build/ jlink-rttlog.spec
echo
echo "[OK] Build complete: jlink-rttlog"
