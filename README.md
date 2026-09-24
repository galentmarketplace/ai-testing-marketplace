# AI Testing Marketplace

An agentic testing platform: a fleet of AI agents turns a **Jira ticket** into **functional test cases**,
generates **Playwright automation grounded in the real app**, self-heals it to green, runs **Jenkins CI**,
opens a **pull request**, and posts the results back to the ticket — plus multi-methodology **security
scanning**, enterprise **performance testing** (open-model k6, Core Web Vitals, per-pod CPU/memory) and
**Go code coverage** with standard exports.

Ships with a **VS Code extension** and an **MCP server**, so the same capabilities are available inside
the editor and to coding agents (Copilot agent mode, Cursor, Claude Code).

## How it works

Every capability is one declarative `AgentSpec` in `src/registry.py`. A generic dependency scheduler
(`src/orchestrator.py`) runs the active plan: agents produce artifacts, runners execute them, gates judge
the results, and a failing gate loops the responsible agent back until it passes or the run blocks.
Adding an agent means appending a spec — no edits to the orchestrator, server or UI.

**Sources of truth, never guesses:** requirements come from Jira, routes and the login path are derived
from the application's own code, locators come from the running app's accessibility tree, and Jenkins is
the final verdict. An **oracle check** rejects any "fix" that makes a test pass without still asserting
the acceptance criterion.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # add ANTHROPIC_API_KEY (and GitHub OAuth if you want sign-in)
PORT=8090 python -m web.server  # → http://127.0.0.1:8090
```

Or with Docker: `docker compose up --build`. For a cluster, see [docs/DEPLOY.md](docs/DEPLOY.md).

Mock mode (`MOCK_LLM=1`) runs the whole pipeline with canned responses — no API key, no cost — which is
what CI uses to prove every gate still passes.

## Access

Every `/api/*` route needs a principal: a GitHub-OAuth browser session, or an `Authorization: Bearer`
API token minted in the dashboard (**Configuration → API tokens**) for the extension, the MCP server and
CI. Runs and Configurations are owner-scoped. Project secrets and OAuth tokens are encrypted at rest;
child processes run with an allow-listed environment so customer test code never sees platform
credentials.

## The agents

Prescreen · Acceptance Criteria · Functional Cases · **Playwright Agent** (DOM + accessibility grounded,
trace-fed healing) · **Oracle Check** (rejects false passes) · Performance (k6 + Web Vitals + per-pod
CPU/memory) · Security (SAST · SCA/CVE · secrets · IaC · containers · SBOM → SARIF) · **Go Coverage** +
Go Test Gen · Unit · Regression · Accessibility · API Contract · Jenkins CI · Publish Branch · Jira Update
· Self-Heal. Browse them in the dashboard's **Agents** view or via the MCP `list_agents` tool.

## Documentation

| | |
|---|---|
| [docs/STANDARDS.md](docs/STANDARDS.md) | agent output contract, gate vocabulary, report formats, access control |
| [docs/DEPLOY.md](docs/DEPLOY.md) | single VM or Kubernetes, with per-pod performance |
| [docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md) | gap analysis and the phased plan |
| [docs/PUBLISHING.md](docs/PUBLISHING.md) | publishing the VS Code extension |
| [vscode-extension/](vscode-extension/) | the editor integration |

## Development

```bash
python -m pytest tests -q     # platform tests (authorization, isolation, run control)
ruff check src web tests      # lint
```

CI runs both plus a full mock pipeline on every push.
