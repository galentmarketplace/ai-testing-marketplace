// The UI agent's "eyes": launch the real app, LOG IN, and analyze the actual DOM of every page
// the flow touches — INCLUDING interactive states. For each page it also clicks the primary
// action triggers (Add / New / Create) and snapshots the modal/drawer that opens, so form fields
// are grounded in real locators instead of guessed.
//
// Usage:  node explore.cjs <baseUrl> [route1,route2,...]
// Env:    LOGIN_EMAIL, LOGIN_PASSWORD, LOGIN_PATH (default /login)
// Output: JSON array of { url, label, title, elements } — one per explored state.
const { chromium } = require('@playwright/test');

// Serializable element extractor — runs in the page via Playwright's evaluate (CSP-safe, no
// in-page eval). Given an optional scope selector, captures the attributes a Playwright locator
// keys on: role, label/aria, placeholder, id, name, data-testid, visible text.
const EXTRACT = (sel) => {
  const SEL = 'input, button, select, textarea, a[href], label, [role], [data-testid], h1, h2, h3';
  const root = (sel && document.querySelector(sel)) || document;
  return Array.from(root.querySelectorAll(SEL)).slice(0, 90).map(n => ({
    tag: n.tagName.toLowerCase(),
    type: n.getAttribute('type') || undefined,
    role: n.getAttribute('role') || undefined,
    name: n.getAttribute('name') || undefined,
    id: n.id || undefined,
    testid: n.getAttribute('data-testid') || undefined,
    placeholder: n.getAttribute('placeholder') || undefined,
    ariaLabel: n.getAttribute('aria-label') || n.getAttribute('title') || undefined,
    text: (n.innerText || n.value || '').trim().slice(0, 50) || undefined,
  })).filter(e => e.type !== 'hidden' && (e.text || e.placeholder || e.ariaLabel || e.id || e.role || e.testid));
};

const DIALOG = '.ant-modal, .ant-drawer, [role="dialog"]';
const TRIGGER_RE = /\b(add|new|create|register)\b|^\s*\+/i;

async function snap(page, url, label, sel) {
  const elements = await page.evaluate(EXTRACT, sel || null);
  // A11Y-TREE GROUNDING: the accessibility snapshot (roles + accessible names) is the SOTA representation
  // for agents — compact and semantic, it maps 1:1 onto getByRole(name) locators. Scoped to the dialog when open.
  let aria = '';
  try { aria = await page.locator(sel || 'body').first().ariaSnapshot(); } catch (e) { /* best-effort */ }
  return { url, label, title: await page.title(), elements, aria: (aria || '').slice(0, 2500) };
}

async function dialogSnap(page, url, label) {
  const has = await page.$(DIALOG);                 // scope to the opened dialog so we get its fields
  return snap(page, url, label, has ? DIALOG : null);
}

(async () => {
  const base = (process.argv[2] || 'http://localhost:3000').replace(/\/$/, '');
  const routes = (process.argv[3] || '/').split(',').map(r => r.trim()).filter(Boolean);
  const loginPath = process.env.LOGIN_PATH || '/login';
  const email = process.env.LOGIN_EMAIL, password = process.env.LOGIN_PASSWORD;

  const browser = await chromium.launch();
  const page = await browser.newPage();
  const out = [];
  const go = async (u) => { await page.goto(u, { waitUntil: 'networkidle', timeout: 30000 }); await page.waitForTimeout(1000); };

  // Snapshot the current page, then probe its primary action triggers (Add/New/Create) and
  // snapshot the modal/drawer each opens — so form fields are grounded, not guessed.
  const exploreCurrent = async (label) => {
    out.push(await snap(page, page.url(), label));
    const triggers = await page.$$eval('button', (btns, reStr) => {
      const re = new RegExp(reStr, 'i');
      return btns.map(b => (b.innerText || b.getAttribute('aria-label') || '').trim()).filter(t => t && re.test(t));
    }, TRIGGER_RE.source).catch(() => []);
    for (const t of [...new Set(triggers)].slice(0, 2)) {
      try {
        await page.locator('button', { hasText: t }).first().click({ timeout: 5000 });
        await page.waitForTimeout(1200);
        out.push(await dialogSnap(page, page.url(), `after clicking "${t}"`));
        await page.keyboard.press('Escape').catch(() => {});
        await page.waitForTimeout(500);
      } catch (e) { /* trigger not clickable — skip */ }
    }
  };

  const norm = u => u.replace(/\/$/, '');
  try {
    // 1) The login page itself.
    await go(base + loginPath);
    out.push(await snap(page, base + loginPath, 'login page'));

    // 2) Log in (generic: email/username + password + submit) so authenticated pages are reachable.
    let loggedIn = false;
    if (email && password) {
      try {
        const e = await page.$('input[type="email"], input[name*="email" i], input[id*="email" i], input[type="text"]');
        const p = await page.$('input[type="password"]');
        if (e && p) {
          await e.fill(email); await p.fill(password);
          const btn = await page.$('[type="submit"]')   // matches <button> AND <input type=submit>
            || await page.$('button:has-text("Log In")') || await page.$('button:has-text("Login")')
            || await page.$('button:has-text("Sign in")') || await page.$('button:has-text("Sign In")');
          if (btn) await btn.click();
          await page.waitForLoadState('networkidle', { timeout: 20000 }).catch(() => {});
          await page.waitForTimeout(1500);
          loggedIn = true;
        }
      } catch (e) { /* best-effort login */ }
    }

    // 3) Whatever page login LANDS on (e.g. /inventory.html, /dashboard) — captured automatically,
    //    so we ground the post-login assertion for ANY app regardless of its landing route.
    const seen = new Set([norm(base + loginPath)]);
    if (loggedIn && !seen.has(norm(page.url()))) {
      seen.add(norm(page.url()));
      await exploreCurrent('post-login landing');
    }

    // 4) Each explicitly requested route + its interactive states.
    for (const r of routes) {
      const url = base + (r.startsWith('/') ? r : '/' + r);
      if (seen.has(norm(url))) continue;
      seen.add(norm(url));
      try { await go(url); await exploreCurrent('page loaded'); }
      catch (e) { out.push({ url, error: e.message }); }
    }
    console.log(JSON.stringify(out));
  } catch (e) {
    console.log(JSON.stringify([{ error: e.message }]));
  } finally {
    await browser.close();
  }
})();
