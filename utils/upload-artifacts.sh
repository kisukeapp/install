#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

REPO="kisukeapp/install"
TOKEN="${GITHUB_TOKEN:?GITHUB_TOKEN is required}"
SCRIPTS_DIR="${SCRIPTS_DIR:-$REPO_ROOT/scripts}"  # Default to repo_root/scripts

api() {
  curl -sSfL -H "Authorization: token $TOKEN" -H "Accept: application/vnd.github+json" "$@"
}

release_info=$(api "https://api.github.com/repos/$REPO/releases/latest")
release_id=$(echo "$release_info" | jq -r .id)
release_tag=$(echo "$release_info" | jq -r .tag_name)
upload_url=$(echo "$release_info" | jq -r .upload_url | sed 's/{.*}//')

if [[ -z "$release_id" || "$release_id" == "null" ]]; then
  echo "Error: Could not fetch latest release for $REPO" >&2
  exit 1
fi

echo "[INFO] Latest release ID: $release_id"
echo "[INFO] Release version: $release_tag"
if [[ -d "$SCRIPTS_DIR" ]]; then
  bundle_name="$HOME/kisuke-bridge-${release_tag}.tar.xz"
  echo "[INFO] Creating bundle: $(basename "$bundle_name") from $SCRIPTS_DIR"
  tar -cJf "$bundle_name" -C "$REPO_ROOT" "scripts"
  
  if [[ -f "$bundle_name" ]]; then
    echo "[INFO] Successfully created $(basename "$bundle_name")"
  else
    echo "[ERROR] Failed to create bundle $(basename "$bundle_name")" >&2
    exit 1
  fi
else
  echo "[WARN] Scripts directory $SCRIPTS_DIR not found, skipping bundle creation"
fi

shopt -s nullglob
files=("$HOME"/kisuke-*.tar.xz "$REPO_ROOT"/setup.sh)

if [[ ${#files[@]} -eq 0 ]]; then
  echo "[WARN] No release files found."
  exit 0
fi
for file in "${files[@]}"; do
  [[ -f "$file" ]] || continue
  echo "[INFO] Uploading $file ..."
  mime_type=$(file --mime-type -b "$file")
  asset_id=$(api "https://api.github.com/repos/$REPO/releases/$release_id/assets" \
    | jq -r ".[] | select(.name==\"$(basename "$file")\") | .id")
  
  if [[ -n "$asset_id" && "$asset_id" != "null" ]]; then
    echo "[INFO] Deleting existing asset $(basename "$file") (ID: $asset_id)..."
    curl -sSfL -X DELETE \
      -H "Authorization: token $TOKEN" \
      "https://api.github.com/repos/$REPO/releases/assets/$asset_id"
  fi

  download_url=$(curl -sSfL \
    -X POST \
    -H "Authorization: token $TOKEN" \
    -H "Content-Type: $mime_type" \
    --data-binary @"$file" \
    "$upload_url?name=$(basename "$file")" \
    | jq -r '.browser_download_url')
  
  echo "[INFO] Uploaded: $download_url"
done

echo "[INFO] Upload process completed"