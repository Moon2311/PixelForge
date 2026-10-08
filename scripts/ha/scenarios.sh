#!/usr/bin/env bash
# Failure scenarios against the running HA cluster (docker-compose.ha.yml).
#
#   docker compose -f docker-compose.ha.yml up -d --build
#   scripts/ha/scenarios.sh            # all scenarios
#   scripts/ha/scenarios.sh 1 6 7      # some of them
#
# Every scenario asserts its outcome; the script exits non-zero on the first
# failure. It changes the cluster (kills and restarts nodes, pauses
# replication, stops Redis, cuts a node's network) and waits for it to
# heal, so it ends with 1 primary + DESIRED_REPLICAS healthy replicas.
# Needs: docker, curl and psql (client) on the host; jq too, or it uses the
# reconciler container's jq.
set -euo pipefail

cd "$(dirname "$0")/../.."
DC=(docker compose -f docker-compose.ha.yml)
export PROJECT=pixelforge-ha
API="http://127.0.0.1:${HA_BACKEND_PORT:-8001}"
PATRONI="http://127.0.0.1:${HA_PATRONI_API_PORT:-8008}"
export STATS="http://127.0.0.1:${HA_STATS_PORT:-8404}/;csv"
WRITER_PORT="${HA_WRITER_PORT:-5000}"
READER_PORT="${HA_READER_PORT:-5001}"
DESIRED="${DESIRED_REPLICAS:-3}"
RUN="$(date +%s)"  # makes every written value unique to this run
export PGPASSWORD="${HA_APP_DB_PASSWORD:-pixelforge}"
PSQL=(psql -h 127.0.0.1 -U pixelforge -d pixelforge -qAt -v ON_ERROR_STOP=1)

# ---------------------------------------------------------------- helpers

if ! command -v jq >/dev/null; then
    jq() { docker exec -i "${PROJECT}-reconciler-1" jq "$@"; }
    export -f jq
fi

