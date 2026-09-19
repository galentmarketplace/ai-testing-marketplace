## Give your coding agent the marketplace
**Register MCP server** writes `.vscode/mcp.json` (and, on VS Code ≥ 1.101, registers the server directly). Copilot agent mode, Cursor and Claude Code then see these tools:

- `list_playbooks` · `list_agents` · `list_configurations`
- `start_run(playbook, ticket, configuration_id, …)`
- `run_status(run_id)` · `run_results(run_id)`

Try in Copilot agent mode: *"Run the functional playbook for ticket QA-1 using the SauceDemo configuration and summarise the gates."*

A ready-made prompt file ships with the platform at `.github/prompts/run-playbook.prompt.md`.
