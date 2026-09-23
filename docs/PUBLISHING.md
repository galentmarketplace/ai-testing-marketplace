# Publishing the VS Code extension

Everything is prepared in `vscode-extension/` (PNG icon, Marketplace README, CHANGELOG, walkthrough,
license, CI). Two things only the account owner can do — then it's one command.

## One-time setup (5 minutes) — verified 2026-09-23
Use a **personal Microsoft account** (outlook/hotmail/live). Corporate Entra ID tenants often block Azure
DevOps/Marketplace and surface it as a bare **404** after sign-in. Use an incognito window, off any VPN.

1. **Azure DevOps identity FIRST** — https://aex.dev.azure.com/signup → sign in → *Create new organization*
   (any name; it only hosts the token). Visiting the publisher page before this exists can 404.
2. **Create the publisher** — https://marketplace.visualstudio.com/manage → left pane → **Create publisher**
   → ID **`AITestingMarketplace`** (permanent; must match `publisher` in `vscode-extension/package.json`) · Name *AI Testing Marketplace*.
3. **Create the PAT** — in that Azure DevOps org: user settings (top-right) → *Personal access tokens* →
   *New Token* → Organization **All accessible organizations** (not a single org, or publishing fails) →
   Scopes **Custom defined → Marketplace → Manage** → Create → copy once.
4. **Store it as a repo secret, never in chat/docs** — `gh secret set VSCE_PAT` (reads from your keyboard) or
   GitHub → Settings → Secrets and variables → Actions → `VSCE_PAT`.
5. Code lives at https://github.com/galentmarketplace/ai-testing-marketplace (the Marketplace links README/issues).

> **PAT retirement:** Microsoft retires global Azure DevOps PATs on **1 Dec 2026** in favour of Entra ID managed
> identity for publishing. This workflow works until then; plan the switch (`vsce` supports `--azure-credential`).

## Publish
```bash
cd vscode-extension
npm ci && npm run compile
npx @vscode/vsce login AITestingMarketplace         # paste the PAT once
npm run publish                                    # → live on the Marketplace in ~5 minutes
```
Or via CI: add the PAT as repo secret **`VSCE_PAT`** (and optionally **`OVSX_PAT`** from
https://open-vsx.org for Cursor/VSCodium users) and push a tag: `git tag ext-v0.1.0 && git push --tags`.

## Releasing updates
Bump `version` in `package.json` (or `npx @vscode/vsce publish patch`), add a CHANGELOG entry, tag `ext-vX.Y.Z`.

## What users need
The extension is a client: they need a backend — either they run the platform locally (the walkthrough
shows how) or you give them a **hosted instance URL**. To let "anyone with the editor" use it *without*
self-hosting, deploy the platform (see the deploy notes) and put that URL in the README.