passed=0
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
ok() { passed=$((passed + 1)); printf '   \033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '   \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
check() { local what="$1"; shift; if "$@"; then ok "$what"; else fail "$what"; fi; }

cluster() { curl -sf --max-time 3 "$PATRONI/cluster"; }
leader() { cluster | jq -r '[.members[] | select(.role == "leader" and .state == "running")][0].name // empty'; }
healthy_replicas() {
    cluster | jq -r '.members[] | select(.role != "leader" and .state == "streaming")
        | select((.lag | if type == "number" then . else 0 end) <= 1048576) | .name' | sort
}
container() {  # member name -> container name (compose node or reconciler-provisioned node)
    if docker inspect "$1" >/dev/null 2>&1; then echo "$1"; else echo "${PROJECT}-$1-1"; fi
}
node_sql() { docker exec -u postgres "$(container "$1")" psql -qAt -d pixelforge -c "$2"; }
ip_of() { docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$(container "$1")"; }
node_of_ip() {
    for c in $(docker ps -q --filter label=com.pixelforge.ha.node=true); do
        if [ "$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$c")" = "$1" ]; then
            docker inspect -f '{{.Config.Hostname}}' "$c"; return
        fi
    done
}
writer_node() { node_of_ip "$("${PSQL[@]}" -p "$WRITER_PORT" -c 'SELECT host(inet_server_addr())')"; }
reader_node() { node_of_ip "$("${PSQL[@]}" -p "$READER_PORT" -c 'SELECT host(inet_server_addr())')"; }
writable_nodes() {  # every running node that accepts writes (split brain if > 1)
    for c in $(docker ps -q --filter label=com.pixelforge.ha.node=true); do
        if [ "$(docker exec -u postgres "$c" psql -qAt -d postgres -c 'SELECT NOT pg_is_in_recovery()' 2>/dev/null)" = "t" ]; then
            docker inspect -f '{{.Config.Hostname}}' "$c"
        fi
    done
}
wait_for() {  # wait_for <seconds> <description> <command...>
    local timeout="$1" what="$2"; shift 2
    local start; start=$(date +%s)
    until "$@" >/dev/null 2>&1; do
        if [ $(( $(date +%s) - start )) -ge "$timeout" ]; then fail "timed out after ${timeout}s: $what"; fi
        sleep 1
    done
    info "$what after $(( $(date +%s) - start ))s"
}
steady() { [ -n "$(leader)" ] && [ "$(healthy_replicas | wc -l)" -ge "$DESIRED" ]; }
wait_steady() { wait_for "${1:-240}" "cluster back to 1 primary + $DESIRED healthy replicas" steady; }

token() {
    curl -sf -X POST "$API/api/auth/login/" -H 'Content-Type: application/json' \
        -d '{"username":"admin","password":"Admin@123"}' | jq -r .data.access_token
}
api() { curl -s --max-time 10 "$@"; }
patch_category() {  # patch_category <name> -> HTTP status
    api -o /dev/null -w '%{http_code}' -X PATCH "$API/api/catalog/categories/$CATEGORY/" \
        -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "{\"name\": \"$1\"}"
}
category_name() { api "$API/api/catalog/categories/$CATEGORY/" "$@" | jq -r .data.name; }
admin_category_name() { category_name -H "Authorization: Bearer $TOKEN"; }
reader_up_count() {  # reader servers HAProxy currently sends connections to
    curl -sf "$STATS" | awk -F, 'NR == 1 { sub(/^# /, ""); for (i = 1; i <= NF; i++) col[$i] = i; next }
        $1 == "reader" && $2 ~ /^slot/ && $col["status"] == "UP" { n++ } END { print n + 0 }'
}
export -f reader_up_count
walreceiver_pid() {
    docker exec "$(container "$1")" bash -c \
        'for p in /proc/[0-9]*; do tr "\0" " " < $p/cmdline 2>/dev/null | grep -q "^postgres: .*walreceiver" && echo ${p#/proc/}; done; true' | sed -n 1p
}
# Replication stall: freeze (SIGSTOP) every replica's WAL receiver, as if the
# network to the primary hung. (pg_wal_replay_pause() can't simulate lag
# here: Patroni resumes paused replay on its next loop.)
STALLED=""
stall_replication() {
    for n in $(healthy_replicas); do
        local pid; pid="$(walreceiver_pid "$n")"
        if [ -n "$pid" ]; then
            docker exec -u postgres "$(container "$n")" kill -STOP "$pid"
            STALLED="$STALLED $n:$pid"
        fi
    done
    info "stalled WAL receiving on:$STALLED"
}
resume_replication() {
    for entry in $STALLED; do
        docker exec -u postgres "$(container "${entry%%:*}")" kill -CONT "${entry#*:}" 2>/dev/null || true
    done
    STALLED=""
}
trap resume_replication EXIT

# ---------------------------------------------------------------- setup

setup() {
    say "Setup: waiting for the cluster and seeding demo data"
    wait_for 240 "Patroni elected a primary" bash -c "[ -n \"\$(curl -sf $PATRONI/cluster | jq -r '.members[] | select(.role==\"leader\" and .state==\"running\") | .name')\" ]"
    wait_steady
    wait_for 240 "Django answers through the endpoints" curl -sf "$API/api/health/database/"
    "${DC[@]}" exec -T backend python manage.py seed_users >/dev/null
    "${DC[@]}" exec -T backend python manage.py seed_catalog >/dev/null 2>&1 || true
    if [ "$("${PSQL[@]}" -p "$WRITER_PORT" -c 'SELECT count(*) FROM apps_product')" -lt 5 ]; then
        "${DC[@]}" exec -T backend python manage.py seed_inventory --count 10 >/dev/null 2>&1
    fi
    TOKEN="$(token)"
    CATEGORY="$(api "$API/api/catalog/categories/?page_size=1" | jq -r '.data[0].id')"
    PRODUCT="$(api "$API/api/catalog/products/?page_size=1" | jq -r '.data[0].id')"
    info "primary=$(leader) replicas=$(healthy_replicas | tr '\n' ' ') category=$CATEGORY product=$PRODUCT"
}

# ---------------------------------------------------------------- scenarios

scenario_1() {
    say "Scenario 1: normal operation (1 primary + $DESIRED replicas)"
    local primary; primary="$(leader)"
    check "writer endpoint reaches the primary ($primary)" [ "$(writer_node)" = "$primary" ]
    check "exactly one writable node" [ "$(writable_nodes | wc -l)" -eq 1 ]
    check "write through Django succeeds" [ "$(patch_category "Phones s1")" = 200 ]
    local seen; seen="$(for _ in $(seq 30); do reader_node; done | sort | uniq -c)"
    info "reader endpoint, 30 new connections: $(echo "$seen" | tr '\n' ' ')"
    check "reader endpoint spreads over every replica" [ "$(echo "$seen" | wc -l)" -eq "$DESIRED" ]
    check "reader endpoint never returns the primary" bash -c "! echo '$seen' | grep -qw '$primary'"
    for _ in $(seq 40); do api -o /dev/null "$API/api/search/products/?q=a"; done
    local metrics; metrics="$(api "$API/api/health/database/" | jq -c .data.routing_metrics.db_reader_routing_total)"
    info "Django reader routing (one worker's counters): $metrics"
    check "Django serves catalog reads from the reader endpoint" bash -c "echo '$metrics' | grep -q '\"replica\"'"
    check "health: cluster healthy, writer is primary" [ "$(api "$API/api/health/database/" | jq -r '.data.cluster_status + "/" + .data.writer_role')" = "healthy/primary" ]
}

scenario_3() {
    say "Scenario 3: replica failure"
    VICTIM="$(healthy_replicas | sed -n 1p)"
    local up_before; up_before="$(reader_up_count)"
    info "killing replica $VICTIM (reader servers UP: $up_before)"
    docker kill "$(container "$VICTIM")" >/dev/null
    wait_for 30 "HAProxy dropped $VICTIM from the reader pool" bash -c "[ \"\$(reader_up_count)\" -lt $up_before ]"
    local seen; seen="$(for _ in $(seq 20); do reader_node; done | sort -u | tr '\n' ' ')"
    info "reader endpoint now serves: $seen"
    check "$VICTIM gets no reads" bash -c "! echo '$seen' | grep -qw '$VICTIM'"
    check "reads through Django continue" [ "$(api -o /dev/null -w '%{http_code}' "$API/api/search/products/?q=a")" = 200 ]
    check "writes unaffected" [ "$(patch_category "Phones s3")" = 200 ]
}

scenario_4() {
    say "Scenario 4: replica recovery"
    docker start "$(container "$VICTIM")" >/dev/null
    wait_for 120 "$VICTIM streaming again" bash -c "curl -sf $PATRONI/cluster | jq -e '.members[] | select(.name==\"$VICTIM\" and .state==\"streaming\")'"
    wait_for 60 "$VICTIM caught up (lag 0)" bash -c "curl -sf $PATRONI/cluster | jq -e '.members[] | select(.name==\"$VICTIM\" and .lag==0)'"
    wait_for 30 "$VICTIM back in the reader endpoint" bash -c "for i in \$(seq 20); do PGPASSWORD=$PGPASSWORD psql -h 127.0.0.1 -p $READER_PORT -U pixelforge -d pixelforge -qAt -c 'SELECT host(inet_server_addr())'; done | grep -qx '$(ip_of "$VICTIM")'"
    check "$VICTIM is a read-only standby" [ "$(node_sql "$VICTIM" 'SELECT pg_is_in_recovery()')" = t ]
    wait_steady
}

scenario_6_7() {
    say "Scenarios 6 + 7: read-after-write and replica lag (replication stalled on every replica)"
    api -o /dev/null "$API/api/products/$PRODUCT/"  # cache the product document
    local before; before="$(api "$API/api/products/$PRODUCT/" | jq -r .data.name)"
    local new="New Name $(date +%s)"  # unique per run
    stall_replication
    local status
    status="$(api -o /dev/null -w '%{http_code}' -X PATCH "$API/api/catalog/products/$PRODUCT/" \
        -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "{\"name\": \"$new\"}")"
    check "product.name = '$new' written" [ "$status" = 200 ]
    local replica_row; replica_row="$(node_sql "$(healthy_replicas | sed -n 1p)" "SELECT name FROM apps_product WHERE id = $PRODUCT")"
    info "replica row (replication stalled): '$replica_row' (was '$before')"
    check "replicas really are stale" [ "$replica_row" != "$new" ]
    check "writer immediately reads '$new' (LSN marker -> primary)" \
        [ "$(api "$API/api/catalog/products/$PRODUCT/" -H "Authorization: Bearer $TOKEN" | jq -r .data.name)" = "$new" ]
    check "anonymous cache refill is not stale (fence -> primary)" \
        [ "$(api "$API/api/products/$PRODUCT/" | jq -r .data.name)" = "$new" ]
    local cached; cached="$("${DC[@]}" exec -T redis redis-cli -n 1 get "pixelforge:product:$PRODUCT" | jq -r .v.name)"
    check "Redis holds '$new', not the replica's '$replica_row'" [ "$cached" = "$new" ]
    info "waiting past REPLICA_MAX_LAG (10 s) with replication still stalled"
    sleep 12
    local pool; pool="$(api "$API/api/health/database/" | jq -r .data.replica.status)"
    check "Django takes the lagging reader out of rotation" [ "$pool" = lagging ]
    check "reads keep working (from the primary)" [ "$(api -o /dev/null -w '%{http_code}' "$API/api/search/products/?q=a")" = 200 ]
    check "catalog reads now come from the primary" [ "$(api "$API/api/health/database/" | jq -r .data.reads)" = primary ]
    resume_replication
    wait_for 60 "replicas caught up and back in rotation" bash -c "[ \"\$(curl -sf $API/api/health/database/ | jq -r .data.replica.status)\" = ok ]"
}

scenario_8() {
    say "Scenario 8: Redis failure (replication stalled, so a replica read would be stale)"
    stall_replication
    "${DC[@]}" stop redis >/dev/null 2>&1
    check "write succeeds without Redis" [ "$(patch_category "Written without Redis $RUN")" = 200 ]
    check "writer still reads its write (no marker -> primary, never a stale replica)" \
        [ "$(admin_category_name)" = "Written without Redis $RUN" ]
    check "anonymous reads still work" [ "$(api -o /dev/null -w '%{http_code}' "$API/api/search/products/?q=a")" = 200 ]
    check "health reports Redis down as degraded, not unhealthy" \
        [ "$(api "$API/api/health/" | jq -r '.data.status + "/" + .data.checks.redis.status')" = "degraded/error" ]
    resume_replication
    "${DC[@]}" start redis >/dev/null 2>&1
    sleep 2
}

scenario_2() {
    say "Scenario 2: primary failure -> healthiest replica promoted"
    OLD_PRIMARY="$(leader)"
    local replicas behind
    replicas="$(healthy_replicas)"
    behind="$(echo "$replicas" | sed -n 1p)"
    local pid; pid="$(walreceiver_pid "$behind")"
    info "freezing WAL receiving on $behind (pid $pid) so it falls behind"
    docker exec -u postgres "$(container "$behind")" kill -STOP "$pid"
    for i in $(seq 5); do patch_category "Before failover $i" >/dev/null; done
    sleep 3
    cluster | jq -r '.members[] | "   \(.name) \(.role) receive=\(.receive_lsn // "-") replay=\(.replay_lsn // "-") lag=\(.lag // "-")"'
    local start; start=$(date +%s)
    info "killing primary $OLD_PRIMARY (SIGKILL: no clean shutdown, its leader key must expire)"
    docker kill "$(container "$OLD_PRIMARY")" >/dev/null
    wait_for 90 "a new primary was elected" bash -c "l=\$(curl -sf $PATRONI/cluster | jq -r '.members[] | select(.role==\"leader\" and .state==\"running\") | .name'); [ -n \"\$l\" ] && [ \"\$l\" != '$OLD_PRIMARY' ]"
    NEW_PRIMARY="$(leader)"
    info "promoted $NEW_PRIMARY in $(( $(date +%s) - start ))s"
    docker exec -u postgres "$(container "$behind")" kill -CONT "$pid" || true
    check "the replica that was behind ($behind) was not promoted" [ "$NEW_PRIMARY" != "$behind" ]
    check "promotion picked one of the up-to-date replicas" bash -c "echo '$replicas' | grep -qx '$NEW_PRIMARY'"
    wait_for 30 "writer endpoint reaches $NEW_PRIMARY" bash -c "[ \"\$(PGPASSWORD=$PGPASSWORD psql -h 127.0.0.1 -p $WRITER_PORT -U pixelforge -d pixelforge -qAt -c 'SELECT host(inet_server_addr())')\" = '$(ip_of "$NEW_PRIMARY")' ]"
    check "exactly one writable node (no split brain)" [ "$(writable_nodes | tr '\n' ' ')" = "$NEW_PRIMARY " ]
    wait_for 60 "Django writes through the same writer endpoint" bash -c "[ \"\$(curl -s -o /dev/null -w '%{http_code}' -X PATCH $API/api/catalog/categories/$CATEGORY/ -H 'Authorization: Bearer $TOKEN' -H 'Content-Type: application/json' -d '{\"name\": \"After failover $RUN\"}')\" = 200 ]"
    check "the write landed on $NEW_PRIMARY" [ "$(node_sql "$NEW_PRIMARY" "SELECT name FROM apps_category WHERE id = $CATEGORY")" = "After failover $RUN" ]
    check "writer reads its write after failover" [ "$(admin_category_name)" = "After failover $RUN" ]
    local timeline; timeline="$(cluster | jq -r --arg n "$NEW_PRIMARY" '.members[] | select(.name == $n) | .timeline')"
    info "new timeline: $timeline"
    wait_for 240 "reconciler provisioned a replacement (1 + $DESIRED again)" steady
    info "members: $(cluster | jq -r '[.members[] | "\(.name):\(.role):\(.state)"] | join(" ")')"
    check "health: writer is $NEW_PRIMARY" [ "$(api "$API/api/health/database/" | jq -r .data.writer.node)" = "$NEW_PRIMARY" ]
    "${DC[@]}" logs --no-log-prefix reconciler 2>/dev/null | grep -E 'primary_failure_detected|failover_started|replica_selected|promotion_completed|writer_endpoint_changed|replacement_replica_provisioned' | tail -n 6 | sed 's/^/   /'
}

scenario_5() {
    say "Scenario 5: old primary ($OLD_PRIMARY) comes back"
    docker start "$(container "$OLD_PRIMARY")" >/dev/null
    wait_for 180 "$OLD_PRIMARY rejoined as a streaming replica" bash -c "curl -sf $PATRONI/cluster | jq -e '.members[] | select(.name==\"$OLD_PRIMARY\" and .role==\"replica\" and .state==\"streaming\")'"
    check "$OLD_PRIMARY is read-only (in recovery)" [ "$(node_sql "$OLD_PRIMARY" 'SELECT pg_is_in_recovery()')" = t ]
    check "$OLD_PRIMARY follows the new timeline" [ "$(cluster | jq -r --arg n "$OLD_PRIMARY" '.members[] | select(.name == $n) | .timeline')" = "$(cluster | jq -r '.members[] | select(.role == "leader") | .timeline')" ]
    check "still exactly one writable node" [ "$(writable_nodes | wc -l)" -eq 1 ]
    check "writer endpoint did not move back to $OLD_PRIMARY" [ "$(writer_node)" = "$(leader)" ]
    "${DC[@]}" logs --no-log-prefix "$OLD_PRIMARY" 2>/dev/null | grep -iE 'rewind|reinitializ|demot|timeline' | tail -n 4 | sed 's/^/   /' || true
    wait_for 120 "surplus replica retired (back to 1 + $DESIRED)" bash -c "[ \"\$(curl -sf $PATRONI/cluster | jq '[.members[] | select(.role != \"leader\")] | length')\" -eq $DESIRED ]"
}

scenario_9() {
    say "Scenario 9: network partition (primary cut off from etcd, HAProxy and replicas)"
    local isolated; isolated="$(leader)"
    local network; network="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$(container "$isolated")")"
    local start; start=$(date +%s)
    docker network disconnect "$network" "$(container "$isolated")"
    info "disconnected $isolated; sampling writable nodes every second"
    local max_writable=0 stopped="" readonly="" elected=""
    for _ in $(seq 90); do
        local w; w="$(writable_nodes | wc -l)"
        [ "$w" -gt "$max_writable" ] && max_writable="$w"
        # Stops accepting writes = no longer answers as a primary (shut down or read-only).
        local role; role="$(node_sql "$isolated" 'SELECT pg_is_in_recovery()' 2>/dev/null || true)"
        [ -z "$stopped" ] && [ "$role" != f ] && stopped=$(( $(date +%s) - start ))
        [ -z "$readonly" ] && [ "$role" = t ] && readonly=$(( $(date +%s) - start ))
        local l; l="$(leader 2>/dev/null || true)"
        [ -z "$elected" ] && [ -n "$l" ] && [ "$l" != "$isolated" ] && elected=$(( $(date +%s) - start ))
        [ -n "$stopped" ] && [ -n "$elected" ] && [ -n "$readonly" ] && break
        sleep 1
    done
    info "isolated primary stopped accepting writes after ${stopped:-?}s (back up read-only after ${readonly:-?}s);" \
        "new primary elected after ${elected:-?}s"
    check "never more than one writable node during the partition" [ "$max_writable" -le 1 ]
    check "the isolated primary stopped accepting writes" [ -n "$stopped" ]
    check "it stopped before the new primary was elected" [ -n "$elected" ] && [ "$stopped" -le "$elected" ]
    check "the majority side elected a new primary" [ -n "$elected" ]
    wait_for 60 "writes through Django succeed on the majority side" bash -c "[ \"\$(curl -s -o /dev/null -w '%{http_code}' -X PATCH $API/api/catalog/categories/$CATEGORY/ -H 'Authorization: Bearer $TOKEN' -H 'Content-Type: application/json' -d '{\"name\": \"During partition $RUN\"}')\" = 200 ]"
    docker network connect --alias pgnode "$network" "$(container "$isolated")"
    wait_for 180 "$isolated healed and rejoined as a replica" bash -c "curl -sf $PATRONI/cluster | jq -e '.members[] | select(.name==\"$isolated\" and .role==\"replica\" and .state==\"streaming\")'"
    check "exactly one writable node after healing" [ "$(writable_nodes | wc -l)" -eq 1 ]
    wait_steady
}

final() {
    say "Final state"
    cluster | jq -r '.members[] | "   \(.name)\t\(.role)\t\(.state)\ttimeline \(.timeline)\tlag \(.lag // "-")"'
    check "1 primary + $DESIRED healthy replicas" steady
    check "Django writes through the writer endpoint" [ "$(patch_category "Phones")" = 200 ]
    local health; health="$(api "$API/api/health/database/")"
    info "health: $(echo "$health" | jq -c '.data | {cluster_status, writer: .writer.node, writer_role, replica_count, healthy_replica_count, desired_replica_count, failover: .failover_state.state, promotions: .failover_state.promotions}')"
    printf '\n\033[32mAll %s checks passed.\033[0m\n' "$passed"
}

setup
for s in "${@:-1 3 4 6 8 2 5 9}"; do
    for one in $s; do
        case "$one" in
            1) scenario_1 ;; 3) scenario_3 ;; 4) scenario_4 ;; 6|7) scenario_6_7 ;; 8) scenario_8 ;;
            2) scenario_2 ;; 5) scenario_5 ;; 9) scenario_9 ;;
            *) fail "unknown scenario $one" ;;
        esac
    done
done
final
