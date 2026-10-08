# Replica-count reconciler for docker-compose.ha.yml (development only:
# it drives Docker through the mounted socket).
FROM docker:27-cli
RUN apk add --no-cache curl jq
COPY reconcile.sh /usr/local/bin/reconcile.sh
RUN chmod 0755 /usr/local/bin/reconcile.sh
ENTRYPOINT ["/usr/local/bin/reconcile.sh"]
