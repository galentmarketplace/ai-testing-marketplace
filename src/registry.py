"""The Agent Registry — the marketplace's single source of truth.

Every capability in the platform is ONE declarative `AgentSpec`. The orchestrator
(dependency-driven scheduling), the Prescreen (available tracks), the web server
(node metadata), and the UI (graph columns/routes) are all DERIVED from this list.

Adding a new testing agent = append one `AgentSpec` here (or drop a module that
registers one). No edits to the orchestrator, gates, server, or frontend.

--- Scheduling model -------------------------------------------------------------
The orchestrator runs a generic loop over the active plan:
  * a step is *runnable* when its track is active and every in-plan dependency is done;
  * gates evaluate a suite and, on failure, reset their `on_fail_reset` steps to
    pending (the fixer + the chain + the gate itself) and burn one retry — so the
    responsible agent re-runs, the suite re-executes, and the gate re-evaluates;
  * when nothing is left runnable, the goal is reached.

--- Display model ----------------------------------------------------------------
`display` lists the UI node(s) a step maps to (usually one; the feature runner
fans out to two). `column` is a top-level agent lane; `parent` nests a runner/gate
under its agent's lane. The server ships this to the browser as a manifest.
"""
from collections.abc import Callable
from dataclasses import dataclass, field

from .agents.ac_agent import generate_ac
from .agents.dev_agent import generate_code
from .agents.functional_case_agent import generate_functional_cases
from .agents.go_coverage_agent import coverage_gate, go_coverage
from .agents.go_test_agent import generate_go_tests
from .agents.jira_report_agent import jira_report
from .agents.oracle_agent import oracle_check
from .agents.perf_agent import generate_perf_scripts
from .agents.pr_agent import open_pr, push_branch
from .agents.prescreen_agent import prescreen
from .agents.regression_agent import select_regression
from .agents.security_agent import generate_security_scan, run_security_scan, security_gate
from .agents.ui_automation_agent import generate_ui_scripts
from .agents.unit_agent import run_unit_tests, unit_gate  # real repo unit-tester (+ advisory gate)
from .config import MAX_RETRIES
from .gates.quality_gates import quality_gate_1, quality_gate_2
from .runner.executor import execute_regression, execute_scripts


# ---------- Display node (one graph node) ----------
@dataclass
class DisplayNode:
    id: str                       # display id used by the frontend graph
    label: str
    icon: str
    color: str
    cap: str                      # capability tag shown on the card
    kind: str = "agent"           # agent | runner | gate
    column: str | None = None  # a top-level lane (agents). None if this is a child
    parent: str | None = None  # the column id this child nests under (runners/gates)


# ---------- Agent spec (one capability) ----------
@dataclass
class AgentSpec:
    id: str                                   # backend node id (what the orchestrator dispatches)
    label: str
    kind: str                                 # agent | runner | gate
    fn: Callable                              # state -> partial state update
    tracks: tuple = ()                        # capability tracks; empty => always-on (prescreen)
    depends_on: tuple = ()                    # step ids that must be done first
    produces: str = ""                        # short human note for reasoning
    needs_build: bool = False                 # requires run_config.entry == "from_story"
    ship: bool = False                        # commits the branch — waits on every NON-post_pr gate (pre-CI)
    final: bool = False                       # opens the PR at the very end — waits on EVERY gate incl. post_pr (CI)
    post_pr: bool = False                     # a gate that runs AFTER the branch push (CI verification); ship does NOT wait on it
    superseded_by_jenkins: bool = False       # a LOCAL runner/gate that Jenkins replaces — skipped when the 'jenkins' track is active
    trigger_only: bool = False                # not in the forward plan; only fired via a gate loop-back
    gate_name: str = ""                       # for gates: the name it writes (UNIT|QG1|QG2)
    on_fail_reset: tuple = ()                 # for gates: steps to re-run on failure
    max_retries: int = MAX_RETRIES
    category: str = "Testing"                 # marketplace grouping (Requirements/Build/Testing/Security/…)
    catalog: bool | None = None               # show in the Agents marketplace? None => auto (top-level lane nodes only)
    display: list = field(default_factory=list)   # UI node(s)

    def is_active(self, cfg: dict) -> bool:
        """Would this step run under the given resolved RunConfig?"""
        if not self.tracks and not self.ship and not self.final:
            return True                                   # always-on (prescreen)
        # Jenkins is the executor when its track is active — skip the redundant LOCAL run+gate.
        if self.superseded_by_jenkins and "jenkins" in set(cfg.get("tracks", [])):
            return False
        if self.ship or self.final:
            # active when the PR is explicitly requested (open_pr) OR the PR agent was
            # dropped onto the canvas as an individual agent (its 'delivery' track).
            return bool(cfg.get("open_pr", True)) or ("delivery" in set(cfg.get("tracks", [])))
        if self.needs_build and cfg.get("entry", "from_story") != "from_story":
            return False
        active = set(cfg.get("tracks", []))
        return any(t in active for t in self.tracks)


