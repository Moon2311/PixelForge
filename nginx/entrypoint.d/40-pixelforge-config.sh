#!/bin/sh
# Renders the PixelForge load balancer config before nginx starts (run by
# the official image's /docker-entrypoint.sh).
#
#   BACKEND_UPSTREAMS   "backend-1:8000,backend-2:8000" (comma or space separated)
#   NGINX_TLS_CERT/KEY  HTTPS on :443 when both files exist
#   NGINX_REDIRECT_HTTPS=true  redirect plain HTTP to HTTPS (needs the certificate)
#   NGINX_HSTS=true     Strict-Transport-Security on HTTPS responses
set -eu

templates=/etc/nginx/pixelforge-templates
out=/etc/nginx/pixelforge
mkdir -p "$out"
echo ok > "$out/health.txt"

: "${BACKEND_UPSTREAMS:?BACKEND_UPSTREAMS is required}"
: "${NGINX_MAX_FAILS:=3}"
: "${NGINX_FAIL_TIMEOUT:=10s}"
: "${NGINX_PROXY_CONNECT_TIMEOUT:=3s}"
: "${NGINX_PROXY_READ_TIMEOUT:=65s}"
: "${NGINX_CLIENT_MAX_BODY_SIZE:=25m}"
: "${NGINX_TLS_CERT:=/etc/nginx/certs/tls.crt}"
: "${NGINX_TLS_KEY:=/etc/nginx/certs/tls.key}"
: "${NGINX_REDIRECT_HTTPS:=false}"
: "${NGINX_HSTS:=false}"
export NGINX_MAX_FAILS NGINX_FAIL_TIMEOUT NGINX_PROXY_CONNECT_TIMEOUT NGINX_PROXY_READ_TIMEOUT \
  NGINX_CLIENT_MAX_BODY_SIZE NGINX_TLS_CERT NGINX_TLS_KEY

# Upstream servers. `resolve` re-resolves the names through Docker's DNS, so a
# restarted instance with a new IP is found again; max_fails/fail_timeout
# take an instance out of rotation after repeated errors (passive checks).
: > "$out/upstream-servers.conf"
for server in $(echo "$BACKEND_UPSTREAMS" | tr ',' ' '); do
  echo "server $server max_fails=$NGINX_MAX_FAILS fail_timeout=$NGINX_FAIL_TIMEOUT resolve;" >> "$out/upstream-servers.conf"
done

# Only our variables: nginx's own $variables stay untouched.
vars='${NGINX_PROXY_CONNECT_TIMEOUT} ${NGINX_PROXY_READ_TIMEOUT} ${NGINX_CLIENT_MAX_BODY_SIZE} ${NGINX_TLS_CERT} ${NGINX_TLS_KEY}'
envsubst "$vars" < "$templates/proxy.conf.template" > "$out/proxy.conf"

tls=false
if [ -f "$NGINX_TLS_CERT" ] && [ -f "$NGINX_TLS_KEY" ]; then
  tls=true
fi

if [ "$tls" = true ] && [ "$NGINX_REDIRECT_HTTPS" = true ]; then
  echo 'location / { return 308 https://$host$request_uri; }' > "$out/http-locations.conf"
else
  echo 'include /etc/nginx/pixelforge/proxy.conf;' > "$out/http-locations.conf"
fi

envsubst "$vars" < "$templates/pixelforge.conf.template" > /etc/nginx/conf.d/default.conf
if [ "$tls" = true ]; then
  envsubst "$vars" < "$templates/tls.conf.template" >> /etc/nginx/conf.d/default.conf
  if [ "$NGINX_HSTS" = true ]; then
    echo 'add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;' > "$out/hsts.conf"
  else
    : > "$out/hsts.conf"
  fi
  echo "pixelforge: HTTPS enabled ($NGINX_TLS_CERT)"
else
  echo "pixelforge: no certificate at $NGINX_TLS_CERT; serving HTTP only"
fi
echo "pixelforge: upstreams: $(tr '\n' ' ' < "$out/upstream-servers.conf")"
