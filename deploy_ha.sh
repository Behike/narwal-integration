#!/bin/sh
# Deploy the local Freo X Plus build (branch main) into Home Assistant and
# tell Marketplace which upstream release it is based on, so it stops
# offering that release as an update.
# Usage: sh deploy_ha.sh            (run from anywhere)
set -eu
REPO=$(cd "$(dirname "$0")" && pwd)
HA=/home/dietpi/Projects/HomeAssistant/ha_data
DEST="$HA/custom_components/narwal"
BASE_TAG=$(git -C "$REPO" describe --tags --abbrev=0 --match 'v*')   # e.g. v0.10.1
BASE_COMMIT=$(git -C "$REPO" rev-parse --short=7 "$BASE_TAG^{commit}")

[ "$(git -C "$REPO" branch --show-current)" = main ] || { echo "checkout main first"; exit 1; }
echo "Deploying $(git -C "$REPO" log --oneline -1) (base $BASE_TAG)"

# HA runs as root and leaves root-owned __pycache__ folders we can't delete;
# remove them from inside the container while it is still running.
docker exec homeassistant find /config/custom_components/narwal -name __pycache__ -prune -exec rm -rf {} +

docker stop -t 120 homeassistant
# Whatever happens from here on, start Home Assistant again.
trap 'docker start homeassistant >/dev/null && echo "Home Assistant started"' EXIT

cp "$HA/.storage/marketplace.repositories" "$HA/.storage/marketplace.repositories.bak"
python3 - "$HA/.storage/marketplace.repositories" "$BASE_TAG" "$BASE_COMMIT" <<'EOF'
import json, os, sys
path, tag, commit = sys.argv[1:]
store = json.load(open(path))
repo = next(r for r in store["data"].values() if r.get("full_name") == "nadavbau/narwal-integration")
repo["version_installed"], repo["installed_commit"] = tag, commit
# The file is root-owned but the folder is ours: write a copy, then swap it in.
with open(path + ".tmp", "w") as f:
    json.dump(store, f, indent=2, ensure_ascii=False)
os.replace(path + ".tmp", path)
print("Marketplace: narwal installed version ->", tag)
EOF

rm -rf "$DEST"
mkdir "$DEST"
# git archive = tracked files only (no local __pycache__ or stray files).
git -C "$REPO" archive HEAD custom_components/narwal | tar -x -C "$DEST" --strip-components=2
echo "Deployed: $(grep '"version"' "$DEST/manifest.json")"
