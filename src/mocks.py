"""Canned LLM responses for MOCK_LLM=1 mode (demo/dev without an API key)."""

MOCK_RESPONSES = {
    "ac_agent": """{
  "story_id": "DEMO-1",
  "criteria": [
    {"id": "AC-1", "gherkin": "Given I am logged into IDURAR as an admin\\nWhen I create a new invoice for customer 'Acme Corp' with one line item of $100\\nThen the invoice appears in the invoice list with status 'Draft' and total $100", "priority": "must", "tags": ["invoice"]},
    {"id": "AC-2", "gherkin": "Given an invoice exists with status 'Draft'\\nWhen I record a full payment against it\\nThen the invoice status changes to 'Paid'", "priority": "must", "tags": ["invoice", "payment"]},
    {"id": "AC-3", "gherkin": "Given I am on the create invoice form\\nWhen I submit without selecting a customer\\nThen a validation error 'Customer is required' is shown", "priority": "should", "tags": ["invoice", "validation"]}
  ]
}""",
    "dev_agent": """{
  "files": [
    {"path": "generated/code/invoiceStatus.js", "description": "Invoice status transition helper", "content": "// STUB: generated feature code would go here\\nfunction canTransition(from, to) {\\n  const allowed = { Draft: ['Sent', 'Paid'], Sent: ['Paid'] };\\n  return (allowed[from] || []).includes(to);\\n}\\nmodule.exports = { canTransition };"},
    {"path": "generated/code/invoiceStatus.test.js", "description": "Jest unit tests", "content": "const { canTransition } = require('./invoiceStatus');\\ntest('draft can be paid', () => expect(canTransition('Draft','Paid')).toBe(true));\\ntest('paid is terminal', () => expect(canTransition('Paid','Draft')).toBe(false));"}
  ]
}""",
    "ui_automation_agent": """{
  "files": [
    {"path": "generated/e2e/invoice-create.spec.ts", "covers_ac": ["AC-1", "AC-3"], "tags": ["@invoice", "@smoke"], "content": "import { test, expect } from '@playwright/test';\\n\\ntest.describe('Invoice creation @invoice @smoke', () => {\\n  test.beforeEach(async ({ page }) => {\\n    await page.goto('/login');\\n    await page.getByLabel('Email').fill(process.env.IDURAR_USER!);\\n    await page.getByLabel('Password').fill(process.env.IDURAR_PASS!);\\n    await page.getByRole('button', { name: 'Log in' }).click();\\n  });\\n\\n  test('AC-1: create invoice with one line item', async ({ page }) => {\\n    await page.goto('/invoice');\\n    await page.getByRole('button', { name: 'Add New Invoice' }).click();\\n    await page.getByLabel('Client').click();\\n    await page.getByText('Acme Corp').click();\\n    await page.getByPlaceholder('Item Name').fill('Consulting');\\n    await page.getByPlaceholder('Price').fill('100');\\n    await page.getByRole('button', { name: 'Save' }).click();\\n    await expect(page.getByText('Draft')).toBeVisible();\\n  });\\n\\n  test('AC-3: customer required validation', async ({ page }) => {\\n    await page.goto('/invoice');\\n    await page.getByRole('button', { name: 'Add New Invoice' }).click();\\n    await page.getByRole('button', { name: 'Save' }).click();\\n    await expect(page.getByText('Customer is required')).toBeVisible();\\n  });\\n});"}
  ]
}""",
    "perf_agent": """{
  "files": [
    {"path": "generated/perf/invoice-api.k6.js", "covers_ac": ["AC-1"], "tags": ["@invoice", "@perf"], "content": "import http from 'k6/http';\\nimport { check } from 'k6';\\n\\nexport const options = {\\n  vus: 10,\\n  duration: '30s',\\n  thresholds: {\\n    http_req_duration: ['p(95)<800'],\\n    http_req_failed: ['rate<0.01'],\\n  },\\n};\\n\\nexport default function () {\\n  const res = http.get(`${__ENV.BASE_URL}/api/invoice/list`, {\\n    headers: { Authorization: `Bearer ${__ENV.TOKEN}` },\\n  });\\n  check(res, { 'status 200': (r) => r.status === 200 });\\n}"}
  ]
}""",
    "regression_agent": """{
  "selected_tags": ["@invoice", "@regression"],
  "reasoning": "Story touches invoice creation and payment status; impacted areas are invoice CRUD and payment flows. Customer module untouched, excluded."
}""",
    "pr_agent": """{
  "title": "DEMO-1: Invoice status transitions",
  "body": "## Summary\\nImplements invoice status transitions per DEMO-1.\\n\\n## Acceptance criteria covered\\n- AC-1, AC-2, AC-3\\n\\n## Quality gates\\n- Unit: PASS (2/2, coverage 84%)\\n- QG1 feature: PASS (smoke 100%, p95 640ms)\\n- QG2 regression: PASS (98.6%)"
}""",
}
