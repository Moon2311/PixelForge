# PostgreSQL 16 managed by Patroni (HA cluster in docker-compose.ha.yml).
# Patroni comes from the PGDG apt repository the postgres image already uses.
FROM postgres:16

RUN apt-get update \
    && apt-get install -y --no-install-recommends patroni python3-etcd curl \
    && rm -rf /var/lib/apt/lists/*

COPY patroni.yml /etc/patroni/patroni.yml
COPY entrypoint.sh /usr/local/bin/patroni-entrypoint.sh
COPY post_bootstrap.sh /usr/local/bin/pixelforge-post-bootstrap.sh
RUN chmod 0755 /usr/local/bin/patroni-entrypoint.sh /usr/local/bin/pixelforge-post-bootstrap.sh

USER postgres
EXPOSE 5432 8008
ENTRYPOINT ["/usr/local/bin/patroni-entrypoint.sh"]
