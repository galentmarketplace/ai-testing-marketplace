# Leadership demo runbook

A live demo of the proof of concept. Everything here has been rehearsed; the timings and
figures are measured, not estimated.

**Total: about 15 minutes.**

## Reliability: a live run is now safe to show

Three root causes made earlier runs block, all fixed, all measured on the same ticket:

| Cause | Fix |
|---|---|
| Agents wrote specs importing page objects that cannot resolve, then navigated to a bare `/` | Specs are validated before execution and rejected with their faults fed back |
| Agents were told only one test account existed, so cases needing a locked account were skipped or made unpassable | Projects declare the accounts that exist; `locked_out_user` makes the lockout case a real test |
| Output-token ceilings truncated replies into invalid JSON mid-run | Every agent's budget raised, with a test enforcing a floor |

**Since those fixes: 6 runs, 6 passed.**

| | |
|---|---|
| Duration | 111 to 363 seconds |
| Attempts before passing | 1 to 4 |
| Cost | $0.17 to $0.57 |
| Skipped cases | 1 to 2 of 10-13, each with a stated reason |

So **run it live.** Budget up to 6 minutes and keep narrating; the gate loop is the
interesting part, not a hang. Still open the completed run in a second tab, because a
network blip or a SauceDemo hiccup is outside our control.

Do not weaken the gate threshold to make a demo pass. That is the one change that would
undermine everything the demo is about.

---

## Thirty minutes before

Run the pre-flight. If any line is not green, fix it before the room fills, not during.

```bash
# 1. the Mac must not sleep mid-demo
caffeinate -dimsu &

# 2. everything the demo touches
curl -s -o /dev/null -w 'dashboard      %{http_code}\n' http://127.0.0.1:8090/
curl -s -o /dev/null -w 'public URL     %{http_code}\n' https://deepness-t-shirt-unwelcome.ngrok-free.dev/
curl -s -o /dev/null -w 'SauceDemo      %{http_code}\n' https://www.saucedemo.com
curl -s -o /dev/null -w 'Jenkins        %{http_code}\n' http://localhost:8081/login
docker ps --filter name=marketplace-jenkins --format 'jenkins        {{.Status}}'
```

Then confirm Jira and the model are reachable, and that the test repository is green:

```bash
TOK=$(grep -E '^ATM_API_TOKEN=' .env | cut -d= -f2-)
curl -s -o /dev/null -w 'Jira SCRUM-5   %{http_code}\n' -H "Authorization: Bearer $TOK" \
  'http://127.0.0.1:8090/api/jira/issue?project_id=f07ecd4e1a8d42d8a68d354dfe2b39fa&key=SCRUM-5'
gh api repos/galentmarketplace/saucedemo-e2e/commits/main/check-runs -q '.check_runs[0].conclusion'
```

**Have a completed run open in a second browser tab.** If the live run misbehaves, you walk
through that one instead of debugging in front of leadership. This is the single most
important preparation step.

---

## The demo

### 1. The problem, 2 minutes — no screen

The failure mode of AI test generation is not a broken test. It is a test that passes,
looks green, and verifies nothing. A false pass is worse than no test, because it buys
confidence and scales silently.

Say plainly that this is what the platform is built to prevent, and that you will show the
mechanisms rather than assert them.

### 2. The source of truth, 1 minute

Open **SCRUM-5** in Jira. Show the Acceptance Criteria section. Say: this is the only input.
Not a prompt, not a description of the app. The ticket.

### 3. The live run, 2 to 6 minutes

Start it from the dashboard, or:

```bash
curl -s -X POST http://127.0.0.1:8090/api/run \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' -d '{
    "mock": false, "mode": "custom", "tracks": ["criteria","functional"],
    "project_id": "f07ecd4e1a8d42d8a68d354dfe2b39fa", "ticket": "SCRUM-5" }'
```

Narrate the execution view as it moves. The order is always the same:

