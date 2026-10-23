#!/usr/bin/env bash
# deploy/inspect-ports.sh — READ-ONLY look at which services are reachable
# from the internet, and how the Docker containers publish their ports.
# Changes nothing.
#   bash /opt/vb/deploy/inspect-ports.sh
set -u
h() { printf '\n===== %s =====\n' "$*"; }

h "Listening on all interfaces (reachable from outside unless a firewall blocks it)"
ss -ltnpH 2>/dev/null | awk '$4 ~ /^(0\.0\.0\.0|\*|\[::\]):/ {print}' \
  | sed -E 's/users:\(\("([^"]+)".*/\1/' | awk '{printf "  %-22s %s\n", $4, $6}' | sort -u

h "Firewall on the server (ufw / nftables)"
command -v ufw >/dev/null && ufw status verbose 2>&1 | head -20 || echo "  ufw not installed"
nft list ruleset 2>/dev/null | grep -cE 'chain|rule' | sed 's/^/  nftables lines: /'
echo "  (Note: ports published by Docker bypass ufw. A Hetzner Cloud Firewall, if you set one"
echo "   up in the Hetzner console, isn't visible from here.)"

h "Docker containers and their published ports"
docker ps --format '  {{.Names}}\t{{.Image}}\t{{.Ports}}' 2>/dev/null

h "How each container was started (Compose project / file)"
for c in $(docker ps -q 2>/dev/null); do
  docker inspect -f '  {{.Name}}  project={{index .Config.Labels "com.docker.compose.project"}}  dir={{index .Config.Labels "com.docker.compose.project.working_dir"}}  files={{index .Config.Labels "com.docker.compose.project.config_files"}}  restart={{.HostConfig.RestartPolicy.Name}}' "$c"
done

h "Port lines in those Compose files"
for f in $(docker ps -q 2>/dev/null | xargs -r docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' | tr ',' '\n' | sort -u); do
  [ -f "$f" ] || continue
  echo "--- $f"
  grep -nE '^\s*(ports:|- *"?[0-9.:]*[0-9]+:[0-9]+|[a-z_-]+:$)' "$f" | sed 's/^/  /'
  # an .env next to it may hold the port numbers (only port-ish lines, no secrets)
  [ -f "$(dirname "$f")/.env" ] && grep -nE '^[A-Z_]*PORT[A-Z_]*=' "$(dirname "$f")/.env" | sed 's/^/  .env: /'
done

h "Where Caddy forwards to (reverse_proxy lines)"
grep -rnE 'reverse_proxy|^[^#[:space:]].*\{\s*$' /etc/caddy/ 2>/dev/null | grep -v '\.bak' | sed 's/^/  /'

h "Printer service (cups)"
systemctl is-active cups cups-browsed 2>/dev/null | paste - - | sed 's/^/  cups, cups-browsed: /'
systemctl is-enabled cups cups-browsed 2>/dev/null | paste - - | sed 's/^/  enabled: /'

echo
echo "Done. Nothing was changed. Paste everything above into the chat."
