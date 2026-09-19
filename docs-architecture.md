# Agentic Testing Marketplace — Architecture & Design Doc

**Author:** Mohanram | **Date:** July 2026 | **Status:** Draft v1
**Target application:** [IDURAR ERP/CRM](https://github.com/idurar/idurar-erp-crm) — open-source MERN stack (Node.js/Express, MongoDB, React + Ant Design, Redux)

---

## 1. Executive Summary

A multi-agent orchestration platform that takes a **user story as input** and produces **tested, quality-gated, PR-ready code as output**. An Orchestrator Agent coordinates specialist agents: acceptance criteria generation, code + unit test generation, Playwright automation scripting, performance scripting, regression selection, and execution — with quality gates deciding whether work proceeds to a PR.

```
User Story → Acceptance Criteria → Code + Unit Tests
     → [Orchestrator] → Playwright Agent + Perf Agent (parallel)
     → Execute (Quality Gate 1) → Regression Agent (tag-based) → Execute (Quality Gate 2)
     → Results Dashboard → PR + CI/CD → Feedback Loop (Step 12)
```

---

## 2. Your Plan, Refined

Your 11 steps are fundamentally sound. Here they are with corrections, gaps filled, and a proposed Step 12.

| # | Your step | Refinement |
|---|-----------|------------|
| 1 | Setup one proper application | IDURAR ERP/CRM. Run it via Docker Compose (app + MongoDB) so agents get a clean, resettable environment. Seed data with a fixed script — deterministic test data is non-negotiable for reliable automation. |
| 2 | User story → acceptance criteria | **AC Agent.** Input: Jira story (you have Jira MCP connected — pull stories directly). Output: Gherkin-style Given/When/Then criteria, written back to the Jira ticket. Add a human-approval checkpoint here; bad AC poisons everything downstream. |
| 3 | Code generation + unit test | **Dev Agent.** Generates the feature code + Jest/Vitest unit tests against the AC. Gate: unit tests must pass + coverage threshold before handing off. |
| 4 | Orchestrator Agent | This is the **core**, not step 4 — it wraps steps 2–11. It routes work, holds shared state, enforces gates, and retries failed agents. Build this as a LangGraph graph (see §4). |
| 5 | Playwright script agent | **UI Automation Agent.** Consumes AC + a page-object map of IDURAR. Generates Playwright (TypeScript) specs tagged (`@smoke`, `@invoice`, `@regression`). Must self-validate: run the generated script against the app, and self-heal selectors on failure (bounded retries, e.g., 3). |
| 6 | Performance script agent | **Perf Agent.** Generates k6 scripts (JS — same language as your stack) against IDURAR's REST APIs. Output includes thresholds (p95 latency, error rate) that double as the perf quality gate. |
| 7 | Execute + Quality Gate | **Execution is deterministic infrastructure, not an agent.** Runner executes Playwright + k6; Orchestrator evaluates results against gate policy (e.g., 100% smoke pass, p95 < 800ms, error rate < 1%). Fail → route back to the responsible agent with the failure context. |
| 8 | Regression Agent (input: tags) | Maps the code diff + story to impacted areas, selects regression suites by tag (e.g., story touches invoices → `@invoice @regression`). Smarter than "run everything." |
| 9 | Execute tag scripts | Same runner as step 7, second gate. Catches side-effects the new feature introduced. |
| 10 | Results dashboard | Aggregate all runs into one view: per-story pipeline status, gate pass/fail, test results, perf trends, flakiness. Allure or ReportPortal + a thin custom UI reading from the Orchestrator's state DB. |
| 11 | PR route CI/CD | Only reached if both gates pass. Agent opens the PR with a summary: AC covered, tests added, gate results, perf numbers. GitHub Actions re-runs the suites on the PR as an independent check. |
| **12** | **(your blank) → Feedback & Self-Healing Loop** | The step that makes this a *system* rather than a pipeline: (a) failed-run analysis feeds back into agent prompts, (b) flaky-test detection and quarantine, (c) auto-defect creation in Jira with repro steps, (d) metrics on agent quality over time (how often did generated scripts pass first try?). |

**Ordering fix:** think of it as Orchestrator (4) at the center; 1 is environment; 2→3 is the dev lane; 5–9 is the QA lane; 10–12 are outputs/feedback.

---

## 3. Framework Recommendation: LangGraph

For a pipeline with **conditional routing and quality gates**, the fit ranking (based on current 2026 comparisons):

| Framework | Model | Verdict for this project |
|-----------|-------|--------------------------|
| **LangGraph** ✅ | Directed graph, conditional edges, checkpointed state | **Recommended.** Your pipeline *is* a graph: nodes = agents, edges = gate decisions ("tests passed → regression; failed → back to script agent"). Built-in state persistence, retries, human-in-the-loop interrupts. Leads enterprise adoption in 2026; LangSmith gives you tracing for free. Most boilerplate, but the control is exactly what quality gates need. |
| Claude Agent SDK | Orchestrator + subagents, tool-use | Strong #2, and best-in-class for the *code-generating* agents themselves. Simpler, but less fine-grained routing control. Viable hybrid: LangGraph as the skeleton, Claude-powered agents as nodes. |
| CrewAI | Role-based crews | Fastest prototype (agents in ~20 lines), but benchmarks show ~2–3x token cost of LangGraph for equivalent work, and role/delegation abstractions fit collaborative tasks better than gated pipelines. Good for a throwaway learning spike, not the build. |

**Practical stack:** LangGraph (Python) for orchestration · Claude API for agent LLM calls · Playwright (TS) · k6 · Jest · GitHub Actions · MongoDB or Postgres for orchestration state · Allure/ReportPortal for results.

---

## 4. Architecture

### 4.1 Component view

```
┌────────────────────────────────────────────────────────────┐
│                    ORCHESTRATOR (LangGraph)                │
│   shared state · routing · quality gates · retries · HITL  │
└──┬───────┬───────┬────────────┬───────────┬───────┬────────┘
   │       │       │            │           │       │
┌──▼──┐ ┌──▼──┐ ┌──▼───────┐ ┌──▼───────┐ ┌─▼────┐ ┌▼─────┐
│ AC  │ │ Dev │ │ UI Auto  │ │ Perf     │ │ Regr │ │ PR   │
│Agent│ │Agent│ │ (P'wright│ │ (k6)     │ │Agent │ │Agent │
└──┬──┘ └──┬──┘ └──┬───────┘ └──┬───────┘ └─┬────┘ └┬─────┘
   │       │       │            │           │       │
┌──▼───────▼───────▼────────────▼───────────▼───────▼──────┐
│ TOOL LAYER: Jira MCP · Git/GitHub · Test Runner (Docker) │
│ IDURAR env (app+DB) · Results store · Dashboard          │
└──────────────────────────────────────────────────────────┘
```

Agents **think** (LLM + prompts + context); the tool layer **acts** (deterministic code). Keep that boundary sharp — never let an LLM "decide" whether tests passed. The runner reports facts; gate logic is plain code.

### 4.2 Orchestrator graph (LangGraph nodes/edges)

```
fetch_story → generate_ac → [human approve?] → generate_code
  → run_unit_tests ──fail──▶ generate_code (retry ≤3)
  └─pass─▶ {generate_ui_scripts ∥ generate_perf_scripts}
  → execute_scripts → QUALITY_GATE_1 ──fail──▶ responsible agent (retry ≤3)
  └─pass─▶ select_regression → execute_regression → QUALITY_GATE_2
  ──fail──▶ analyze_failure → (fix or open defect in Jira)
  └─pass─▶ open_pr → update_dashboard → feedback_loop → END
```

### 4.3 Data contracts (what flows between agents)

Every agent reads/writes typed JSON in the shared state. Minimal set:

```jsonc
// AcceptanceCriteria (AC Agent → all downstream)
{ "storyId": "PROJ-123", "criteria": [ { "id": "AC-1",
  "gherkin": "Given... When... Then...", "priority": "must", "tags": ["invoice"] } ] }

// TestArtifact (script agents → runner)
{ "type": "playwright|k6|jest", "path": "tests/e2e/invoice-create.spec.ts",
  "coversAC": ["AC-1"], "tags": ["@invoice","@smoke"] }

// RunResult (runner → orchestrator/gates)
{ "runId": "…", "suite": "smoke", "passed": 14, "failed": 1,
  "failures": [ { "test": "…", "error": "…", "trace": "…", "screenshot": "…" } ],
  "perf": { "p95_ms": 640, "error_rate": 0.002 } }

// GateDecision (orchestrator → next route)
{ "gate": "QG1", "verdict": "fail", "reason": "smoke pass 93% < 100%",
  "routeTo": "ui_automation_agent", "attempt": 2 }
```

Defining these contracts **first** is the highest-leverage design decision — it lets you build and test agents independently.

### 4.4 Quality gate policy (starting values)

| Gate | Criteria |
|------|----------|
| Unit gate | 100% unit tests pass, coverage ≥ 70% on changed files |
| QG1 (feature) | 100% smoke pass, ≥95% feature suite pass, k6: p95 < 800ms, errors < 1% |
| QG2 (regression) | ≥98% regression pass, no failures in `@critical` |
| PR gate | CI re-run green + human review |

---

## 5. Build Order (phased — this is also your learning path)

Do **not** build all agents at once. Each phase is shippable and teaches the next concept.

**Phase 0 — Foundations (week 1):** IDURAR running in Docker Compose with seeded data. Hand-write 2–3 Playwright tests and one k6 script yourself. *You can't judge agent-generated scripts if you've never written them by hand.*

**Phase 1 — Single agent, no framework (week 2):** Python script: Jira story in → Claude API call → Gherkin AC out → posted back to Jira. Teaches prompting, structured output, tool calls. No orchestration yet.

**Phase 2 — First graph (weeks 3–4):** Wrap Phase 1 in a 3-node LangGraph: fetch → generate AC → generate Playwright script. Add the UI Automation Agent with a self-check loop (run script, feed failure back, retry). Teaches state, conditional edges, cycles — the heart of the system.

**Phase 3 — Gates + runner (weeks 5–6):** Dockerized runner executing Playwright + k6; RunResult contract; QG1 as code. Now you have story → scripts → executed → gated. **This is your MVP demo.**

**Phase 4 — Dev + Regression agents (weeks 7–9):** Code-gen agent with unit-test gate; tag-based regression selection; QG2.

**Phase 5 — Dashboard, PR, feedback (weeks 10–12):** Allure/ReportPortal + custom dashboard; PR agent + GitHub Actions; Step 12 feedback loop.

---

## 6. Key Risks

Non-determinism: LLM-generated scripts vary run-to-run — mitigate with low temperature, strict output schemas, and self-validation before accepting any script. Flaky UI tests: will erode trust in gates fastest — mitigate with quarantine list + retry-once policy from day one. Cost: every retry loop is tokens — cap retries, log token usage per agent from the start. Scope: the marketplace pitch is 12 components; the demo that earns buy-in is Phase 3.

---

## 7. Concepts to Learn (in order)

1. **Prompting for structured output** — getting reliable JSON from an LLM (Phase 1)
2. **Tool use / function calling** — how agents act on the world (Phase 1)
3. **State graphs** — LangGraph nodes, edges, conditional routing, checkpoints (Phase 2)
4. **Agent self-correction loops** — feeding execution errors back as context (Phase 2)
5. **Human-in-the-loop interrupts** — approval checkpoints (Phase 2–3)
6. **Evaluation** — measuring agent output quality over time, LangSmith tracing (Phase 3+)

Free resources: LangGraph docs + LangChain Academy's "Intro to LangGraph" course; Anthropic's "Building Effective Agents" guide; Playwright docs (codegen mode is a great learning aid); k6 docs.
