#!/bin/sh
# Desired-state loop for the local HA cluster: 1 primary + DESIRED_REPLICAS
# healthy replicas. A development stand-in for what an orchestrator (a
# Kubernetes operator, an ASG, a managed database service) does in production.
#
# It never promotes or demotes anything: Patroni + etcd decide who is
# primary. It only watches the cluster (Patroni REST API via HAProxy),
# logs what happens as JSON lines, and changes the number of nodes:
#
#   * fewer running replica nodes than desired, while a primary exists
#     -> start a replacement node (it clones the current primary);
#   * a replica stuck unhealthy longer than PROVISION_GRACE_SECONDS
#     -> ask Patroni to reinitialise it from the primary;
#   * more healthy replicas than desired (e.g. an old primary rejoined)
#     -> retire a node this reconciler started (never a compose-defined one,
#        never the primary), if SCALE_DOWN_SURPLUS=true.
#
# FAILOVER_ENABLED=false puts Patroni in maintenance mode (no automatic
# failover); true takes it out.
set -u

API="${PATRONI_API:-http://haproxy:8008}"
DESIRED="${DESIRED_REPLICAS:-3}"
INTERVAL="${RECONCILE_INTERVAL:-5}"
MAX_LAG_BYTES="${REPLICA_MAX_LAG_BYTES:-1048576}"
GRACE="${PROVISION_GRACE_SECONDS:-120}"
FAILOVER_ENABLED="${FAILOVER_ENABLED:-true}"
SCALE_DOWN="${SCALE_DOWN_SURPLUS:-true}"
NODE_LABEL="com.pixelforge.ha.node=true"
OWNED_LABEL="com.pixelforge.ha.provisioned-by=reconciler"
STATE_DIR="$(mktemp -d)"

log() {  # log <event> [key=value ...] -> one JSON line
    event="$1"; shift
    line="$(jq -cn --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg event "$event" \
        '{ts: $ts, component: "ha-reconciler", event: $event}')"
    for kv in "$@"; do
        line="$(printf '%s' "$line" | jq -c --arg k "${kv%%=*}" --arg v "${kv#*=}" '. + {($k): $v}')"
    done
    printf '%s\n' "$line"
}

members() {  # members <jq filter on .members[]> -> names, one per line
    printf '%s' "$cluster" | jq -r ".members[] | $1 | .name" | sort
}

healthy_filter='select(.role != "leader") | select(.state == "streaming" or .state == "running")
    | select((.lag | if type == "number" then . else 0 end) <= '"$MAX_LAG_BYTES"')'

candidates_with_lsn() {  # replicas with their received WAL position, e.g. "pg1@0/5297CF8 pg3@0/5297CF8"
    printf '%s' "$cluster" | jq -r '[.members[] | select(.role != "leader")
        | "\(.name)@\(.receive_lsn // .lsn // "?")"] | join(" ")'
}

apply_failover_setting() {
    paused="$(printf '%s' "$cluster" | jq -r '.pause // false')"
    if [ "$FAILOVER_ENABLED" = "false" ] && [ "$paused" != "true" ]; then
        curl -sf -X PATCH -d '{"pause": true}' "$API/config" >/dev/null \
            && log failover_disabled reason="FAILOVER_ENABLED=false (Patroni maintenance mode)"
    elif [ "$FAILOVER_ENABLED" != "false" ] && [ "$paused" = "true" ]; then
        curl -sf -X PATCH -d '{"pause": false}' "$API/config" >/dev/null && log failover_enabled
    fi
}

template_node() {  # a running node container to copy image/network/env from
    docker ps -q --filter "label=$NODE_LABEL" | head -n 1
}

provision() {
    template="$(template_node)"
    [ -n "$template" ] || { log provision_skipped reason="no running node to copy"; return; }
    image="$(docker inspect -f '{{.Config.Image}}' "$template")"
    network="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$template" | awk '{print $1}')"
    project="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$template")"
    node="pg-r$(date +%s)"
    volume="${project}_${node}_data"
    envfile="$(mktemp)"
    chmod 600 "$envfile"
    # Cluster-wide settings and secrets only; name/addresses come from the hostname.
    docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$template" \
        | grep -E '^(PATRONI_(SCOPE|NAMESPACE|ETCD3_HOSTS|SUPERUSER_|REPLICATION_|REWIND_)|APP_DB_)' > "$envfile"
    docker volume create --label "$OWNED_LABEL" --label "com.docker.compose.project=$project" "$volume" >/dev/null
    if docker run -d --name "$node" --hostname "$node" --network "$network" --network-alias pgnode \
        --label "$NODE_LABEL" --label "$OWNED_LABEL" \
        --label "com.docker.compose.project=$project" --label "com.docker.compose.service=pg-replacement" \
        --env-file "$envfile" -v "$volume:/var/lib/postgresql/data" --restart unless-stopped \
        "$image" >/dev/null; then
        log replacement_replica_provisioned node="$node" image="$image" \
            replicas_running="$replicas_running" desired="$DESIRED"
    else
        log provision_failed node="$node"
        docker volume rm "$volume" >/dev/null 2>&1
    fi
    rm -f "$envfile"
}

