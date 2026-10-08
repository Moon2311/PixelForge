#!/bin/sh
# Start Patroni for this node. The node's name and the addresses other
# members, HAProxy and the reconciler use default to the container hostname,
# so a replacement node only needs a new hostname.
set -eu

NODE="${PATRONI_NAME:-$(hostname)}"
export PATRONI_NAME="$NODE"
export PATRONI_RESTAPI_CONNECT_ADDRESS="${PATRONI_RESTAPI_CONNECT_ADDRESS:-$NODE:8008}"
export PATRONI_POSTGRESQL_CONNECT_ADDRESS="${PATRONI_POSTGRESQL_CONNECT_ADDRESS:-$NODE:5432}"

exec patroni /etc/patroni/patroni.yml
