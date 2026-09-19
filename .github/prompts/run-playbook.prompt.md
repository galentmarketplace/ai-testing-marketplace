---
mode: agent
description: Run an AI Testing Marketplace playbook against this repo / a Jira ticket and report the results.
tools: ['ai-testing-marketplace']
---
You are driving the AI Testing Marketplace through its MCP tools (`list_playbooks`, `list_configurations`,
`start_run`, `run_status`, `run_results`).

1. Call `list_configurations`. If one matches this repo/app, use its id; otherwise ask me for the target.
2. Ask which playbook (default **functional**) and, for functional, the **Jira ticket key** (its acceptance
   criteria are pulled live) — or paste acceptance criteria.
3. `start_run` with `mock: false`. Poll `run_status` every ~10s until `done|blocked|error`.
4. Call `run_results` and summarise: each gate's verdict + failing checks, the PR url, Jenkins result,
   coverage/security numbers, and any `test.fixme` product-mismatch findings.
5. If a gate failed, propose the concrete fix in this repo (never weaken assertions to force a pass).
