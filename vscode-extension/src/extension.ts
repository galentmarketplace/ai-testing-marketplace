// AI Testing Marketplace — VS Code extension (hybrid: agent-native MCP + thin visual UI).
// The extension is a thin CLIENT of the independent platform: REST for data/runs, a webview that
// reuses the platform's own results dashboard, LCOV gutter overlays for coverage, and MCP
// registration so Copilot / Cursor agent mode can call the marketplace as tools.
import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';

const PLAYBOOKS: Record<string, { label: string; mode: string; tracks?: string[]; desc: string }> = {
  functional: { label: 'Functional (Jira → Cases → Playwright)', mode: 'custom', tracks: ['criteria', 'functional', 'delivery', 'jenkins'], desc: 'AC → cases → DOM-grounded Playwright → heal → Jenkins → PR → Jira' },
  full:       { label: 'Full Pipeline', mode: 'full', desc: 'story → criteria → code → unit, functional, perf, regression, security → PR' },
  perf:       { label: 'Performance', mode: 'perf', desc: 'open-model k6 + Web Vitals + per-pod CPU/mem' },
  security:   { label: 'Security Audit', mode: 'custom', tracks: ['security'], desc: 'SAST · SCA/CVE · secrets · IaC · SBOM → SARIF' },
  coverage:   { label: 'Go Code Coverage', mode: 'custom', tracks: ['coverage'], desc: 'go test -cover → LCOV/Cobertura + threshold gate' },
  regression: { label: 'Regression', mode: 'custom', tracks: ['regression', 'delivery', 'jenkins'], desc: 'impacted cases in Jenkins, heal to green, PR' },
  unit:       { label: 'Unit Tests', mode: 'custom', tracks: ['unit'], desc: 'run the repo unit suite and gate' },
};

const cfg = () => vscode.workspace.getConfiguration('aiTestingMarketplace');
const backend = () => (cfg().get<string>('backendUrl') || 'http://127.0.0.1:8090').replace(/\/$/, '');
const platformPath = () => cfg().get<string>('platformPath') || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || '';

async function api<T = any>(p: string, init?: RequestInit): Promise<T> {
  const r = await fetch(backend() + p, { ...init, headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) } });
  if (!r.ok) throw new Error(`${p} → HTTP ${r.status}`);
  return r.json() as Promise<T>;
}

// ---------------------------------------------------------------- tree views
class Item extends vscode.TreeItem {
  constructor(label: string, opts: { desc?: string; tip?: string; icon?: string; cmd?: vscode.Command; ctx?: string } = {}) {
    super(label, vscode.TreeItemCollapsibleState.None);
    this.description = opts.desc; this.tooltip = opts.tip; this.command = opts.cmd; this.contextValue = opts.ctx;
    if (opts.icon) this.iconPath = new vscode.ThemeIcon(opts.icon);
  }
}
class ListProvider implements vscode.TreeDataProvider<Item> {
  private _ev = new vscode.EventEmitter<void>(); readonly onDidChangeTreeData = this._ev.event;
  constructor(private loader: () => Promise<Item[]>) {}
  refresh() { this._ev.fire(); }
  getTreeItem(i: Item) { return i; }
  async getChildren() { try { return await this.loader(); } catch (e: any) { return [new Item('Backend unreachable', { desc: backend(), tip: String(e?.message || e), icon: 'warning' })]; } }
}

const statusIcon = (s: string) => ({ done: 'pass', blocked: 'error', error: 'error', running: 'sync~spin', queued: 'clock', interrupted: 'warning' } as any)[s] || 'circle-outline';

// ---------------------------------------------------------------- results webview (reuses the platform dashboard)
function openResults(runId?: string, view = 'results') {
  const panel = vscode.window.createWebviewPanel('atm.results', runId ? `Run ${runId.slice(0, 8)} — results` : 'AI Testing Marketplace',
    vscode.ViewColumn.Beside, { enableScripts: true, retainContextWhenHidden: true });
  const url = runId ? `${backend()}/?run=${encodeURIComponent(runId)}&view=${view}` : `${backend()}/`;
  panel.webview.html = `<!DOCTYPE html><html><head>
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; frame-src ${backend()} https:; style-src 'unsafe-inline';">
    <style>html,body,iframe{margin:0;padding:0;height:100%;width:100%;border:0;background:#0F172A}</style></head>
    <body><iframe src="${url}" allow="clipboard-read; clipboard-write"></iframe></body></html>`;
}

