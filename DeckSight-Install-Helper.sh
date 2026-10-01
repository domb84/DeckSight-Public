#!/bin/bash
# Helper script to download and run the DeckSight installer

set -euo pipefail

WORKDIR="/tmp/decksight"
TARBALL="$WORKDIR/decksight.tgz"
INSTALL_DIR="$WORKDIR/work"

# Clean up from previous runs
echo "[DeckSight] Cleaning up previous install data..."
rm -rf "$WORKDIR"
mkdir -p "$INSTALL_DIR"

# Download the release tarball (Placeholder URL)
echo "[DeckSight] Downloading release package..."
curl -fsSL "https://github.com/ShadeTechnik/DeckSight-Public/releases/latest/download/DeckSight.tar.gz" -o "$TARBALL"

# Extract into the install directory
echo "[DeckSight] Extracting package..."
tar -xzf "$TARBALL" -C "$INSTALL_DIR"

# Run the actual installer
echo "[DeckSight] Launching installer..."
bash "$INSTALL_DIR/install.sh"
