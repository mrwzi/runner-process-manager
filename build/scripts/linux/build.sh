#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$root"
python3 -m pip install -r build/scripts/requirements-build.txt
python3 -m PyInstaller \
  --noconfirm --clean --onefile --name Runner \
  --distpath "$root/build/temp/linux" \
  --workpath "$root/build/temp/pyinstaller-linux" \
  --specpath "$root/build/temp/specs" \
  --paths "$root/src" \
  --add-data "src/config/apps.json:." \
  --add-data "src/assets:assets" \
  src/launcher/run.py
echo "Linux binary: $root/build/temp/linux/Runner"
