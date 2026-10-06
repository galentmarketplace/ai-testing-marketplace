#!/usr/bin/env bash
# Bring the AI Testing Marketplace up on a bare Linux host, end to end.
# Tested shape: Ubuntu/Debian, and Amazon Linux 2023 (EC2 or Lightsail).
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
ID_LIKE=$(. /etc/os-release 2>/dev/null; echo "${ID} ${ID_LIKE}")
if ! command -v docker >/dev/null 2>&1; then
  log "installing Docker"
  case "$ID_LIKE" in
    *amzn*|*rhel*|*fedora*)
      # get.docker.com does not support Amazon Linux; its own packages are the supported path.
      sudo dnf install -y docker
      sudo systemctl enable --now docker
      ;;
    *)
      curl -fsSL https://get.docker.com | sudo sh
      ;;
  esac
  sudo usermod -aG docker "$USER"
  NEED_RELOGIN=1
fi
command -v docker >/dev/null 2>&1 || die "Docker did not install"
sudo systemctl is-active --quiet docker 2>/dev/null || sudo systemctl start docker 2>/dev/null || true

if ! docker compose version >/dev/null 2>&1; then
  # Amazon Linux's docker package ships without the compose plugin.
  log "installing the docker compose plugin"
  ARCH=$(uname -m); case "$ARCH" in aarch64) CA=aarch64 ;; x86_64) CA=x86_64 ;; *) die "unsupported arch $ARCH" ;; esac
  sudo mkdir -p /usr/local/lib/docker/cli-plugins
  sudo curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
    "https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-${CA}"
  sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fi
docker compose version >/dev/null 2>&1 || die "docker compose v2 is required and could not be installed"

# ---------------------------------------------------------------- prerequisites
# A minimal AWS image has neither; the script needs git to clone and python3 to generate
# the encryption key.
for tool in git python3; do
  command -v "$tool" >/dev/null 2>&1 && continue
  log "installing $tool"
  case "$ID_LIKE" in
    *amzn*|*rhel*|*fedora*) sudo dnf install -y "$tool" ;;
    *) sudo apt-get update -qq && sudo apt-get install -y -qq "$tool" ;;
  esac
  command -v "$tool" >/dev/null 2>&1 || die "could not install $tool"
done

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