# ---------- UI palette (kept out of the specs for readability) ----------
_C = {"plan": "#c58bff", "tool": "#58a6ff", "exec": "#39c5cf", "eval": "#d29922",
      "resp": "#3fb950", "perf": "#3fb0c9", "heal": "#f778ba", "intake": "#7c8db5",
      "sec": "#e3b341"}


# ---------- THE REGISTRY ----------
# Order matters: it is the canonical left-to-right lane order and the base topo order.
REGISTRY: list[AgentSpec] = [
    AgentSpec(
        id="prescreen", label="Prescreen", kind="gate", fn=prescreen, category="Intake",
        produces="decide which tracks this run activates",
        display=[DisplayNode("prescreen", "Prescreen", "🚦", _C["intake"], "INTAKE", "gate", column="prescreen")],
    ),
    AgentSpec(
        id="generate_ac", label="Acceptance Criteria", kind="agent", fn=generate_ac,
        tracks=("criteria",), category="Requirements",
        produces="parse acceptance criteria (from a Jira link or pasted text; else infer from the repo)",
        display=[DisplayNode("ac", "Acceptance Criteria", "🗺️", _C["plan"], "CRITERIA", "agent", column="ac")],
    ),
    AgentSpec(
        id="generate_code", label="Dev Agent", kind="agent", fn=generate_code,
        tracks=("build",), depends_on=("generate_ac",), category="Build",
        produces="implement code for the criteria (build a feature)",
        display=[DisplayNode("dev", "Dev Agent", "🛠️", _C["tool"], "BUILD", "agent", column="dev")],
    ),
    AgentSpec(
        id="run_unit_tests", label="Unit Tests", kind="runner", fn=run_unit_tests,
        tracks=("unit",), depends_on=("generate_code",), produces="run the unit suite",
        display=[DisplayNode("unit_run", "Unit Tests", "🧪", _C["exec"], "UNIT", "runner", column="unit_run")],
    ),
    AgentSpec(
        id="unit_gate", label="UNIT Gate", kind="gate", fn=unit_gate,
        tracks=("unit",), depends_on=("run_unit_tests",), gate_name="UNIT",
        on_fail_reset=("generate_code", "run_unit_tests", "unit_gate"),
        produces="evaluate unit results against policy",
        display=[DisplayNode("unit_gate", "UNIT Gate", "✅", _C["eval"], "EVALUATION", "gate", parent="unit_run")],
    ),
    AgentSpec(
        id="go_coverage", label="Go Coverage", kind="runner", fn=go_coverage,
        tracks=("coverage",), category="Quality",
        produces="measure real Go code coverage (go test -coverprofile) → LCOV + Cobertura + uncovered functions",
        display=[DisplayNode("coverage", "Go Coverage", "📈", _C["exec"], "COVERAGE", "runner", column="coverage")],
    ),
    AgentSpec(
        id="generate_go_tests", label="Go Test Gen", kind="agent", fn=generate_go_tests,
        tracks=("coverage",), depends_on=("go_coverage",), category="Quality", catalog=True,
        produces="propose unit tests for uncovered Go functions, grounded in their source — execution-verified when go is available",
        display=[DisplayNode("go_tests", "Go Test Gen", "🧬", _C["tool"], "TOOL USE", "agent", parent="coverage")],
    ),
    AgentSpec(
        id="coverage_gate", label="COVERAGE Gate", kind="gate", fn=coverage_gate,
        tracks=("coverage",), depends_on=("go_coverage", "generate_go_tests"), gate_name="COVERAGE",
        on_fail_reset=(),   # observe-first audit — no Go test generator yet, so never self-heal-loop
        produces="gate on statement coverage vs policy threshold (advisory by default)",
        display=[DisplayNode("coverage_gate", "COVERAGE Gate", "✅", _C["eval"], "EVALUATION", "gate", parent="coverage")],
    ),
    AgentSpec(
        id="generate_functional_cases", label="Functional Cases", kind="agent", fn=generate_functional_cases,
        tracks=("functional",), depends_on=("generate_ac",), category="Requirements",
        produces="derive functional test cases from the acceptance criteria (source of truth)",
        display=[DisplayNode("fcases", "Functional Cases", "📋", _C["plan"], "TEST DESIGN", "agent", column="fcases")],
    ),
    AgentSpec(
        id="generate_ui_scripts", label="Playwright Agent", kind="agent", fn=generate_ui_scripts,
        tracks=("functional",), depends_on=("generate_functional_cases",),
        produces="automate each functional case as a Playwright spec (grounded in the real DOM)",
        display=[DisplayNode("ui", "Playwright Agent", "🎭", _C["tool"], "TOOL USE", "agent", column="ui")],
    ),
    AgentSpec(
        id="generate_perf_scripts", label="Performance", kind="agent", fn=generate_perf_scripts,
        tracks=("perf",), depends_on=("generate_code", "generate_ac"), category="Performance", produces="k6 load scripts",
        display=[DisplayNode("perf", "Performance", "⚡", _C["perf"], "LOAD/PERF", "agent", column="perf")],
    ),
    AgentSpec(
        id="execute_scripts", label="Feature Runner", kind="runner", fn=execute_scripts,
        tracks=("functional", "perf"), depends_on=("generate_ui_scripts", "generate_perf_scripts"),
        produces="run the feature suite LOCALLY (fast self-heal pre-flight before CI)",
        display=[DisplayNode("feat_run", "Feature Runner", "⚙️", _C["exec"], "EXECUTION", "runner", parent="ui"),
                 DisplayNode("perf_run", "Perf Runner (k6)", "📊", _C["exec"], "EXECUTION", "runner", parent="perf")],
    ),
    AgentSpec(
        id="oracle_check", label="Oracle Check", kind="agent", fn=oracle_check,
        tracks=("functional",), depends_on=("execute_scripts",), category="Quality", catalog=True,
        produces="audit passing tests against the AC — catch false passes before they reach the gate",
        display=[DisplayNode("oracle", "Oracle Check", "🔎", _C["eval"], "EVALUATION", "agent", parent="ui")],
    ),
    AgentSpec(
        id="quality_gate_1", label="QG1 Gate", kind="gate", fn=quality_gate_1,
        tracks=("functional", "perf"), depends_on=("execute_scripts", "oracle_check"), gate_name="QG1",
        on_fail_reset=("generate_ui_scripts", "generate_perf_scripts", "execute_scripts", "oracle_check", "quality_gate_1"),
        produces="evaluate the LOCAL feature run + oracle audit — self-heal here (fast) before pushing to CI",
        display=[DisplayNode("qg1", "QG1 Gate", "✅", _C["eval"], "EVALUATION", "gate", parent="ui")],
    ),
    AgentSpec(
        id="select_regression", label="Regression", kind="agent", fn=select_regression,
        tracks=("regression",), produces="select impacted regression tags",
        display=[DisplayNode("reg", "Regression", "🧭", _C["plan"], "REASONING", "agent", column="reg")],
    ),
    AgentSpec(
        id="execute_regression", label="Regr Runner", kind="runner", fn=execute_regression,
        tracks=("regression",), depends_on=("select_regression", "self_heal"),
        superseded_by_jenkins=True,   # when Jenkins runs the suite, skip the local regression run
        produces="run the targeted regression suite",
        display=[DisplayNode("reg_run", "Regr Runner", "⚙️", _C["exec"], "EXECUTION", "runner", parent="reg")],
    ),
    AgentSpec(
        id="quality_gate_2", label="QG2 Gate", kind="gate", fn=quality_gate_2,
        tracks=("regression",), depends_on=("execute_regression",), gate_name="QG2",
        on_fail_reset=("self_heal", "execute_regression", "quality_gate_2"),
        superseded_by_jenkins=True,   # Jenkins is the gate when its track is active
        produces="evaluate regression results",
        display=[DisplayNode("qg2", "QG2 Gate", "✅", _C["eval"], "EVALUATION", "gate", parent="reg")],
    ),
    # ---- Drop-in capability: Security (SAST) — added purely by registering it here. ----
    AgentSpec(
        id="generate_security_scan", label="Security", kind="agent", fn=generate_security_scan,
        tracks=("security",), depends_on=("generate_code", "generate_ac"), category="Security",
        produces="author a SAST ruleset for the change",
        display=[DisplayNode("sec", "Security", "🛡️", _C["sec"], "SECURITY", "agent", column="sec")],
    ),
    AgentSpec(
        id="run_security_scan", label="Security Scan", kind="runner", fn=run_security_scan,
        tracks=("security",), depends_on=("generate_security_scan",),
        produces="run the SAST scan",
        display=[DisplayNode("sec_run", "SAST Runner", "⚙️", _C["exec"], "EXECUTION", "runner", parent="sec")],
    ),
    AgentSpec(
        id="security_gate", label="SEC Gate", kind="gate", fn=security_gate,
        tracks=("security",), depends_on=("run_security_scan",), gate_name="SEC",
        on_fail_reset=(),   # security is an audit — report findings, never self-heal-loop
        produces="report security findings across the repo",
        display=[DisplayNode("sec_gate", "SEC Gate", "✅", _C["eval"], "EVALUATION", "gate", parent="sec")],
    ),
    # Delivery is PR-LAST: push a branch first (so Jenkins can run against it), then open the
    # PR only after every gate — including the Jenkins CI gate — is green.
    AgentSpec(
        id="push_branch", label="Publish Branch", kind="agent", fn=push_branch, ship=True,
        category="Delivery", tracks=("delivery",),
        depends_on=("unit_gate", "quality_gate_1", "quality_gate_2"),
        produces="commit the generated code onto a branch for CI (no PR yet)",
        display=[DisplayNode("pr", "Publish Branch", "📦", _C["tool"], "DELIVERY", "agent", column="pr")],
    ),
    AgentSpec(
        id="open_pr", label="PR Agent", kind="agent", fn=open_pr, final=True,
        category="Delivery", tracks=("delivery",), depends_on=("push_branch",),
        produces="open the pull request once CI is green (the finish line)",
        display=[DisplayNode("pr_open", "Open PR", "📤", _C["resp"], "RESPONSE", "agent", parent="pr")],
    ),
    # Closes the loop: posts the run RESULTS back onto the Jira ticket (after the PR). No-op when the
    # run wasn't started from a Jira ticket.
    AgentSpec(
        id="jira_report", label="Jira Update", kind="agent", fn=jira_report, category="Delivery",
        tracks=("delivery",), depends_on=("open_pr",),
        produces="post the run results back to the Jira ticket (ticket → production loop)",
        display=[DisplayNode("jira_report", "Jira Update", "📨", _C["resp"], "RESPONSE", "agent", column="jira_report")],
    ),
    # NOTE: the single unified Self-Heal now lives in agents/plugins/jenkins_agent.py — it serves
    # BOTH the local regression gate (QG2) and the Jenkins gate, so there is no separate healer here.
]


