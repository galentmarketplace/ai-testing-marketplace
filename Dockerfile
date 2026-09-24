# AI Testing Marketplace — the platform image.
# Carries the agents' real tools so a deployed instance can actually test:
#   git (clone target repos) · node + Playwright/Chromium (UI automation, DOM/a11y exploration,
#   Core Web Vitals) · k6 (open-model load tests) · kubectl (per-pod CPU/mem sampling) · semgrep (SAST).
# Optional tools (gitleaks, trivy, syft, osv-scanner, go) are detected at runtime and reported as
# "available to enable" when absent — the image stays lean and nothing silently breaks.
FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NODE_MAJOR=20

RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl gnupg ca-certificates ttf-mscorefonts-installer fontconfig \
 || apt-get install -y --no-install-recommends git curl gnupg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Node 20 (Playwright runner + explorer scripts)
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

# k6 (performance) and kubectl (per-pod resource sampling) — arch-aware
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64) K6=amd64; KUBE=amd64 ;; arm64) K6=arm64; KUBE=arm64 ;; *) echo "unsupported $arch"; exit 1 ;; esac; \
    curl -fsSL "https://github.com/grafana/k6/releases/download/v0.54.0/k6-v0.54.0-linux-${K6}.tar.gz" \
      | tar -xz --strip-components=1 -C /usr/local/bin "k6-v0.54.0-linux-${K6}/k6"; \
    curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/v1.31.0/bin/linux/${KUBE}/kubectl"; \
    chmod +x /usr/local/bin/k6 /usr/local/bin/kubectl; \
    k6 version; kubectl version --client=true

WORKDIR /app

# Python deps first (layer cache)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Playwright + Chromium for the e2e runner (browsers live in the image)
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY e2e-runner/package.json e2e-runner/package-lock.json ./e2e-runner/
RUN cd e2e-runner && npm ci --no-audit --no-fund \
 && npx playwright install --with-deps chromium

COPY . .

# Persisted state: data/ holds the SQLite DB AND the Fernet secret.key (losing it makes saved
# Configuration secrets undecryptable) — always mount a volume here.
RUN mkdir -p data generated repos
VOLUME ["/app/data", "/app/generated", "/app/repos"]

# Run as a non-root user; the state dirs it must write are chowned below.
RUN useradd --create-home --uid 10001 atm && chown -R atm:atm /app /ms-playwright
USER atm

ENV PYTHONPATH=/app \
    PORT=8090 \
    MOCK_LLM=0 \
    REAL_RUNNER=1
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8090\")}/api/manifest',timeout=4)" || exit 1

CMD ["python", "-m", "web.server"]
