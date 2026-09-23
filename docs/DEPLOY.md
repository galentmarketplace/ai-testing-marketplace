# Deploying the AI Testing Marketplace

Two shapes, depending on what you need:

| | Single VM + Docker Compose | Kubernetes |
|---|---|---|
| Effort | ~15 min | ~1 hour |
| Public URL for the VS Code extension | ✅ | ✅ |
| **Per-pod CPU/memory performance validation** | ❌ (no cluster) | ✅ |
| Cost on Oracle Always Free | $0 | $0 |

Start with the VM if you just need a hosted instance; add Kubernetes when you want the per-pod
capacity story.

---
## Option A — single VM (Docker Compose)

Works on any Linux box; an **Oracle Cloud Always Free ARM (Ampere A1, up to 4 OCPU / 24 GB)** instance
runs it at no cost, forever.

```bash
# on the VM
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
git clone <your-repo> && cd agentic-testing-pipeline
cp .env.example .env && nano .env          # ANTHROPIC_API_KEY, PUBLIC_URL, GitHub OAuth…
sudo docker compose up --build -d
sudo docker compose logs -f marketplace    # wait for the healthcheck
```
Open firewall port 8090 (Oracle: the instance's security list **and** `iptables`/`ufw`), or put
Caddy/nginx in front for TLS. Then set `PUBLIC_URL=https://your-host` and restart.

**State lives in the `marketplace-data` volume** — it holds the SQLite DB *and* the Fernet
`secret.key`. Back it up; if you lose the key, saved Configuration secrets can't be decrypted:
```bash
sudo docker run --rm -v marketplace-data:/d -v "$PWD":/b alpine tar czf /b/marketplace-data.tgz -C /d .
```

---
## Option B — Kubernetes (enables per-pod performance)

Any cluster works: **k3s on a free ARM VM**, **Oracle OKE** (free control plane), GKE, AKS, EKS.

```bash
# 1. a cluster (k3s single node is the cheapest real one)
curl -sfL https://get.k3s.io | sh -
sudo cat /etc/rancher/k3s/k3s.yaml > ~/.kube/config     # then edit the server IP if remote

# 2. metrics-server — REQUIRED for `kubectl top` (k3s ships it; others:)
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl top pod -A          # must return numbers before per-pod perf works

# 3. build & push the platform image
docker build -t ghcr.io/galentmarketplace/ai-testing-marketplace:0.1.0 .
docker push ghcr.io/galentmarketplace/ai-testing-marketplace:0.1.0
#    then set that image in deploy/k8s/30-platform.yaml

# 4. secrets from your .env (never commit a filled Secret)
kubectl create namespace ai-testing
kubectl -n ai-testing create secret generic marketplace-env --from-env-file=.env

# 5. deploy
kubectl apply -k deploy/k8s
kubectl -n ai-testing rollout status deploy/marketplace
kubectl -n ai-testing get ingress          # your public host
```

### What each manifest does
| File | Purpose |
|---|---|
| `10-rbac.yaml` | ServiceAccount + read-only ClusterRole for `pods` and `metrics.k8s.io` — **in-cluster per-pod sampling needs no kubeconfig** |
| `30-platform.yaml` | PVC (SQLite + secret.key), Deployment (1 replica, `Recreate`), Service, probes on `/api/manifest` |
| `40-ingress.yaml` | Public host. `proxy-read-timeout: 3600` + `proxy-buffering: off` so the **SSE run stream isn't cut** |
| `50-sample-app.yaml` | Demo app-under-test **with explicit CPU/memory limits** — the perf report measures utilization against them |

### Per-pod performance run
In the Performance playbook: **API base URL** = the deployed app, **K8s namespace** = `ai-testing`,
**Pod label selector** = `app=sample-app`, test type `breakpoint` for the single-pod ceiling.
For a true ceiling keep that Deployment at `replicas: 1` (we never auto-scale your cluster).
Leave the kubeconfig field empty when the platform runs in-cluster.

---
## Wire up the rest
- **GitHub OAuth** — create an OAuth app with callback `<PUBLIC_URL>/auth/github/callback`; put the
  client id/secret in `.env`/the Secret and set `PUBLIC_URL` to the same host.
- **VS Code extension** — users set `aiTestingMarketplace.backendUrl` to your `PUBLIC_URL`; then no
  local setup is needed and the published extension works out of the box.
- **Jenkins** (optional) — set `JENKINS_URL/USER/TOKEN/UI_JOB`; it must be reachable from the pod.

## Hardening before real customers
- TLS at the ingress; the platform has **no auth of its own beyond GitHub OAuth** — don't expose it
  publicly without it.
- Back up the PVC (see above). Rotate `ANTHROPIC_API_KEY` and any tokens shared in chat.
- Optional scanners make coverage wider: `gitleaks`, `trivy`, `syft`, `osv-scanner`, `go`
  (add to the Dockerfile; each is auto-detected and reported as "available to enable" when absent).

## Scaling
Single replica is deliberate: SQLite on an RWO volume. To scale out, move the store to
Postgres (`web/store.py` is the only SQL layer) and switch `Recreate` → `RollingUpdate`.
