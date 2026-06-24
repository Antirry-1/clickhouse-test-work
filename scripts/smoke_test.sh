#!/usr/bin/env bash
# End-to-end smoke test: containers up, ClickHouse answers, ~1M rows, delivery-rate query
# works, Superset healthy, dashboard present. Exit code 0 = all green.
set -uo pipefail
cd "$(dirname "$0")/.."

# Load .env if present (ports / credentials)
[ -f .env ] && set -a && . ./.env && set +a
CH_USER="${CLICKHOUSE_USER:-default}"; CH_PASS="${CLICKHOUSE_PASSWORD:-clickhouse}"
SUP_PORT="${SUPERSET_PORT:-8088}"; CH_PORT="${CLICKHOUSE_HTTP_PORT:-8123}"
EXPECTED="${GEN_ROWS:-1000000}"

fail=0
pass() { echo "  ✅ $1"; }
bad()  { echo "  ❌ $1"; fail=1; }
ch()   { docker compose exec -T clickhouse clickhouse-client --user "$CH_USER" --password "$CH_PASS" --query "$1" 2>/dev/null; }

echo "── 1. Containers ──────────────────────────────────────────────"
docker compose ps
for c in sms_clickhouse sms_superset; do
  st=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)
  [ "$st" = "running" ] && pass "$c is running" || bad "$c is '$st' (expected running)"
done

echo "── 2. ClickHouse answers ──────────────────────────────────────"
ping=$(curl -fsS "http://localhost:${CH_PORT}/ping" 2>/dev/null || echo "")
[ "$ping" = "Ok." ] && pass "ClickHouse /ping = Ok." || bad "ClickHouse /ping failed"

echo "── 3. Row count (~${EXPECTED}) ────────────────────────────────"
rows=$(ch "SELECT count() FROM sms.messages_mart")
echo "  rows = ${rows:-<none>}"
if [ -n "${rows:-}" ] && [ "$rows" -ge $((EXPECTED * 99 / 100)) ]; then
  pass "row count ${rows} >= 99% of ${EXPECTED}"
else
  bad "row count ${rows:-<none>} below expectation (${EXPECTED})"
fi

echo "── 4. Period covers ~1 year ───────────────────────────────────"
ch "SELECT min(sent_date) AS first, max(sent_date) AS last FROM sms.messages_mart" | sed 's/^/  /'
days=$(ch "SELECT dateDiff('day', min(sent_date), max(sent_date)) FROM sms.messages_mart")
if [ -n "${days:-}" ] && [ "$days" -ge 330 ] && [ "$days" -le 400 ]; then
  pass "period spans ${days} days (~1 year)"
else
  bad "period spans ${days:-<none>} days (expected 330–400)"
fi

echo "── 5. Several customers present ───────────────────────────────"
cust=$(ch "SELECT uniqExact(customer_id) FROM sms.messages_mart")
if [ -n "${cust:-}" ] && [ "$cust" -ge 2 ]; then
  pass "distinct customers = ${cust} (>= 2)"
else
  bad "distinct customers = ${cust:-<none>} (expected >= 2)"
fi

echo "── 6. Key columns are not entirely empty ──────────────────────"
ok=$(ch "SELECT countIf(delivery_status IS NOT NULL) > 0 AND countIf(country IS NOT NULL) > 0 AND sum(price) > 0 AND uniqExact(customer_id) > 0 FROM sms.messages_mart")
[ "${ok:-0}" = "1" ] && pass "delivery_status / country / price / customer_id all populated" \
                     || bad "a key column is entirely empty/zero"

echo "── 7. Delivery rate (soft-delete excluded) ────────────────────"
dr=$(ch "SELECT round(100*countIf(delivery_status='DELIVRD')/count(),2) FROM sms.messages_mart_active")
[ -n "${dr:-}" ] && pass "delivery rate = ${dr}%" || bad "delivery-rate query failed"

echo "── 8. Revenue is computed ─────────────────────────────────────"
rev=$(ch "SELECT round(sum(price),2) FROM sms.messages_mart_active")
if [ -n "${rev:-}" ] && awk "BEGIN{exit !($rev > 0)}"; then
  pass "revenue (active) = ${rev}"
else
  bad "revenue = ${rev:-<none>} (expected > 0)"
fi

echo "── 9. Superset healthy ────────────────────────────────────────"
sh=$(curl -fsS "http://localhost:${SUP_PORT}/health" 2>/dev/null || echo "")
[ -n "$sh" ] && pass "Superset /health = ${sh}" || bad "Superset /health failed"

echo "── 10. SMS Operations dashboard present ───────────────────────"
tok=$(curl -fsS -X POST "http://localhost:${SUP_PORT}/api/v1/security/login" \
  -H 'Content-Type: application/json' \
  -d '{"username":"'"${SUPERSET_ADMIN:-admin}"'","password":"'"${SUPERSET_ADMIN_PASSWORD:-admin}"'","provider":"db","refresh":true}' \
  2>/dev/null | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')
if [ -n "$tok" ]; then
  q='%7B%22filters%22:%5B%7B%22col%22:%22slug%22,%22opr%22:%22eq%22,%22value%22:%22sms_operations%22%7D%5D%7D'
  found=$(curl -fsS "http://localhost:${SUP_PORT}/api/v1/dashboard/?q=${q}" -H "Authorization: Bearer ${tok}" 2>/dev/null | grep -c '"dashboard_title": *"SMS Operations"')
  [ "${found:-0}" -ge 1 ] && pass "dashboard 'SMS Operations' exists" || bad "dashboard not found"
else
  bad "could not authenticate to Superset API"
fi

echo "───────────────────────────────────────────────────────────────"
if [ "$fail" -eq 0 ]; then echo "✅ SMOKE TEST PASSED"; else echo "❌ SMOKE TEST FAILED"; fi
exit $fail
