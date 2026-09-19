# Agentic Testing Pipeline — Prototype

Multi-agent test orchestration skeleton (LangGraph + Claude). Companion to
`agentic-testing-marketplace-architecture.md`. Target app: [IDURAR ERP/CRM](https://github.com/idurar/idurar-erp-crm).

## What it does

Takes a user story and runs it through the full pipeline:

```
generate_ac → generate_code → run_unit_tests → UNIT gate
  → generate_scripts (Playwright + k6) → execute → QG1
  → select_regression (tag-based) → execute → QG2
  → open_pr
```

Every gate failure routes back to the responsible agent with failure context
(self-correction), capped at `MAX_RETRIES`, then blocks for human review.

## Quick start

```bash
pip install -r requirements.txt

# 1. Demo with canned LLM responses — no API key, no cost:
MOCK_LLM=1 python -m src.main

# 2. Real Claude calls:
cp .env.example .env   # add your ANTHROPIC_API_KEY
python -m src.main
python -m src.main --story my_story.json   # your own story
```

Generated artifacts land in `generated/` (code, e2e specs, k6 scripts).
Full final state is dumped to `pipeline_result.json`.

## Web UI

A live dashboard that runs the real pipeline and streams every agent/gate step
into the browser as it executes (via Server-Sent Events).

```bash
pip install -r requirements.txt          # includes fastapi + uvicorn

MOCK_LLM=1 python -m web.server           # demo, no API key needed
# python -m web.server                    # real Claude (needs .env)

# then open http://127.0.0.1:8000
```

Features: edit the story and run it, watch the pipeline graph light up node by
node, inspect generated acceptance criteria, quality-gate verdicts, test-run
results, click any generated artifact to view its code, and see the drafted PR.
Toggle "Mock mode" off in the UI to make real Claude calls.

Backend: `web/server.py` (FastAPI, streams `graph.stream(...)`).
Frontend: `web/static/index.html` (self-contained, no build step).

## Project map

| Path | What | Design doc § |
|------|------|--------------|
| `src/state.py` | **Data contracts** (AC, TestArtifact, RunResult, GateDecision) — read this first | §4.3 |
| `src/graph.py` | **The Orchestrator** — LangGraph nodes + conditional edges | §4.2 |
| `src/agents/` | AC, Dev, UI Automation, Perf, Regression, PR agents | §2 |
| `src/gates/quality_gates.py` | Gate policy evaluation + routing (plain code, no LLM) | §4.4 |
| `src/runner/executor.py` | Test runner (simulated; `REAL_RUNNER=1` hooks sketched) | step 7 |
| `src/llm.py` | Anthropic client + strict JSON parsing + mock mode | — |
| `src/config.py` | Gate thresholds, retry caps | §4.4 |

## What's real vs stubbed

**Real:** the orchestration graph, gate logic, retry/blocked routing, state
contracts, LLM prompts for all six agents, artifact file writing.

**Stubbed (your build order, matching design doc phases):**

1. **Runner** (`REAL_RUNNER=1`) — wire Jest/Playwright/k6 subprocess calls and
   parse their JSON reports into `RunResult`. Needs IDURAR running
   (`docker compose up` in the IDURAR repo) + `npm init playwright@latest` + k6 installed.
   *Do this first — it turns the demo into a real system.*
2. **Jira input** — replace `--story` JSON with a Jira fetch (your Jira MCP or REST API).
3. **Human approval interrupt** after `generate_ac` — LangGraph `interrupt()`.
4. **UI agent grounding** — feed a page-object map of IDURAR into the prompt so
   selectors match the real DOM; add the run-fail-regenerate self-heal loop.
5. **PR agent** — `gh pr create` / GitHub API with a branch of generated artifacts.
6. **Dashboard + feedback loop** (steps 10/12) — persist state to a DB, Allure/ReportPortal.

## Learning guide (start-from-scratch reading order)

1. `src/state.py` — agents only talk through typed state
2. `src/agents/ac_agent.py` — the simplest agent: prompt → JSON → validated contract
3. `src/graph.py` — how nodes/edges/conditional routing express the pipeline
4. `src/gates/quality_gates.py` — why gates are code, not LLM judgment
5. `src/agents/dev_agent.py` — self-correction: failures fed back on retry
6. Run `MOCK_LLM=1 python -m src.main` and follow the log against the graph
