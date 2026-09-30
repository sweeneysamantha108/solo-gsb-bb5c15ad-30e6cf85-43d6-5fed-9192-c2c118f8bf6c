#!/usr/bin/env bash
# End-to-end calling example for the royalty settlement API.
# Requires: a running service (docker compose up -d) and curl + jq.
#
# Usage:
#   BASE=http://localhost:8000 ./examples/walkthrough.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
API="$BASE/api/v1"
j() { jq -S . 2>/dev/null || cat; }

echo "== 1. health =="
curl -sS "$BASE/health" | j; echo

echo "== 2. register rightsholders =="
for body in \
  '{"code":"RH-001","name":"作曲家甲"}' \
  '{"code":"RH-002","name":"作词家乙"}' \
  '{"code":"RH-003","name":"出版商丙"}'; do
  curl -sS -X POST "$API/rightsholders" -H 'Content-Type: application/json' -d "$body" | j; echo
done

echo "== 3. register works =="
for body in \
  '{"code":"W-001","title":"夜场录音 A"}' \
  '{"code":"W-002","title":"夜场录音 B（暂无协议）"}'; do
  curl -sS -X POST "$API/works" -H 'Content-Type: application/json' -d "$body" | j; echo
done

echo "== 4. create agreement for W-001 (2026-01-01 .. 2026-07-01), shares 50/30/20 =="
curl -sS -X POST "$API/agreements" -H 'Content-Type: application/json' -d '{
  "work_code":"W-001",
  "valid_from":"2026-01-01T00:00:00Z",
  "valid_to":"2026-07-01T00:00:00Z",
  "shares":[
    {"rightsholder_code":"RH-001","ratio":"50"},
    {"rightsholder_code":"RH-002","ratio":"30"},
    {"rightsholder_code":"RH-003","ratio":"20"}
  ]
}' | j; echo

echo "== 5. overlapping agreement is rejected (422) =="
curl -sS -o /tmp/overlap.json -w "HTTP %{http_code}\n" -X POST "$API/agreements" \
  -H 'Content-Type: application/json' -d '{
  "work_code":"W-001",
  "valid_from":"2026-03-01T00:00:00Z",
  "valid_to":"2026-08-01T00:00:00Z",
  "shares":[{"rightsholder_code":"RH-001","ratio":"100"}]
}'
cat /tmp/overlap.json | j; echo

echo "== 6. ratios not summing to 100 are rejected (422) =="
curl -sS -o /tmp/ratios.json -w "HTTP %{http_code}\n" -X POST "$API/agreements" \
  -H 'Content-Type: application/json' -d '{
  "work_code":"W-002",
  "valid_from":"2026-01-01T00:00:00Z",
  "shares":[
    {"rightsholder_code":"RH-001","ratio":"60"},
    {"rightsholder_code":"RH-002","ratio":"30"}
  ]
}'
cat /tmp/ratios.json | j; echo

echo "== 7. import plays (amounts are integer cents) =="
curl -sS -X POST "$API/plays" -H 'Content-Type: application/json' -d '{
  "code":"P-0001","work_code":"W-001","occurred_at":"2026-02-10T20:00:00Z","amount":100
}' | j; echo

curl -sS -X POST "$API/plays" -H 'Content-Type: application/json' -d '{
  "code":"P-0002","work_code":"W-001","occurred_at":"2026-02-11T20:00:00Z","amount":101
}' | j; echo

curl -sS -X POST "$API/plays" -H 'Content-Type: application/json' -d '{
  "code":"P-0003","work_code":"W-002","occurred_at":"2026-02-12T20:00:00Z","amount":250
}' | j; echo

echo "== 8. same code + same content is idempotent (HTTP 200) =="
curl -sS -o /tmp/idem.json -w "HTTP %{http_code}\n" -X POST "$API/plays" \
  -H 'Content-Type: application/json' -d '{
  "code":"P-0001","work_code":"W-001","occurred_at":"2026-02-10T20:00:00Z","amount":100
}'
cat /tmp/idem.json | j; echo

echo "== 9. same code + different content is rejected (422) =="
curl -sS -o /tmp/diff.json -w "HTTP %{http_code}\n" -X POST "$API/plays" \
  -H 'Content-Type: application/json' -d '{
  "code":"P-0001","work_code":"W-001","occurred_at":"2026-02-10T20:00:00Z","amount":199
}'
cat /tmp/diff.json | j; echo

echo "== 10. correction: P-0001 amount 100 -> 90, new code P-0001-V2 =="
curl -sS -X POST "$API/plays" -H 'Content-Type: application/json' -d '{
  "code":"P-0001-V2","work_code":"W-001",
  "occurred_at":"2026-02-10T20:00:00Z","amount":90,
  "corrects_code":"P-0001"
}' | j; echo

echo "== 11. forking correction of the original again is rejected (422) =="
curl -sS -o /tmp/fork.json -w "HTTP %{http_code}\n" -X POST "$API/plays" \
  -H 'Content-Type: application/json' -d '{
  "code":"P-0001-ALT","work_code":"W-001",
  "occurred_at":"2026-02-10T20:00:00Z","amount":80,
  "corrects_code":"P-0001"
}'
cat /tmp/fork.json | j; echo

echo "== 12. correction chain for P-0001 (root -> tail) =="
curl -sS "$API/plays/P-0001/chain" | j; echo

echo "== 13. dry-run settlement Jan-Mar with per-play detail =="
curl -sS "$API/settlement/compute?period_from=2026-01-01T00:00:00Z&period_to=2026-04-01T00:00:00Z&detail=true" | j; echo

echo "== 14. issue initial statement for 2026 Q1 =="
curl -sS -X POST "$API/settlement/statements" -H 'Content-Type: application/json' -d '{
  "period_from":"2026-01-01T00:00:00Z",
  "period_to":"2026-04-01T00:00:00Z"
}' | j; echo

echo "== 15. issuing the same period again with no changes -> 409 =="
curl -sS -o /tmp/dup.json -w "HTTP %{http_code}\n" -X POST "$API/settlement/statements" \
  -H 'Content-Type: application/json' -d '{
  "period_from":"2026-01-01T00:00:00Z",
  "period_to":"2026-04-01T00:00:00Z"
}'
cat /tmp/dup.json | j; echo

echo "== 16. late agreement for W-002 (retroactive), then adjustment statement =="
curl -sS -X POST "$API/agreements" -H 'Content-Type: application/json' -d '{
  "work_code":"W-002",
  "valid_from":"2026-01-01T00:00:00Z",
  "valid_to":"2027-01-01T00:00:00Z",
  "shares":[
    {"rightsholder_code":"RH-001","ratio":"40"},
    {"rightsholder_code":"RH-002","ratio":"60"}
  ]
}' | j; echo

curl -sS -X POST "$API/settlement/statements" -H 'Content-Type: application/json' -d '{
  "period_from":"2026-01-01T00:00:00Z",
  "period_to":"2026-04-01T00:00:00Z"
}' | j; echo

echo "== 17. list all statements =="
curl -sS "$API/settlement/statements" | j; echo

echo "Done. Inspect individual documents via /api/v1/settlement/statements/{id}"
