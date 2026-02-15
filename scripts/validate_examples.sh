#!/usr/bin/env bash
set -euo pipefail
TEMPLATE_DIR="templates/minimal"
FLOW="${TEMPLATE_DIR}/flows/hello.yaml"
INPUT='{"name":"TraceMind"}'

if [ ! -f "${FLOW}" ]; then
  echo "flow not found: ${FLOW}" >&2
  exit 1
fi

export PYTHONPATH="${TEMPLATE_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

# shellcheck disable=SC2086 # JSON string needs word splitting as-is
timeout 120s tm run recipe "${FLOW}" -i "${INPUT}"
echo "OK: hello flow finished"
