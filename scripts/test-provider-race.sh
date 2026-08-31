#!/bin/sh

set -eu

base_url="${1:-http://localhost:23456}"
runs="${2:-3}"

command -v jq >/dev/null 2>&1 || {
  echo "jq is required" >&2
  exit 1
}

i=1
while [ "$i" -le "$runs" ]; do
  echo "Race $i/$runs"

  response="$(curl -fsS -X POST "$base_url/api/v1/browsers")"
  echo "$response" | jq

  browser_id="$(echo "$response" | jq -er '.browser_id')"
  curl -fsS -X DELETE "$base_url/api/v1/browsers/$browser_id" | jq

  i=$((i + 1))
done
