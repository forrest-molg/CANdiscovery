#!/bin/bash
# setup_ad3.sh — Install Digilent Adept Runtime + WaveForms SDK on Raspberry Pi (ARM64)
#
# Downloads both packages directly from Digilent's public S3 bucket.
# Run:  chmod +x setup_ad3.sh && ./setup_ad3.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ADEPT_VER="2.27.9"
WF_VER="3.25.1"
S3_BASE="https://digilent.s3.us-west-2.amazonaws.com/Software"

ADEPT_DEB="digilent.adept.runtime_${ADEPT_VER}-arm64.deb"
WF_DEB="digilent.waveforms_${WF_VER}_arm64.deb"

echo "=== CANdiscovery AD3 Setup ==="

# ── 1. Download Adept Runtime ──────────────────────────────────────────────────
if [[ ! -f "$ADEPT_DEB" ]]; then
    echo "[1/4] Downloading Adept Runtime ${ADEPT_VER}..."
    curl -L "${S3_BASE}/Adept2%20Runtime/${ADEPT_VER}/${ADEPT_DEB}" -o "$ADEPT_DEB"
else
    echo "[1/4] Adept Runtime already downloaded: $ADEPT_DEB"
fi

# ── 2. Download WaveForms ──────────────────────────────────────────────────────
if [[ ! -f "$WF_DEB" ]]; then
    echo "[2/4] Downloading WaveForms ${WF_VER}..."
    curl -L "${S3_BASE}/Waveforms/${WF_VER}/${WF_DEB}" -o "$WF_DEB"
else
    echo "[2/4] WaveForms already downloaded: $WF_DEB"
fi

# ── 3. Install (Adept first, then WaveForms) ──────────────────────────────────
echo "[3/4] Installing Adept Runtime..."
sudo dpkg -i "$ADEPT_DEB"

echo "[3/4] Installing WaveForms (SDK only — Qt6 GUI deps skipped)..."
sudo dpkg -i --force-depends "$WF_DEB"
sudo ldconfig

# ── 4. Install pydwf Python wrapper ───────────────────────────────────────────
echo "[4/4] Installing pydwf Python package..."
pip install pydwf --upgrade

# ── udev rule for Digilent USB devices ────────────────────────────────────────
echo "Installing udev rules for Digilent devices..."
if [[ ! -f "/etc/udev/rules.d/52-digilent-usb.rules" ]]; then
    sudo tee /etc/udev/rules.d/52-digilent-usb.rules > /dev/null <<'EOF'
# Digilent Analog Discovery / Digital Discovery — world-accessible
SUBSYSTEM=="usb", ATTR{idVendor}=="1443", MODE="0666"
EOF
fi
sudo udevadm control --reload-rules
sudo udevadm trigger

echo ""
echo "=== Setup complete ==="
echo "Unplug and replug the AD3 if it was already connected, then verify:"
echo "  python3 -c \"from pydwf import DwfLibrary; d=DwfLibrary(); print('OK, version:', d.getVersion())\""
echo "Run app: cd $(pwd) && ./run.sh --fg"
