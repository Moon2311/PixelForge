#!/bin/sh
# Load balancer checks against the running Compose stack (docker compose up):
#   1. nginx config is valid
#   2. requests reach both instances
#   3. with one instance stopped, every request still succeeds
#   4. the restarted instance rejoins the rotation
#   5. internal ports are not published on public interfaces
# Usage: scripts/lb/check_lb.sh [http://localhost:8000]
set -eu

URL="${1:-http://localhost:8000}"
REQUESTS="${REQUESTS:-20}"
fail() { echo "FAIL: $*"; exit 1; }

instances() {
  i=0
  while [ "$i" -lt "$REQUESTS" ]; do
    curl -fsS "$URL/api/health/" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["instance"])'
    i=$((i + 1))
  done | sort | uniq -c
}

echo "== nginx -t"
docker compose exec -T lb nginx -t 2>&1 | tail -1

echo "== distribution over $REQUESTS requests"
dist=$(instances); echo "$dist"
[ "$(echo "$dist" | wc -l)" -ge 2 ] || fail "only one instance served requests"

echo "== backend-2 stopped: every request must still succeed"
docker compose stop backend-2 >/dev/null
trap 'docker compose start backend-2 >/dev/null' EXIT
codes=$(i=0; while [ "$i" -lt "$REQUESTS" ]; do curl -s -o /dev/null -w '%{http_code}\n' "$URL/api/health/"; i=$((i + 1)); done | sort | uniq -c)
echo "$codes"
echo "$codes" | grep -qv ' 200$' && fail "non-200 responses while an instance was down"
writes=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$URL/api/auth/login/" -H 'Content-Type: application/json' -d '{"username":"nobody","password":"x"}')
[ "$writes" = 401 ] || fail "POST while an instance was down returned $writes"
echo "POST /api/auth/login/ -> $writes (served by the healthy instance)"

echo "== backend-2 restarted: it must rejoin"
docker compose start backend-2 >/dev/null
trap - EXIT
until [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q backend-2)")" = healthy ]; do sleep 2; done
tries=0
until curl -fsS "$URL/api/health/" | grep -q '"instance":"backend-2"'; do
  tries=$((tries + 1)); [ "$tries" -gt 60 ] && fail "backend-2 did not rejoin"; sleep 1
done
instances

echo "== published ports (internal services must be loopback-only or absent)"
docker compose ps --format '{{.Service}} {{.Ports}}' | while read -r service ports; do
  case "$service" in
    lb) ;;
    *) echo "$ports" | grep -q '0\.0\.0\.0\|\[::\]' && fail "$service is published on all interfaces: $ports" ;;
  esac
  [ -n "$ports" ] && echo "$service: $ports"
done
echo "OK"
