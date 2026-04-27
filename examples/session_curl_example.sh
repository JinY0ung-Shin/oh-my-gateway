#!/bin/bash

# Multi-turn Responses API example with curl.

set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
AUTH_ARGS=()

if [ -n "${API_KEY:-}" ]; then
  AUTH_ARGS=(-H "Authorization: Bearer $API_KEY")
fi

echo "=== Turn 1 ==="
FIRST_RESPONSE=$(
  curl -sS "$BASE_URL/v1/responses" \
    -H "Content-Type: application/json" \
    "${AUTH_ARGS[@]}" \
    -d '{
      "model": "sonnet",
      "input": "Hello. My name is Sarah and I am learning React."
    }'
)
echo "$FIRST_RESPONSE" | jq -r '.output[0].content[0].text'
FIRST_ID=$(echo "$FIRST_RESPONSE" | jq -r '.id')

echo
echo "=== Turn 2 ==="
SECOND_RESPONSE=$(
  curl -sS "$BASE_URL/v1/responses" \
    -H "Content-Type: application/json" \
    "${AUTH_ARGS[@]}" \
    -d "{
      \"model\": \"sonnet\",
      \"input\": \"What is my name and what am I learning?\",
      \"previous_response_id\": \"$FIRST_ID\"
    }"
)
echo "$SECOND_RESPONSE" | jq -r '.output[0].content[0].text'
SECOND_ID=$(echo "$SECOND_RESPONSE" | jq -r '.id')

SESSION_ID=$(echo "$SECOND_ID" | cut -d_ -f2)

echo
echo "=== Session info ==="
curl -sS "$BASE_URL/v1/sessions/$SESSION_ID" "${AUTH_ARGS[@]}" | jq .

echo
echo "=== Session stats ==="
curl -sS "$BASE_URL/v1/sessions/stats" "${AUTH_ARGS[@]}" | jq .

echo
echo "=== Cleanup ==="
curl -sS -X DELETE "$BASE_URL/v1/sessions/$SESSION_ID" "${AUTH_ARGS[@]}" | jq .