// ---------------------------------------------------------------- run a playbook (human-in-the-loop)
async function runPlaybook(preset?: string, extraInputs: Record<string, string> = {}, status?: vscode.StatusBarItem) {
  const pick = preset ? { id: preset } : await vscode.window.showQuickPick(
    Object.entries(PLAYBOOKS).map(([id, p]) => ({ label: p.label, description: p.desc, id })), { placeHolder: 'Which playbook?' });
  if (!pick) return;
  const pb = PLAYBOOKS[(pick as any).id]; if (!pb) return;

  let projectId: string | undefined, hasJira = false;
  try {
    const projects = (await api<{ projects: any[] }>('/api/projects')).projects || [];
    if (projects.length) {
      const c = await vscode.window.showQuickPick([{ label: 'None — enter details manually', id: '' },
        ...projects.map(p => ({ label: p.name, description: p.config?.app_url || '', id: p.id, jira: !!p.config?.jira_url }))], { placeHolder: 'Use a saved Configuration?' });
      if (c === undefined) return; projectId = (c as any).id || undefined; hasJira = !!(c as any).jira;
    }
  } catch { /* backend may be down; continue manual */ }

  const inputs: Record<string, string> = { ...extraInputs };
  const body: any = { mock: false, mode: pb.mode, inputs, story: { id: (pick as any).id.toUpperCase(), title: pb.label, description: '' } };
  if (pb.tracks) { body.tracks = pb.tracks; body.open_pr = pb.tracks.includes('delivery'); }
  if (projectId) body.project_id = projectId;
  if (pb.tracks?.includes('criteria')) {
    const ac = await vscode.window.showInputBox({ prompt: hasJira ? 'Jira ticket key (e.g. QA-1) — AC pulled live' : 'Jira ticket key or paste the acceptance criteria', ignoreFocusOut: true });
    if (ac === undefined) return;
    if (hasJira && /^[A-Za-z][A-Za-z0-9]+-\d+$/.test(ac.trim())) body.ticket = ac.trim(); else if (ac.trim()) body.ac_input = ac.trim();
    body.story.description = ac.trim();
  }
  if (!projectId && !inputs.repo && !inputs.base_url) {
    const repo = await vscode.window.showInputBox({ prompt: 'Target repository (owner/repo or URL) — blank to use the Configuration/workspace', ignoreFocusOut: true });
    if (repo) inputs.repo = repo;
  }
  const mock = await vscode.window.showQuickPick([{ label: 'Live (real agents)', v: false }, { label: 'Mock (no API cost, simulated)', v: true }], { placeHolder: 'Run mode' });
  if (!mock) return; body.mock = (mock as any).v;

  try {
    const { run_id } = await api<{ run_id: string }>('/api/run', { method: 'POST', body: JSON.stringify(body) });
    vscode.window.showInformationMessage(`▶ ${pb.label} started (${run_id.slice(0, 8)})`, 'Open live execution').then(a => a && openResults(run_id, 'run'));
    if (status) pollRun(run_id, pb.label, status);
    vscode.commands.executeCommand('atm.refresh');
  } catch (e: any) { vscode.window.showErrorMessage(`Could not start run: ${e.message}`); }
}

function pollRun(runId: string, label: string, status: vscode.StatusBarItem) {
  status.text = `$(sync~spin) ATM: ${label}…`; status.command = { command: 'atm.openResults', arguments: [runId], title: 'open' }; status.show();
  const t = setInterval(async () => {
    try {
      const r = await api<any>(`/api/runs/${runId}`);
      if (['done', 'blocked', 'error', 'interrupted'].includes(r.status)) {
        clearInterval(t);
        status.text = `$(${r.status === 'done' ? 'pass' : 'error'}) ATM: ${label} ${r.status}`;
        vscode.window.showInformationMessage(`${label}: ${r.status}`, 'Open results').then(a => a && openResults(runId));
        vscode.commands.executeCommand('atm.refresh');
      }
    } catch { /* keep polling */ }
  }, 4000);
}

// ---------------------------------------------------------------- MCP registration (agent-native path)
function mcpServerDef() {
  const cwd = platformPath();
  return { name: 'ai-testing-marketplace', command: cfg().get<string>('pythonPath') || 'python', args: ['-m', 'src.mcp_server'], cwd, env: { ATM_URL: backend() } };
}
async function registerMcp() {
  const d = mcpServerDef();
  const folder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  if (!folder) { vscode.window.showWarningMessage('Open a workspace folder first.'); return; }
  const dir = path.join(folder, '.vscode'); fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, 'mcp.json');
  let doc: any = {}; try { doc = JSON.parse(fs.readFileSync(file, 'utf8')); } catch { /* new */ }
  doc.servers = { ...(doc.servers || {}), [d.name]: { type: 'stdio', command: d.command, args: d.args, cwd: d.cwd, env: d.env } };
  fs.writeFileSync(file, JSON.stringify(doc, null, 2));
  vscode.window.showInformationMessage(`MCP server registered in .vscode/mcp.json — Copilot agent mode can now call list_playbooks / start_run / run_results.`);
}

