/**
 * Core Web Vitals capture — the browser/UX dimension of web performance that protocol load tests
 * (k6) can't see. Launches the running web app in real Chromium and collects LCP / CLS / TTFB / FCP
 * via the Performance APIs. Prints one JSON line: { lcp, cls, ttfb, fcp } (ms; cls is unitless).
 *
 * Invoked by the Performance agent: `node webvitals.cjs <url>`. Best-effort + advisory.
 * (INP needs real interaction/field data; these are the lab-measurable load vitals.)
 */
const { chromium } = require('playwright');

(async () => {
  const url = process.argv[2];
  if (!url) { console.error('usage: node webvitals.cjs <url>'); process.exit(1); }
  const browser = await chromium.launch();
  const page = await browser.newPage();
  try {
    await page.goto(url, { waitUntil: 'load', timeout: 30000 });
  } catch (e) {
    console.error('navigation failed: ' + e.message);
    await browser.close();
    process.exit(1);
  }
  const vitals = await page.evaluate(() => new Promise((resolve) => {
    const out = { lcp: null, cls: 0, fcp: null, ttfb: null };
    try {
      const nav = performance.getEntriesByType('navigation')[0];
      if (nav) out.ttfb = Math.max(0, nav.responseStart - nav.requestStart);
      const fcp = performance.getEntriesByType('paint').find((e) => e.name === 'first-contentful-paint');
      if (fcp) out.fcp = fcp.startTime;
      new PerformanceObserver((l) => {
        const es = l.getEntries();
        if (es.length) out.lcp = es[es.length - 1].startTime;
      }).observe({ type: 'largest-contentful-paint', buffered: true });
      new PerformanceObserver((l) => {
        for (const e of l.getEntries()) if (!e.hadRecentInput) out.cls += e.value;
      }).observe({ type: 'layout-shift', buffered: true });
    } catch (e) { /* partial vitals are fine */ }
    // give LCP/CLS observers a moment to settle after load
    setTimeout(() => resolve(out), 3000);
  }));
  console.log(JSON.stringify(vitals));
  await browser.close();
})().catch((e) => { console.error(e.message); process.exit(1); });
