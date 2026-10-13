# TLS certificates for the load balancer

nginx serves HTTPS on port 443 (host port 8443 in Docker Compose) when both
`tls.crt` (full chain) and `tls.key` exist in this directory. They are
ignored by git.

Self-signed certificate for local testing:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=localhost" \
  -keyout nginx/certs/tls.key -out nginx/certs/tls.crt
docker compose restart lb
```

In production, mount certificates from your CA or ACME client (or
terminate TLS at a cloud load balancer in front of nginx, see README
"L7 load balancer").