// ---------------------------------------------------------------- coverage overlay (LCOV → gutter)
const covHit = vscode.window.createTextEditorDecorationType({ isWholeLine: true, backgroundColor: 'rgba(34,197,94,0.12)', overviewRulerColor: '#22C55E' });
const covMiss = vscode.window.createTextEditorDecorationType({ isWholeLine: true, backgroundColor: 'rgba(239,68,68,0.14)', overviewRulerColor: '#EF4444' });
let lcovData: Map<string, Map<number, number>> | null = null;
function parseLcov(text: string) {
  const out = new Map<string, Map<number, number>>(); let cur: Map<number, number> | null = null;
  for (const line of text.split(/\r?\n/)) {
    if (line.startsWith('SF:')) { cur = new Map(); out.set(line.slice(3).trim(), cur); }
    else if (line.startsWith('DA:') && cur) { const [ln, hits] = line.slice(3).split(','); cur.set(Number(ln), Number(hits)); }
    else if (line === 'end_of_record') cur = null;
  }
  return out;
}
function applyCoverage(editor?: vscode.TextEditor) {
  if (!editor || !lcovData) return;
  const fsPath = editor.document.uri.fsPath;
  let lines: Map<number, number> | undefined;
  for (const [f, m] of lcovData) if (fsPath.endsWith(f) || f.endsWith(path.basename(fsPath)) && fsPath.includes(path.dirname(f).split('/').slice(-1)[0] || '')) { lines = m; break; }
  if (!lines) { editor.setDecorations(covHit, []); editor.setDecorations(covMiss, []); return; }
  const hit: vscode.Range[] = [], miss: vscode.Range[] = [];
  for (const [ln, hits] of lines) { const r = editor.document.lineAt(Math.min(ln - 1, editor.document.lineCount - 1)).range; (hits > 0 ? hit : miss).push(r); }
  editor.setDecorations(covHit, hit); editor.setDecorations(covMiss, miss);
}
async function showCoverage() {
  const guess = path.join(platformPath(), 'generated', 'coverage', 'coverage.lcov');
  let file = fs.existsSync(guess) ? guess : undefined;
  if (!file) { const pick = await vscode.window.showOpenDialog({ filters: { LCOV: ['lcov', 'info'] }, canSelectMany: false, title: 'Pick an LCOV file' }); file = pick?.[0]?.fsPath; }
  if (!file) return;
  lcovData = parseLcov(fs.readFileSync(file, 'utf8'));
  vscode.window.visibleTextEditors.forEach(applyCoverage);
  const files = lcovData.size; let lf = 0, lh = 0; for (const m of lcovData.values()) for (const h of m.values()) { lf++; if (h > 0) lh++; }
  vscode.window.showInformationMessage(`Coverage overlay on: ${files} file(s), ${lf ? Math.round(100 * lh / lf) : 0}% lines covered (green = hit, red = missed).`);
}

// ---------------------------------------------------------------- CodeLens on Playwright specs
class SpecLens implements vscode.CodeLensProvider {
  provideCodeLenses(doc: vscode.TextDocument) {
    if (!/\.spec\.(ts|js)$/.test(doc.fileName)) return [];
    const r = new vscode.Range(0, 0, 0, 0);
    return [new vscode.CodeLens(r, { title: '▶ Heal / regenerate with AI Testing Marketplace', command: 'atm.runPlaybook', arguments: ['functional', { scope_prompt: `Heal the spec ${path.basename(doc.fileName)}` }] }),
            new vscode.CodeLens(r, { title: '$(graph) Open marketplace dashboard', command: 'atm.openDashboard' })];
  }
}

