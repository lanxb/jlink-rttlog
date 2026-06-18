#!/usr/bin/env bash
set -euo pipefail

echo "========================================"
echo "  J-Link RTT Logger - Environment Setup"
echo "========================================"
echo

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "[!] Python not found. Please install Python 3.10+ first."
    exit 1
fi
python3 --version
echo

# Create venv if not exists
if [ ! -d "venv" ]; then
    echo "[*] Creating virtual environment..."
    python3 -m venv venv
fi

# Activate & install
echo "[*] Activating virtual environment..."
source venv/bin/activate

echo "[*] Installing dependencies..."
pip install -r requirements.txt
echo
echo "[OK] Setup complete."
echo
echo "Run the script:  python jlink_rttlog.py"
echo "Build binary:    bash build.sh"
