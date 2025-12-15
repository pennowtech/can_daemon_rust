#!/usr/bin/env bash
set -euo pipefail

LICENSE_ID="Apache-2.0"
ROOT="${1:-.}"

layer_for_file() {
  local path="$1"
  # normalize to forward slashes just in case
  case "$path" in
    */src/domain/*) echo "Domain" ;;
    */src/app/*) echo "Application" ;;
    */src/ports/*) echo "Ports" ;;
    */src/infra/*) echo "Infrastructure" ;;
    */src/main.rs) echo "Composition Root" ;;
    */src/lib.rs) echo "Composition Root" ;;
    */src/*) echo "Application" ;;
    *) echo "Unknown" ;;
  esac
}

title_for_file() {
  local path="$1"
  local base
  base="$(basename "$path")"
  # Use a readable title: keep file name but strip extension
  echo "${base%.rs}"
}

has_spdx() {
  local path="$1"
  grep -qE '^// SPDX-License-Identifier:' "$path"
}

tmpfile="$(mktemp)"
trap 'rm -f "$tmpfile"' EXIT

mapfile -t files < <(find "$ROOT" -type f -name '*.rs' \
  -not -path '*/target/*' \
  -not -path '*/.git/*')

if [[ ${#files[@]} -eq 0 ]]; then
  echo "No Rust files found."
  exit 0
fi

changed=0
for f in "${files[@]}"; do
  if has_spdx "$f"; then
    continue
  fi

  layer="$(layer_for_file "$f")"
  title="$(title_for_file "$f")"

  echo "Adding header: $f"
  cat > "$tmpfile" <<EOF
// SPDX-License-Identifier: ${LICENSE_ID}
//! ${title}
//!
//! Layer: ${layer}
//! Purpose:
//! - TODO: describe this module briefly
//!
//! Notes:
//! - Standard file header. Keep stable to avoid churn.

EOF

  cat "$f" >> "$tmpfile"
  cp "$tmpfile" "$f"
  changed=$((changed + 1))
done

echo "Done. Headers added to ${changed} file(s)."
echo "Tip: gradually replace the Purpose TODOs as you touch files."
