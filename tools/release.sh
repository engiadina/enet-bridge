#!/bin/sh
# Create a GitHub release of the current main branch with the integration as ZIP asset.
#
#   tools/release.sh <notes-file>
#
# The ZIP (enet_bridge.zip) contains custom_components/enet_bridge at its top level, as hacs.json ("zip_release",
# "filename") expects; HACS installs it instead of the source archive. Without the ZIP a release cannot be
# installed – so always release through this script. Version = "version" in manifest.json, tag v<version>.
set -e
cd "$(dirname "$0")/.."
NOTES="$1"
REPO=engiadina/enet-bridge
[ -f "$NOTES" ] || { echo "usage: tools/release.sh <notes-file>"; exit 1; }
GH=$(command -v gh || echo /opt/homebrew/bin/gh)
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "working tree not clean – commit first"; exit 1; }
[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || { echo "not on main"; exit 1; }
git fetch -q origin main
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || { echo "main is not pushed (or behind origin/main)"; exit 1; }
VER=$(python3 -c 'import json; print(json.load(open("custom_components/enet_bridge/manifest.json"))["version"])')
python3 -c 'import json,sys; h=json.load(open("hacs.json")); sys.exit(0 if h.get("zip_release") and h.get("filename") == "enet_bridge.zip" else 1)' \
  || { echo "hacs.json: zip_release/filename missing"; exit 1; }
"$GH" release view "v$VER" -R "$REPO" >/dev/null 2>&1 && { echo "release v$VER already exists"; exit 1; }

ZIP=$(mktemp -d)/enet_bridge.zip
git archive --format=zip -o "$ZIP" HEAD:custom_components/enet_bridge
unzip -l "$ZIP" | grep -q " manifest.json$" || { echo "ZIP without manifest.json"; exit 1; }
unzip -p "$ZIP" manifest.json | grep -q "\"version\": \"$VER\"" || { echo "ZIP is not version $VER"; exit 1; }

"$GH" release create "v$VER" -R "$REPO" --target "$(git rev-parse HEAD)" --title "eNet Bridge $VER" --notes-file "$NOTES" "$ZIP"
echo "Release v$VER with $(du -k "$ZIP" | cut -f1) kB ZIP."
