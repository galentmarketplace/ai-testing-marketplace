# Deploying the AI Testing Marketplace

Three levels of "real", each with what it needs.

## 0. Local / mock (works today, no keys)
```bash
docker compose up --build      # → http://localhost:8090
```
Agents generate **real, repo-specific artifacts** (k6 from real endpoints, Playwright
from real routes) but run in simulated execution. No API key, no cost.

## 1. Real Claude reasoning (needs an API key)
Richer generation + real self-heal diagnosis. The code already calls Claude when the
key is present (`src/llm.py`); the analyzed repo surface is passed into the prompts.
```bash
MOCK_LLM=0 ANTHROPIC_API_KEY=sk-ant-... docker compose up --build
```
**Credential gate:** an Anthropic API key.

## 2. Real execution (needs tools + the target app running)
Actually runs the generated tests and gates on real pass/fail (`REAL_RUNNER=1`, in
`src/runner/executor.py`).
- **k6** on PATH (`brew install k6`) for perf.
- **Playwright** (`npx playwright install`) for functional/regression.
- The **target app running** with `BASE_URL` / `TOKEN` reachable. For IDURAR:
  its own stack (Node 20 + MongoDB) — see `repos/idurar/INSTALLATION-INSTRUCTIONS.md`.
```bash
REAL_RUNNER=1 BASE_URL=https://your-app TOKEN=... MOCK_LLM=0 ANTHROPIC_API_KEY=... \
  docker compose up --build
```

## AWS free-tier hosting
Pick one (all Free-Tier-eligible for ~12 months):

| Option | Service | Notes |
|--------|---------|-------|
| **Simplest** | **EC2 t3.micro** | `git clone` + `docker compose up -d`. Open port 8090 in the security group. |
| **Managed** | **App Runner** | Point at this repo/image; set env vars in the console. Auto TLS + scaling. |
| **Container** | **ECS Fargate** | Push the image to ECR, run one task. Add an ALB for HTTPS. |

Minimal EC2 path:
```bash
# on a fresh Amazon Linux 2023 t3.micro
sudo yum install -y docker git && sudo systemctl start docker
git clone <this-repo> && cd agentic-testing-pipeline
sudo MOCK_LLM=0 ANTHROPIC_API_KEY=sk-ant-... docker compose up -d --build
# security group: allow inbound TCP 8090 (or put nginx/ALB in front for 443)
```

**Credential gate:** an AWS account + credentials. I can generate the ECR push
script / Terraform / an App Runner `apprunner.yaml` on request, but **provisioning
needs your AWS credentials** — I won't create cloud resources without them.

## Secrets
Never bake `ANTHROPIC_API_KEY` or app tokens into the image. Use compose env / an
`.env` file (git-ignored), or the platform's secret store (SSM Parameter Store,
App Runner secrets).