| Stage | What to say |
|---|---|
| Repo Analysis | It clones and reads the application, so locators come from real code |
| Acceptance Criteria | Pulled live from Jira, not retyped |
| Functional Cases | Ten discrete, traceable cases derived from those criteria |
| Playwright Agent | Specs written against the live accessibility tree, not guessed CSS |
| Feature Runner | Actually executed against the running application |
| Oracle Check | An independent audit of the passing tests against the criteria |
| QG1 Gate | The verdict, with every criterion shown |

### 4. The honest gate, 2 minutes — the most important slide of the demo

Open the gate detail. It reads:

```
pass rate 100%  (8 passed, 0 failed, 2 skipped)
2 of 10 case(s) not executed (20%)        [advisory]
```

Make the point explicitly: **it tells you what it did not test.** The two skipped cases need
a pre-provisioned locked account, so the agent declined to fake them and said so. A tool that
reported "100%" and stopped there would be hiding a fifth of the work.

### 5. The evidence, 3 minutes

Open the generated spec. Point out: one import, absolute URLs, role-based locators, seventeen
real assertions, and no try/catch or soft assertions anywhere. Then open the JUnit report and
show that every case carries its real name, so a green report in CI is traceable to an
acceptance case.

Then open the **blocked pull request** in `saucedemo-e2e`. A deliberately failing test, a red
check, and a merge refused by GitHub with "the base branch policy prohibits the merge". This
is what makes a generated pull request safe rather than merely present.

### 6. Cost, 1 minute

**$0.17 for a clean run, up to $0.80 when the gate loops** — measured across nine runs. Quote
the range, not the best case; someone will ask what happens when it retries.

Measurement, execution and reporting are deterministic code, and the model is spent only where
judgement is genuinely required. Coverage, for instance, costs nothing at all. So cost tracks
how much *reasoning* a change needs, not how much testing you do.

### 7. Close, 2 minutes

Test automation normally decays: written once, drifts, starts failing for reasons nobody
investigates, and is eventually switched off. The expensive part was never writing the tests.
A pipeline that repairs a broken test but escalates a broken product, rather than editing the
failure away, attacks exactly that decay curve.

Then make the ask: **two pilot teams.** Everything else is work we can complete ourselves.

---

## If something breaks

| Symptom | Do this |
|---|---|
| Run blocks or a gate fails | **Do not debug.** Say "this is the self-correction loop, and it blocks rather than reporting a false pass" — that is a true and favourable statement. Switch to the completed run in your second tab |
| Dashboard unreachable | The tunnel or the platform stopped. Use `http://127.0.0.1:8090` locally and carry on |
| Jenkins down | `docker start marketplace-jenkins`, allow 60 seconds |
| Run takes longer than 3 minutes | Expected — measured 111 to 363 seconds. The gate loop is the interesting part; narrate it |
| Live run blocks | Say it accurately: "it refused to pass at 94% against a 95% policy, and blocked instead of reporting a false pass." Then continue with the completed run |

---

## Questions you will be asked

**"How do we know the tests are real?"** Three independent mechanisms: the oracle audits
passes against the criteria, the gate reports skipped cases separately, and the generated spec
is rejected before execution if it contains an assertion that cannot fail.

**"What if it writes a test for a bug?"** Triage runs before any repair. A product defect
leaves the test untouched and escalates; only a test defect is repaired. Rewriting a failing
test to pass would convert a caught bug into a silent regression.

**"What does it cost at scale?"** About $0.17 to $0.30 per run today. The deterministic paths,
including coverage, cost nothing.

**"Is it production ready?"** Honest answer: the engine is, the deployment is not. A formal
production-readiness review found three blockers and all three are closed, there are 206
automated tests green in CI, and the container is verified. What is missing is a host and two
pilot teams.

**"What does not work yet?"** Say it plainly, and do not be talked out of it. Coverage covers
Go, JavaScript and Python but not every language. It runs on a laptop rather than a server.
The full ticket-to-merge chain has been proven in pieces, not yet in one unbroken automated
pass. And **the run converges on about five attempts in seven** — when it does not, it blocks
for a human rather than passing, which is the right failure but still a gap. Reliability of
convergence is the main engineering work remaining.


