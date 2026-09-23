# AI Testing Marketplace for VS Code

Agentic testing, from your editor. A fleet of AI agents turns a **Jira ticket** into **functional test cases**, generates **Playwright automation grounded in the real app**, self-heals it to green, runs **Jenkins CI**, opens a **PR**, and posts results back to the ticket — plus multi-methodology **security scans**, enterprise **performance tests** (open-model k6, Core Web Vitals, per-pod CPU/memory) and **Go code coverage** with standard exports.

This extension is a thin client of the open **AI Testing Marketplace** platform: run it locally or point at your team's hosted instance.

## Features
- **Playbooks · Configurations · Runs** sidebar with live run status.
- **▶ Run Playbook…** — pick a saved Configuration (Jira + app + repos + Jenkins), a Jira ticket, live or mock. Track it in the status bar.
- **Results** — the platform's *Ticket → Production* journey (gates, evidence, artifacts, PR, Jenkins) inside VS Code.
- **CodeLens** on Playwright specs — *Heal / regenerate with AI Testing Marketplace*.
- **Go coverage overlay** — paints LCOV line coverage in the gutter (green hit / red missed).
- **Agent mode (MCP)** — one command registers the marketplace as tools for **GitHub Copilot agent mode, Cursor and Claude Code**: `start_run`, `run_results`, …

## Requirements
- A running **AI Testing Marketplace backend** — local (`python -m web.server`, default `http://127.0.0.1:8090`) or hosted. The extension checks on startup and offers *Start local backend* / *Configure URL* / *Setup guide*.
- VS Code **1.101+** (MCP provider API; the `.vscode/mcp.json` fallback works on older builds).

## Quick start
1. Install the extension → follow the **Get started** walkthrough (Command Palette: *ATM: Open setup guide*).
2. Set **Backend URL** (or **Platform Path** + *Start local backend*).
3. In the platform dashboard, sign in and create a token under **Configuration → API tokens**, then run *ATM: Set API token* (stored in VS Code's secret storage — the backend authorises every call).
3. *ATM: Run Playbook…* → **Functional** → Configuration → Jira ticket.
4. *ATM: Register MCP server* to drive it from Copilot/Cursor agent mode.

## Settings
| Setting | Default | Purpose |
|---|---|---|
| `aiTestingMarketplace.backendUrl` | `http://127.0.0.1:8090` | Platform backend |
| `aiTestingMarketplace.platformPath` | workspace folder | Checkout used to start the backend / MCP server / find coverage |
| `aiTestingMarketplace.pythonPath` | `python` | Interpreter for backend + MCP server |

## Commands
`ATM: Set API token` · `ATM: Run Playbook…` · `ATM: Open Run Results` · `ATM: Open Dashboard` · `ATM: Refresh` · `ATM: Register MCP server` · `ATM: Show Go coverage overlay (LCOV)` · `ATM: Start local backend` · `ATM: Open setup guide`

## Privacy
The extension only talks to the backend URL you configure, authenticated with your personal API token (kept in VS Code SecretStorage, never in settings or files). Credentials (Jira, app logins, Jenkins) live encrypted in the platform's Configurations — never in the extension.

## License
MIT
