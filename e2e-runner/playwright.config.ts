import { defineConfig } from '@playwright/test';

// The pipeline sets PW_TESTDIR to the folder holding the agent-generated spec(s).
export default defineConfig({
  testDir: process.env.PW_TESTDIR || '.',
  timeout: 30000,
  // A failing test retries once WITH tracing on — the trace + our failure-context attachment
  // (see fixtures.ts) give the healer the real page state, not just an error line.
  retries: process.env.PW_RETRIES ? Number(process.env.PW_RETRIES) : 0,
  use: {
    headless: true,
    ignoreHTTPSErrors: true,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
});
