# Running a pilot

A pilot answers one question: **does this reduce the cost of keeping a real suite green?**
Everything below is shaped around measuring that honestly, including the ways it could fail.

Two teams is the target. One team cannot distinguish "the platform works" from "that
codebase suited it".

---

## What a pilot team provides

Five things. If any is missing the pilot will stall, so it is worth confirming all five
before the kickoff rather than discovering a gap in week two.

| # | Item | Why it is needed | Good / bad fit |
|---|------|------------------|----------------|
| 1 | **An application repository** | The Dev Agent reads it for context and, on build runs, edits it | Any Go, JavaScript/TypeScript or Python repo. A repo with no existing tests is fine |
| 2 | **A test environment with a URL** | Automation drives the real app; a build-and-deploy run boots its own, but a stable environment is needed for test-only runs | Must be reachable from wherever the platform runs. A local-only environment will not work |
| 3 | **A test account** | Login-gated flows cannot be automated without one | Non-production data only. Never a real customer account |
| 4 | **A Jira project** | Acceptance criteria are the source of truth for both test generation and the oracle | Tickets must carry an **Acceptance Criteria** section. Vague tickets produce vague tests |
| 5 | **A named champion** | Someone to judge whether a generated test is actually good | ~2 hours a week. Without this you get output nobody evaluates |

### The one that is usually underestimated

Item 4. The platform is only as good as the acceptance criteria, because the oracle checks
generated tests *against them*. A ticket reading "fix the cart bug" gives the oracle nothing
to check against, and the run will correctly refuse to claim success. Ask the champion for
three or four representative tickets up front and read them before agreeing a start date.

---

## What we provide

- Platform access, a configured project, and the VS Code extension
- The pipeline wired to their repository, environment and Jira project
- A working baseline suite in their test repository before the pilot starts
- Continuous integration on that repository with branch protection, so a generated pull
  request cannot merge without a green check

---

## How it runs

**Week 0 — setup.** We configure the project and prove one end-to-end run on a ticket the
champion picks. Nothing is judged yet; this only confirms the plumbing.

**Weeks 1 to 3 — real tickets.** The team works normally. Tickets reaching the trigger
status start a run automatically. The champion reviews each generated pull request as they
would a colleague's.

**Week 4 — assessment.** Against the baseline below.

---

## What is measured

Agreed before the pilot starts, so the result cannot be argued into or out of success
afterwards.

| Measure | How | Why it matters |
|---|---|---|
| **Tests accepted without edit** | Champion marks each PR: merged as-is, merged with edits, rejected | The honest measure of generation quality |
| **Time to first passing test** | Ticket reaching trigger status, to a green PR | What the team actually saves |
| **Escaped defects** | Bugs found after a green run that a test should have caught | Whether the suite is real protection |
| **False passes** | Green tests that verify nothing, found on review | The failure mode the platform exists to prevent. Target is zero |
| **Maintenance avoided** | Self-heal repairs that the champion agrees were correct | Where the compounding value is |
| **Cost per run** | Reported per run by the platform | Whether it scales economically |

Record a **baseline first**: how long does writing an equivalent test take the team today,
and how much time goes into fixing broken tests each week? Without that number, any result
is unfalsifiable.

---

## What would make this fail, honestly

Worth saying to the team at kickoff, so nobody is surprised.

- **Thin acceptance criteria.** The largest single risk. See above.
- **An unstable test environment.** The triage step classifies environment failures
  separately rather than healing them, so you will see them as escalations rather than as
  bad tests, but they still cost the pilot time.
- **A codebase with no test setup at all.** Coverage needs the repository's own runner. We
  can add one, but that is a project, not a pilot.
- **A champion who cannot spare the time.** Generated tests nobody evaluates teach us
  nothing, and the pilot produces output instead of evidence.

---

## What the team should expect not to get

- Application code they can ship unreviewed. Every change arrives as a pull request and
  needs a human approval.
- Coverage of anything the acceptance criteria do not describe.
- A green suite on day one. The first week is usually about the environment, not the agents.
