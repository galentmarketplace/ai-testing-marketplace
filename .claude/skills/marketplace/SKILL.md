---
name: marketplace
description: Verify-as-you-build with the AI Testing Marketplace — run a testing playbook (functional / security / performance / coverage) against the current repo or a Jira ticket via the marketplace MCP tools, then read the gates, PR and results.
---
# AI Testing Marketplace — verify as you build

Use the `ai-testing-marketplace` MCP server (register: `claude mcp add ai-testing-marketplace -- python -m src.mcp_server`;
backend must be running, default `http://127.0.0.1:8090`).

## Workflow
1. **Discover** — `list_playbooks` (functional · full · perf · security · coverage · regression · unit) and
   `list_configurations` (saved Jira + app + repo + Jenkins targets; secrets stay server-side).
2. **Run** — `start_run(playbook, ticket?, configuration_id?, acceptance_criteria?, inputs?, mock=false)`.
   Functional needs a Jira ticket (AC pulled live) or pasted AC. Perf accepts `inputs.perf_type`
   (load|stress|soak|spike|breakpoint) and `k8s_namespace`/`k8s_selector` for per-pod CPU/mem.
3. **Wait** — poll `run_status(run_id)` until `done|blocked|error` (runs execute in the background).
4. **Read** — `run_results(run_id)`: gates (+checks, `advisory` ones inform only), PR url, Jenkins,
   functional case count, security summary, artifacts (SARIF, LCOV/Cobertura, JUnit, YAML intent).

## Rules
- Never "fix" a failing gate by weakening an assertion; the oracle check will flag it as a false pass.
- Treat `test.fixme(... needs triage ...)` as a **product** finding to raise with the team, not a test bug.
- Prefer a saved Configuration over pasting URLs/credentials into inputs.