retire_surplus() {
    owned="$(docker ps --filter "label=$OWNED_LABEL" --format '{{.CreatedAt}}|{{.Names}}' | sort -r | cut -d'|' -f2)"
    for node in $owned; do
        [ "$node" = "$leader" ] && continue
        printf '%s\n' "$healthy" | grep -qx "$node" || continue
        volume="$(docker inspect -f '{{range .Mounts}}{{.Name}}{{end}}' "$node")"
        docker rm -f "$node" >/dev/null && docker volume rm "$volume" >/dev/null 2>&1
        log replica_retired node="$node" reason="surplus" healthy_replicas="$healthy_count" desired="$DESIRED"
        return
    done
    log surplus_kept reason="no reconciler-provisioned replica to retire" healthy_replicas="$healthy_count"
}

reinit_stuck() {
    now="$(date +%s)"
    for node in $(members 'select(.role != "leader")'); do
        if printf '%s\n' "$healthy" | grep -qx "$node"; then
            rm -f "$STATE_DIR/unhealthy.$node"
            continue
        fi
        [ -f "$STATE_DIR/unhealthy.$node" ] || { echo "$now" > "$STATE_DIR/unhealthy.$node"; continue; }
        since="$(cat "$STATE_DIR/unhealthy.$node")"
        if [ $((now - since)) -ge "$GRACE" ]; then
            state="$(printf '%s' "$cluster" | jq -r --arg n "$node" '.members[] | select(.name == $n) | .state')"
            curl -sf -X POST -d '{"force": true}' "http://$node:8008/reinitialize" >/dev/null \
                && log replica_reinitialized node="$node" state="$state" unhealthy_seconds="$((now - since))"
            echo "$now" > "$STATE_DIR/unhealthy.$node"
        fi
    done
}

log reconciler_started desired_replicas="$DESIRED" interval_seconds="$INTERVAL" failover_enabled="$FAILOVER_ENABLED"
last_leader=""
last_candidates=""
last_healthy=""
leaderless=""
surplus_rounds=0

while true; do
    if ! cluster="$(curl -sf --max-time 3 "$API/cluster")"; then
        log ha_api_unreachable api="$API"
        sleep "$INTERVAL"; continue
    fi
    apply_failover_setting

    leader="$(members 'select(.role == "leader" and .state == "running")' | head -n 1)"
    if [ -z "$leader" ]; then
        if [ -n "$last_leader" ] && [ -z "$leaderless" ]; then
            log primary_failure_detected previous_primary="$last_leader"
            log failover_started candidates="$(candidates_with_lsn)"
            leaderless=1
        fi
        sleep "$INTERVAL"; continue  # Patroni is electing; never add nodes mid-failover
    fi

    if [ "$leader" != "$last_leader" ]; then
        timeline="$(printf '%s' "$cluster" | jq -r --arg n "$leader" '.members[] | select(.name == $n) | .timeline')"
        if [ -n "$last_leader" ]; then
            log replica_selected node="$leader" candidates_before_failure="$last_candidates" \
                rule="Patroni: healthy, within maximum_lag_on_failover, no member ahead in WAL"
            log promotion_completed new_primary="$leader" previous_primary="$last_leader" timeline="$timeline"
            log writer_endpoint_changed endpoint="haproxy:5000" primary="$leader"
        else
            log cluster_observed primary="$leader" timeline="$timeline"
        fi
        last_leader="$leader"
        leaderless=""
    fi

    last_candidates="$(candidates_with_lsn)"  # WAL positions to report if the primary fails
    healthy="$(members "$healthy_filter")"
    healthy_count="$(printf '%s' "$healthy" | grep -c . || true)"
    for node in $last_healthy; do
        printf '%s\n' "$healthy" | grep -qx "$node" || log replica_removed node="$node"
    done
    for node in $healthy; do
        printf '%s\n' "$last_healthy" | grep -qx "$node" || log replica_rejoined node="$node"
    done
    last_healthy="$healthy"

    running="$(docker ps -q --filter "label=$NODE_LABEL" | wc -l | tr -d ' ')"
    replicas_running=$((running - 1))
    missing=$((DESIRED - replicas_running))
    while [ "$missing" -gt 0 ]; do
        provision
        missing=$((missing - 1))
        sleep 1  # distinct node names
    done

    reinit_stuck

    if [ "$SCALE_DOWN" = "true" ] && [ "$healthy_count" -gt "$DESIRED" ]; then
        surplus_rounds=$((surplus_rounds + 1))
        [ "$surplus_rounds" -ge 3 ] && { retire_surplus; surplus_rounds=0; }
    else
        surplus_rounds=0
    fi

    sleep "$INTERVAL"
done
