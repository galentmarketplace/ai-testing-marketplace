/**
 * Shared Playwright fixtures the agent-generated specs import instead of '@playwright/test'.
 *
 * Purpose: TRACE-FED HEALING. On failure we don't want just a one-line error — we want the real
 * state of the page at the moment it broke. This wraps the `page` fixture to record console errors,
 * page errors, and failed/4xx network calls throughout the test, and — when the test fails — attach
 * a compact "failure-context" (the accessibility snapshot + those logs) that the runner feeds back
 * to the Playwright agent so it repairs from evidence, not guesses.
 *
 * The pipeline writes this file next to the generated spec(s); specs do:
 *     import { test, expect } from './fixtures';
 */
import { test as base, expect } from '@playwright/test';

export const test = base.extend<{}>({
  page: async ({ page }, use, testInfo) => {
    const consoleLogs: string[] = [];
    const network: string[] = [];

    page.on('console', (m) => {
      const t = m.type();
      if (t === 'error' || t === 'warning') consoleLogs.push(`[console.${t}] ${m.text()}`.slice(0, 300));
    });
    page.on('pageerror', (e) => consoleLogs.push(`[pageerror] ${e.message}`.slice(0, 300)));
    page.on('requestfailed', (r) =>
      network.push(`FAILED ${r.method()} ${r.url()} — ${r.failure()?.errorText ?? ''}`.slice(0, 300)));
    page.on('response', (r) => {
      if (r.status() >= 400) network.push(`${r.status()} ${r.request().method()} ${r.url()}`.slice(0, 300));
    });

    await use(page);

    // Only pay the capture cost when the test did NOT end as expected (fail / timeout / unexpected pass).
    if (testInfo.status !== testInfo.expectedStatus) {
      let aria = '';
      try {
        // Accessibility snapshot of the live page — the semantic tree the agent should build locators from.
        aria = await page.locator('body').ariaSnapshot({ timeout: 2000 });
      } catch {
        try { aria = (await page.title()) + ' — ' + page.url(); } catch { /* page may be closed */ }
      }
      const ctx = {
        url: (() => { try { return page.url(); } catch { return ''; } })(),
        console: consoleLogs.slice(-15),
        network: network.slice(-12),
        aria: (aria || '').slice(0, 3500),
      };
      await testInfo.attach('failure-context', {
        body: JSON.stringify(ctx, null, 2),
        contentType: 'application/json',
      });
    }
  },
});

export { expect };
