#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
hook_source="$repo_root/scripts/hooks/commit-msg"
hooks_dir="$repo_root/.git/hooks"
hook_target="$hooks_dir/commit-msg"

if [[ ! -f "$hook_source" ]]; then
  echo "[Error] Hook source not found at '$hook_source'" >&2
  exit 1
fi

mkdir -p "$hooks_dir"
cp "$hook_source" "$hook_target"
chmod +x "$hook_target"

echo "[OK] Installed 'commit-msg' hook at '$hook_target'."