# Platform standards

Everything the marketplace produces follows one contract, so agents are drop-in and every output is
consumable by CI, IDEs and enterprise tooling without adapters.

## 1. Agent output contract
Every agent/runner/gate is a pure function `state -> partial state update` and returns only these keys:

| key | type | rule |
|---|---|---|
| `*_artifacts` / `test_artifacts` | `list[Artifact]` | `{type, path, tags[]}` — `type` MUST be from the registry below |
| `run_results` | `list[RunResult]` | facts only — counts + failures; **no LLM decides pass/fail** |
| `gate_decisions` | `list[GateDecision]` | `{gate, verdict, reason, checks[], route_to}` |
| `<domain>_report` | `dict` | native structured report (e.g. `security_report`, `coverage_report`) |
| `attempts` | `dict` | `{step_id: n}` for self-heal accounting |

Agents never call each other; they only read/write state. Errors are observations (caught by the
orchestrator), never crashes.

## 2. Gate vocabulary
* `verdict`: `pass` | `fail`. A gate with `on_fail_reset=()` **never self-heal-loops** (audits).
* `checks[]`: `{label, threshold, ok, advisory?, detail?}`. **`advisory: true` checks inform but never
  fail the gate** (used where regenerating code can't fix the finding — pod CPU, security risk).
* Severity words, everywhere: `critical` · `high` · `medium` · `low` · `info`.
* Rollout: **observe-first** — new gates start advisory; blocking thresholds are per-policy
  (`GATE_POLICY` in `src/config.py`).

## 3. Standard interchange formats (emit native + standard)
| domain | native | standard (CI/IDE/enterprise) |
|---|---|---|
| test runs | `run_results` | **JUnit XML** — `generated/reports/junit-<suite>-<run>.xml` |
| code coverage | `coverage_report` | **LCOV** (`coverage.lcov`) + **Cobertura XML** (`cobertura.xml`) |
| security | `security_report` | **SARIF 2.1.0** (`security.sarif`) + CWE / OWASP Top-10 / CVSS tags |
| SBOM | — | **CycloneDX** (Syft) |
| functional cases | `functional_cases` | **YAML intent DSL** (`functional-cases.yaml`) |
| performance | `PerfMetrics` | k6 summary JSON; thresholds = SLOs (p95, p99, error, throughput) |

## 4. Artifact `type` registry
`code` · `playwright` · `k6` · `web-vitals` · `unit` · `functional-cases` · `functional-cases-yaml` ·
`sast-plan` · `sast-report` · `sast-json` · `sast-sarif` · `a11y` · `contract` · `pact` ·
`coverage-report` · `coverage-json` · `coverage-lcov` · `coverage-cobertura` · `junit-<suite>`.
Prefix rule: the Results journey groups `<type>` and `<type>-*` under one stage — new types MUST
reuse an existing prefix or add a stage.

## 5. Identifiers
* step ids: `snake_case` verbs (`generate_ui_scripts`, `oracle_check`); tracks: single lowercase
  nouns (`functional`, `security`, `coverage`); gate names: UPPER (`QG1`, `SEC`, `COVERAGE`).
* Generated files live under `generated/<domain>/`; never write elsewhere.

## 6. Code standards
* Python: **ruff** (lint + format, `ruff.toml`, line length 120); type hints on public functions;
  docstring on every agent module stating what it grounds against.
* TypeScript/JS (e2e-runner, vscode-extension): `tsc --strict`; Playwright specs follow the
  accessibility-first locator house style (no raw CSS/XPath).
* Pre-commit: `.pre-commit-config.yaml` (ruff, ruff-format, whitespace/EOF). Install: `pip install
  pre-commit && pre-commit install`.
* Secrets only in git-ignored `.env`; project secrets Fernet-encrypted at rest; never logged.