---

# The other four tracks

Each verified live on 7 October. Pick a playbook, paste the inputs, untick **Mock mode**.

## Go code coverage — the fix, not just the number — ~35s with the PR

This is the strongest of the four, because it does not stop at reporting a gap. It finds the
uncovered functions, reads their real source, writes tests, **compiles and runs them**, and
re-measures. Only then does it raise a pull request.

Playbook **Go Code Coverage** with the **delivery** track on. Repository
`https://github.com/galentmarketplace/go-orders-service`, destination the same repository,
minimum coverage `80`.

Narrate it in four beats, which is the order the screen shows:

| Beat | What appears |
|---|---|
| The gap | `28.6% of statements (8/28), 4 uncovered function(s) · threshold 80%` — and the report names each one at its exact line: `Count`, `Remove`, `BulkDiscount`, `LoyaltyPoints` |
| The fix | 2 test files written against the real signatures, table-driven |
| The proof | `go test ./...` compiled and passed, then coverage was **re-measured** — `28.6% → 96.4%` |
| The delivery | A pull request whose title is `Raise test coverage 28.6% → 96.4%`, listing the functions now covered and stating the figure was measured, not projected |

**Measured: 5 runs out of 5 identical.** 19 to 22 seconds for the coverage track, about
35 with the pull request, and $0.033 to $0.041 a run.

> **Leave pull request #3 open.** It contains this same fix. Merging it raises the
> repository to 96.4%, and then a live run correctly finds nothing to do and the whole
> demonstration disappears. The gap in `go-orders-service` is the demo asset.

**The point to make:** every number on that pull request came from a measurement taken after
the tests ran, not from the model's description of its own work. Ask the room how they would
know otherwise — that is the whole argument. Open `coverage-report.md` and show the
before/after table next to the diff.

Two secondary points if there is appetite:

* the report also tells you what it did **not** fix — functions it declined to test, with the
  reason, rather than quietly leaving them out
* on a larger repository (`gorilla/mux`: 90.7%) it also reports that coverage instruments only
  80% of the source files, so the effective figure is 72.6%. A tool reporting 90.7% alone is
  flattering you

## Unit tests — ~6s

Playbook **Unit Tests**. Repository `https://github.com/galentmarketplace/sample-app-web`.

Result: the repository's own jest suite, 146 passed, 0 failed, UNIT gate pass.

**The point to make:** these are the team's existing tests, run as they are. Nothing was
generated and nothing was replaced.

## Security audit — ~45s

Playbook **Security Audit**. Repository `https://github.com/galentmarketplace/sample-app-web`.

Result: 95 findings (67 high, 27 medium, 1 low) across six methodologies — static analysis,
dependencies and CVEs, secrets, infrastructure misconfiguration, container image, and a
software bill of materials. SARIF 2.1.0 emitted for GitHub code scanning.

**The point to make:** one pass, six disciplines, and it degrades honestly — a methodology
whose scanner is absent is reported as not run rather than as clean.

## Performance — ~120s

Playbook **Performance**, with the deploy track enabled. Repository
`https://github.com/galentmarketplace/sample-app-web`, deploy `yes`, type `load`.

Result: the platform builds and boots the application itself, then load-tests the build it
just deployed. 4274 checks passed, p95 2ms, error rate 0.0%, QG1 pass.

**The point to make:** it tests what it deployed, not a URL someone typed last month. And it
load-tests the application's real routes, discovered from the code.

**Do not** point a load test at saucedemo.com or any third-party site. That is someone else's
infrastructure. The deploy track exists so you are always testing your own.

---

## If you run all five in one sitting

Functional, then these four, is roughly 10 minutes of runtime and under two dollars. Start
each from Playbooks and let the previous results stay open in other tabs, so you can compare
a coverage gate against a security gate against a performance gate and make the real point:
**every track reports the same way, and every one tells you what it did not verify.**
