# Changelog

## 0.2.0 — 2026-09-23
- Authenticates to the platform with a personal API token (VS Code SecretStorage); *ATM: Set API token*.
- MCP registration prompts for the token (never written to `.vscode/mcp.json`).

## 0.1.0 — 2026-09-16
- Sidebar: Playbooks · Configurations · Runs (live status).
- **Run Playbook…** wizard: saved Configuration → Jira ticket / acceptance criteria → live or mock; status-bar tracking.
- Results webview reusing the platform's *Ticket → Production* journey dashboard.
- CodeLens on `*.spec.ts`: *Heal / regenerate with AI Testing Marketplace*.
- **Go coverage overlay** from LCOV (green = hit, red = missed).
- **MCP registration** for Copilot agent mode / Cursor / Claude Code (`.vscode/mcp.json` + native provider).
- First-run onboarding: backend health check, *Start local backend*, setup walkthrough.
