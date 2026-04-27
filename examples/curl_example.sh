#!/bin/bash

# Responses API curl examples.

set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
AUTH_ARGS=()

if [ -n "${API_KEY:-}" ]; then
  AUTH_ARGS=(-H "Authorization: Bearer $API_KEY")
fi

echo "=== Basic response ==="
curl -sS "$BASE_URL/v1/responses" \
  -H "Content-Type: application/json" \
  "${AUTH_ARGS[@]}" \
  -d '{
    "model": "sonnet",
    "input": "What is 2 + 2?"
  }' | jq .

echo
echo "=== Instructions ==="
curl -sS "$BASE_URL/v1/responses" \
  -H "Content-Type: application/json" \
  "${AUTH_ARGS[@]}" \
  -d '{
    "model": "sonnet",
    "instructions": "Answer in one short sentence.",
    "input": "How do I read a file in Python?"
  }' | jq .

echo
echo "=== Streaming ==="
curl -sS -N "$BASE_URL/v1/responses" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  "${AUTH_ARGS[@]}" \
  -d '{
    "model": "sonnet",
    "input": "Count from 1 to 5 slowly.",
    "stream": true
  }'

echo
echo "=== Models ==="
curl -sS "$BASE_URL/v1/models" "${AUTH_ARGS[@]}" | jq .

echo
echo "=== Health ==="
curl -sS "$BASE_URL/health" | jq .
