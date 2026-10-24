#!/usr/bin/env bash
# Turns an install that was copied over (no .git) into a Git checkout of a release, so the updater can manage it.
# Site files (.env, registry CSVs, backups, Mosquitto passwords, generated dashboards) are git-ignored and stay as they are.
# Run as the admin user from the install folder:
#   scripts/convert-to-git.sh <repo-url> <tag>          # dry run: lists tracked files that differ from the release
#   scripts/convert-to-git.sh <repo-url> <tag> --apply  # backup, switch every tracked file to the release, restart
set -euo pipefail
cd "$(dirname "$0")/.."
URL="${1:?usage: convert-to-git.sh <repo-url> <tag> [--apply]}"; TAG="${2:?release tag, e.g. v0.9.0}"
if [ ! -d .git ]; then
  git init -q
  git remote add origin "$URL"
fi
git fetch -q --tags origin
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || { echo "No tag $TAG at $URL"; exit 1; }
git reset -q "$TAG"   # index and HEAD = the release; the files on disk are not touched
echo "Tracked files on disk that differ from $TAG (--apply replaces them with the release version):"
git status --short --untracked-files=no | sed 's/^/  /'
[ "${3:-}" = "--apply" ] || { echo "Dry run only. Re-run with --apply."; exit 0; }
scripts/backup.sh
git checkout -q -- .
git -c advice.detachedHead=false checkout -q --detach "$TAG"
mkdir -p state
python3 scripts/build_dashboards.py
docker compose up -d --remove-orphans && docker compose restart
echo "Now on $(git describe --tags). Next: sudo scripts/setup-updater.sh <admin-user> <email> <signing-key.pub>"