# ---- Auto-discovery: any module in agents/plugins/ that exposes SPECS joins the roster ----
# This is what makes the marketplace scale to N agents: a new capability is a NEW FILE,
# with zero edits to this registry, the orchestrator, the server, or the UI.
def _discover_plugins():
    import importlib
    import pkgutil
    try:
        pkg = importlib.import_module(".agents.plugins", package=__package__)
    except ModuleNotFoundError:
        return
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        mod = importlib.import_module(f".agents.plugins.{info.name}", package=__package__)
        for spec in getattr(mod, "SPECS", []):
            REGISTRY.append(spec)


_discover_plugins()

BY_ID: dict[str, AgentSpec] = {s.id: s for s in REGISTRY}

# All capability tracks the platform currently offers (derived, not hand-maintained).
ALL_TRACKS: list[str] = sorted({t for s in REGISTRY for t in s.tracks})


def plan_for(cfg: dict) -> list[AgentSpec]:
    """The ordered list of steps active for a resolved RunConfig (prescreen excluded —
    it runs before the plan exists). Dependencies referencing inactive steps are simply
    ignored by the scheduler, so a scoped run needs no special-casing."""
    return [s for s in REGISTRY if s.id != "prescreen" and s.is_active(cfg)]


def build_manifest() -> dict:
    """UI manifest derived from the registry: lane columns (with nested children),
    the backend-node → display-id routes, and each column's tracks (so the frontend
    can decide which lanes a scope activates — exactly like the backend)."""
    columns, col_index = [], {}
    for spec in REGISTRY:
        for d in spec.display:
            if d.column:                                   # a top-level agent lane
                col = {"id": d.column, "label": d.label, "icon": d.icon, "color": d.color,
                       "cap": d.cap, "children": [],
                       "tracks": list(spec.tracks), "needs_build": spec.needs_build,
                       "ship": spec.ship, "always": not spec.tracks and not spec.ship}
                col_index[d.column] = col
                columns.append(col)
    for spec in REGISTRY:
        for d in spec.display:
            if d.parent and d.parent in col_index:         # nest runners/gates under their lane
                col_index[d.parent]["children"].append(
                    {"id": d.id, "label": d.label, "icon": d.icon, "color": d.color,
                     "cap": d.cap, "kind": d.kind})
    route = {spec.id: [d.id for d in spec.display] for spec in REGISTRY}
    node_meta = {spec.id: {"label": spec.label, "kind": spec.kind,
                           "emits": spec.produces} for spec in REGISTRY}

    # Track catalog for the Custom flow builder: one draggable capability per track,
    # with a representative icon/colour and the agents it pulls in.
    _pretty = {"perf": "Performance", "criteria": "Acceptance Criteria", "build": "Dev / Build",
               "delivery": "PR Agent", "jenkins": "Jenkins CI", "functional": "Playwright Agent",
               "coverage": "Code Coverage"}
    tracks_cat = []
    for t in ALL_TRACKS:
        reps = [s for s in REGISTRY if s.tracks == (t,)]
        rep = next((s for s in reps if s.kind == "agent"), reps[0] if reps else
                   next(s for s in REGISTRY if t in s.tracks))
        d = rep.display[0]
        tracks_cat.append({
            "track": t, "label": _pretty.get(t, t.title()), "icon": d.icon, "color": d.color,
            "agents": [s.label for s in REGISTRY if t in s.tracks],
        })

    # Marketplace catalog: one browsable entry per agent capability. Auto rule = top-level lane nodes;
    # a spec can opt in/out explicitly via `catalog` (e.g. a verifier that lives inside another lane).
    agents_cat = []
    for spec in REGISTRY:
        d = spec.display[0] if spec.display else None
        if not d:
            continue
        show = spec.catalog if spec.catalog is not None else bool(d.column)
        if show:
            agents_cat.append({"id": spec.id, "label": d.label, "icon": d.icon, "color": d.color,
                               "kind": spec.kind, "produces": spec.produces, "category": spec.category,
                               "tracks": list(spec.tracks)})

    return {"columns": columns, "route": route, "node_meta": node_meta, "tracks": tracks_cat,
            "agents": agents_cat,
            "retry_from": {s.gate_name: list(s.on_fail_reset) for s in REGISTRY if s.gate_name}}
