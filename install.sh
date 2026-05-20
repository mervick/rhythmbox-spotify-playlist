#!/usr/bin/env bash
# install.sh — copy plugin to per-user Rhythmbox plugins directory
set -euo pipefail

PLUGIN_DIR="$HOME/.local/share/rhythmbox/plugins/spotify_playlist"

echo "→ Installing to $PLUGIN_DIR"
mkdir -p "$PLUGIN_DIR"
cp spotify_playlist.plugin spotify_playlist.py "$PLUGIN_DIR/"

echo "→ Checking 'requests' library..."
if ! python3 -c "import requests" 2>/dev/null; then
    echo "  Installing requests..."
    pip3 install --user requests
else
    echo "  requests already installed."
fi

echo ""
echo "Done! Now:"
echo "  1. Start (or restart) Rhythmbox"
echo "  2. Edit → Plugins → enable 'Spotify Playlist'"
echo "  3. Tools → Spotify plugin preferences… → paste your Client ID"
echo "  4. Tools → Connect to Spotify…"
echo "  5. Select a song → Tools → Add to Spotify playlist…"
