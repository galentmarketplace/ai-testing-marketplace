"""The generated spec must be able to run at all before it costs an execution cycle.

Found the hard way during a demo rehearsal: a spec imported `../../pages/LoginPage`, which
does not exist beside the spec, and called `loginPage.goto()`, which navigates to a bare "/"
with no configured baseURL. Playwright failed all nine tests with "Cannot navigate to invalid
URL", the gate looped, and the run blocked after four attempts and $0.74. Each of those
attempts cost a full generate, execute and gate cycle to learn something a regex knows.
"""
import pytest

from src.agents.ui_automation_agent import lint_spec

GOOD = """import { test, expect } from './fixtures';

const BASE_URL = process.env.BASE_URL || 'https://www.saucedemo.com';

test.describe('login', () => {
  test('FC-1 valid login', async ({ page }) => {
    await page.goto(`${BASE_URL}/`);
    await expect(page.getByRole('button', { name: 'Login' })).toBeVisible();
  });
});
"""


def test_a_correct_spec_passes_the_lint():
    assert lint_spec(GOOD) == []


def test_a_page_object_import_is_rejected():
    bad = GOOD.replace("import { test, expect } from './fixtures';",
                       "import { test, expect } from './fixtures';\n"
                       "import { LoginPage } from '../../pages/LoginPage';")
    problems = lint_spec(bad)
    assert any("pages/LoginPage" in p for p in problems)


def test_importing_playwright_directly_is_rejected():
    """The fixtures capture the failure context the healer needs; bypassing them loses it."""
    bad = GOOD.replace("from './fixtures'", "from '@playwright/test'")
    assert any("@playwright/test" in p for p in lint_spec(bad))


@pytest.mark.parametrize("nav", [
    "await page.goto('/');",
    'await page.goto("/");',
    "await page.goto(`/`);",
    "await loginPage.goto();",
])
def test_relative_or_empty_navigation_is_rejected(nav):
    bad = GOOD.replace("await page.goto(`${BASE_URL}/`);", nav)
    problems = lint_spec(bad)
    assert any("absolute" in p for p in problems), f"{nav!r} was accepted"


def test_absolute_navigation_is_accepted():
    for nav in ("await page.goto(`${BASE_URL}/`);",
                "await page.goto(`${BASE_URL}/inventory.html`);",
                "await page.goto(BASE_URL + '/cart.html');"):
        spec = GOOD.replace("await page.goto(`${BASE_URL}/`);", nav)
        assert not any("absolute" in p for p in lint_spec(spec)), f"{nav!r} was rejected"


def test_a_spec_that_ignores_base_url_is_rejected():
    bad = GOOD.replace("const BASE_URL = process.env.BASE_URL || 'https://www.saucedemo.com';",
                       "const BASE_URL = 'https://www.saucedemo.com';")
    assert any("BASE_URL" in p for p in lint_spec(bad))


def test_every_problem_is_reported_not_just_the_first():
    bad = ("import { test, expect } from '@playwright/test';\n"
           "import { LoginPage } from '../pages/LoginPage';\n"
           "test('t', async ({ page }) => { await page.goto('/'); });\n")
    assert len(lint_spec(bad)) >= 3, "the model needs every fault, or it fixes one per cycle"


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


# ---- an assertion that cannot fail is a false pass wearing a test's clothes ----
@pytest.mark.parametrize("dead", [
    "await expect(page.getByRole('main')).toBeVisible().catch(() => {});",
    "await expect.soft(page.getByText('x')).toBeVisible();",
    "await expect(page.getByText('x')).toBeVisible().then(() => {});",
])
def test_a_swallowed_assertion_is_rejected(dead):
    """Seen live: `.catch(() => {})` on a real assertion made it unable to fail."""
    spec = GOOD.replace("await expect(page.getByRole('button', { name: 'Login' })).toBeVisible();", dead)
    assert any("cannot fail" in p for p in lint_spec(spec)), f"{dead!r} was accepted"


def test_an_ordinary_assertion_is_not_flagged():
    assert not any("cannot fail" in p for p in lint_spec(GOOD))


def test_a_catch_unrelated_to_an_assertion_is_not_flagged():
    """Only assertions matter; a guarded non-assertion call is legitimate."""
    spec = GOOD.replace("await page.goto(`${BASE_URL}/`);",
                        "await page.goto(`${BASE_URL}/`);\n    await page.unroute('**').catch(() => {});")
    assert not any("cannot fail" in p for p in lint_spec(spec))


def test_the_mock_spec_satisfies_the_lint():
    """The mock fixture must model what the real agent should produce.

    It did not: it imported '@playwright/test' and navigated relatively, so adding the lint
    broke the mock full-track CI check. A mock that cannot pass its own rules teaches the
    wrong shape and hides prompt regressions instead of exercising them.
    """
    import json

    from src.mocks import MOCK_RESPONSES
    files = json.loads(MOCK_RESPONSES["ui_automation_agent"])["files"]
    assert files
    for f in files:
        assert lint_spec(f["content"]) == [], f"the mock spec violates the lint: {f['path']}"


# ---- test accounts: the upstream reason cases were skipped ----
def test_extra_accounts_are_parsed_from_a_compact_string():
    from src.agents.ui_automation_agent import _test_accounts
    got = _test_accounts({"login_user": "standard_user",
                          "test_accounts": "locked_out_user:a pre-locked account, problem_user:UI defects"})
    assert got == [{"username": "locked_out_user", "purpose": "a pre-locked account"},
                   {"username": "problem_user", "purpose": "UI defects"}]


def test_the_primary_account_is_not_listed_twice():
    from src.agents.ui_automation_agent import _test_accounts
    got = _test_accounts({"login_user": "standard_user",
                          "test_accounts": "standard_user:primary, locked_out_user:locked"})
    assert [a["username"] for a in got] == ["locked_out_user"]


def test_a_list_of_dicts_is_accepted_too():
    from src.agents.ui_automation_agent import _test_accounts
    got = _test_accounts({"test_accounts": [{"username": "admin", "role": "administrator"},
                                            {"username": "ro", "purpose": "read only"},
                                            {"nope": "no username"}]})
    assert got == [{"username": "admin", "purpose": "administrator"},
                   {"username": "ro", "purpose": "read only"}]


def test_no_accounts_configured_is_not_an_error():
    from src.agents.ui_automation_agent import _test_accounts
    assert _test_accounts({}) == []
    assert _test_accounts({"test_accounts": ""}) == []


def test_both_agents_share_one_parser():
    """If the two agents disagreed about which accounts exist, the case agent would mark a
    case un-automatable while the spec agent had the account to drive it."""
    from src.agents.functional_case_agent import _accounts
    from src.agents.ui_automation_agent import _test_accounts
    raw = "locked_out_user:locked"
    assert _accounts(raw, "standard_user") == _test_accounts(
        {"test_accounts": raw, "login_user": "standard_user"})