// ---------------------------------------------------------------- onboarding (first run for people who just installed it)
function startBackend() {
  const cwd = platformPath();
  if (!cwd) {
    vscode.window.showWarningMessage('Set "AI Testing Marketplace › Platform Path" to your agentic-testing-pipeline checkout first.', 'Open Settings')
      .then(a => a && vscode.commands.executeCommand('workbench.action.openSettings', 'aiTestingMarketplace.platformPath'));
    return;
  }
  let port = '8090'; try { port = new URL(backend()).port || '8090'; } catch { /* default */ }
  const term = vscode.window.createTerminal({ name: 'AI Testing Marketplace', cwd });
  term.sendText(`PORT=${port} ${cfg().get<string>('pythonPath') || 'python'} -m web.server`);
  term.show();
}
function openSetupGuide(ctx: vscode.ExtensionContext) {
  vscode.commands.executeCommand('workbench.action.openWalkthrough', `${ctx.extension.id}#atm.setup`, false);
}
async function checkBackend(ctx: vscode.ExtensionContext) {
  try {
    const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), 3000);
    const r = await fetch(backend() + '/api/manifest', { signal: ctl.signal }); clearTimeout(t);
    if (r.ok) return true;
  } catch { /* unreachable */ }
  const a = await vscode.window.showWarningMessage(
    `AI Testing Marketplace: no backend at ${backend()}. Run the platform locally or point to a hosted instance.`,
    'Start local backend', 'Configure URL', 'Setup guide');
  if (a === 'Start local backend') startBackend();
  else if (a === 'Configure URL') vscode.commands.executeCommand('workbench.action.openSettings', 'aiTestingMarketplace.backendUrl');
  else if (a === 'Setup guide') openSetupGuide(ctx);
  return false;
}

// ---------------------------------------------------------------- activate
export function activate(ctx: vscode.ExtensionContext) {
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  const playbooks = new ListProvider(async () => Object.entries(PLAYBOOKS).map(([id, p]) =>
    new Item(p.label, { desc: id, tip: p.desc, icon: 'play', cmd: { command: 'atm.runPlaybook', title: 'run', arguments: [id] } })));
  const configurations = new ListProvider(async () => ((await api<{ projects: any[] }>('/api/projects')).projects || []).map(p =>
    new Item(p.name, { desc: p.config?.app_url || '', icon: 'settings-gear', tip: `repo ${p.config?.source_repo || '—'} · jira ${p.config?.jira_url ? 'yes' : 'no'}` })));
  const runs = new ListProvider(async () => ((await api<{ runs: any[] }>('/api/runs')).runs || []).slice(0, 30).map(r =>
    new Item(`#${r.num ?? ''} ${r.flow || r.mode}`, { desc: r.status, icon: statusIcon(r.status), tip: new Date((r.created_at || 0) * 1000).toLocaleString(),
      cmd: { command: 'atm.openResults', title: 'open', arguments: [r.id] } })));

  ctx.subscriptions.push(
    vscode.window.registerTreeDataProvider('atm.playbooks', playbooks),
    vscode.window.registerTreeDataProvider('atm.configurations', configurations),
    vscode.window.registerTreeDataProvider('atm.runs', runs),
    vscode.commands.registerCommand('atm.runPlaybook', (preset?: string, extra?: Record<string, string>) => runPlaybook(preset, extra || {}, status)),
    vscode.commands.registerCommand('atm.openResults', (runId?: string) => openResults(runId)),
    vscode.commands.registerCommand('atm.openDashboard', () => openResults(undefined)),
    vscode.commands.registerCommand('atm.refresh', () => { playbooks.refresh(); configurations.refresh(); runs.refresh(); }),
    vscode.commands.registerCommand('atm.registerMcp', registerMcp),
    vscode.commands.registerCommand('atm.showCoverage', showCoverage),
    vscode.commands.registerCommand('atm.startBackend', startBackend),
    vscode.commands.registerCommand('atm.setupGuide', () => openSetupGuide(ctx)),
    vscode.languages.registerCodeLensProvider([{ language: 'typescript' }, { language: 'javascript' }], new SpecLens()),
    vscode.window.onDidChangeActiveTextEditor(applyCoverage),
    status,
  );

  // Agent-native: if this VS Code exposes the MCP definition API, provide the server directly (no file edits needed).
  const lm: any = (vscode as any).lm;
  if (lm?.registerMcpServerDefinitionProvider) {
    try {
      ctx.subscriptions.push(lm.registerMcpServerDefinitionProvider('atm.mcp', {
        provideMcpServerDefinitions: () => { const d = mcpServerDef(); const Def = (vscode as any).McpStdioServerDefinition;
          return Def ? [new Def(d.name, d.command, d.args, d.env, undefined, d.cwd ? vscode.Uri.file(d.cwd) : undefined)] : []; },
      }));
    } catch { /* older VS Code — fall back to atm.registerMcp */ }
  }
  setInterval(() => runs.refresh(), 15000);
  void checkBackend(ctx);   // first-run guidance instead of a silently empty sidebar
}
export function deactivate() {}
