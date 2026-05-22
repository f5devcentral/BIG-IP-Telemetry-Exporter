#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GEN="$ROOT/otel-collector/generated-config.yaml"
BASE="$ROOT/otel-collector/config.yaml"
if [[ ! -f "$GEN" ]]; then
  cp "$BASE" "$GEN"
  echo "Created $GEN from base config"
fi
