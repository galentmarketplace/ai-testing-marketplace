#!/usr/bin/env bash
# Bring the AI Testing Marketplace up on a bare Ubuntu/Debian VPS, end to end.
#
#   scp deploy/vps-bootstrap.sh you@your-vps:~ && ssh you@your-vps 'bash ~/vps-bootstrap.sh'
#
# This is the cheapest always-on option: a ~4 GB VPS runs the platform, Chromium and Jenkins
# together for a few euros a month, with no platform-specific configuration to maintain.
#
# It is idempotent — safe to re-run to pick up a new release.
set -euo pipefail

REPO="${ATM_REPO:-https://github.com/galentmarketplace/ai-testing-marketplace.git}"
DIR="${ATM_DIR:-$HOME/ai-testing-marketplace}"
PORT="${PORT:-8090}"

log() { printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight
[ "$(id -u)" -eq 0 ] && die "run as a normal user with sudo, not as root (the container runs unprivileged)"

MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
if [ "$MEM_MB" -lt 1900 ]; then
  die "this host has ${MEM_MB} MB of RAM. Chromium needs ~2 GB, and a smaller box kills
  Playwright mid-run, which looks like flaky tests rather than an out-of-memory error.
  Resize to at least 2 GB (4 GB if Jenkins runs here too) and re-run."
fi
log "host has ${MEM_MB} MB RAM — enough"

# ---------------------------------------------------------------- docker
if ! command -v docker >/dev/null 2>&1; then
  log "installing Docker"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  NEED_RELOGIN=1
fi
docker compose version >/dev/null 2>&1 || die "docker compose v2 is required"

# ---------------------------------------------------------------- source
if [ -d "$DIR/.git" ]; then
  log "updating $DIR"
  git -C "$DIR" pull --ff-only
else
  log "cloning into $DIR"
  git clone "$REPO" "$DIR"
fi
cd "$DIR"

# ---------------------------------------------------------------- configuration
if [ ! -f .env ]; then
  cp .env.example .env
  # A stable encryption key must exist BEFORE first start: it encrypts stored credentials,
  # and regenerating it later makes every saved secret unreadable.
  python3 -c "import secrets;print('ATM_SECRET_KEY='+secrets.token_urlsafe(32))" >> .env
  python3 -c "import secrets;print('ATM_API_TOKEN='+secrets.token_urlsafe(32))" >> .env
  cat <<'MSG'

  A .env was created from the example, with a fresh encryption key and API token.
  Fill in the real values before the platform is useful:

      ANTHROPIC_API_KEY   required for any model-driven run
      GITHUB_TOKEN        a PAT with `repo` scope
      PUBLIC_URL          the URL this host is reachable on (OAuth callbacks need it)

  Then re-run this script.

MSG
  exit 0
fi

grep -q '^ATM_SECRET_KEY=' .env || die "ATM_SECRET_KEY missing from .env — stored credentials cannot be encrypted without it"

# ---------------------------------------------------------------- run
log "building and starting (first build downloads Chromium; allow ~10 minutes)"
PORT="$PORT" docker compose up -d --build

log "waiting for the platform to answer"
for _ in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/api/manifest" || true)
  [ "$code" = "200" ] && break
  sleep 5
done
[ "${code:-}" = "200" ] || { docker compose logs --tail=40 marketplace; die "did not become healthy"; }

log "up on http://127.0.0.1:${PORT}"
docker compose ps
cat <<MSG

Next:
  * put it behind TLS (Caddy or nginx) before exposing ${PORT} publicly — the platform
    stores credentials and must not be reached over plain HTTP
  * set PUBLIC_URL in .env to that HTTPS address and re-run, so OAuth callbacks resolve
  * logs:    docker compose logs -f marketplace
  * upgrade: re-run this script

MSG
[ "${NEED_RELOGIN:-0}" = "1" ] && echo "  NOTE: log out and back in so your user picks up docker group membership"
exit 0
