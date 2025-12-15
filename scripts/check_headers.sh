#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:-.}"
LICENSE_ID="Apache-2.0"

missing=()

while IFS= read -r -d '' f; do
  if [[ "$f" == *"/target/"* || "$f" == *"/.git/"* ]]; then
    continue
  fi

  # Must contain SPDX and must be Apache-2.0
  if ! grep -qE "^// SPDX-License-Identifier: ${LICENSE_ID}\$" "$f"; then
    missing+=("$f (missing or wrong SPDX; expected ${LICENSE_ID})")
    continue
  fi

  # Must contain module-level doc `//!`
  if ! grep -qE '^//!' "$f"; then
    missing+=("$f (missing module docs //! )")
    continue
  fi
done < <(find "$ROOT" -type f -name '*.rs' -print0)

if [[ ${#missing[@]} -gt 0 ]]; then
  echo "Header/signature check failed. Fix these files:"
  for m in "${missing[@]}"; do
    echo "  - $m"
  done
  echo
  echo "Run: scripts/add_headers.sh ."
  exit 1
fi

echo "Header/signature check OK."
