# Publishing the VS Code extension

Everything is prepared in `vscode-extension/` (PNG icon, Marketplace README, CHANGELOG, walkthrough,
license, CI). Two things only the account owner can do — then it's one command.

## One-time setup (5 minutes)
1. **Create a publisher** at https://marketplace.visualstudio.com/manage — sign in with a Microsoft
   account, *Create publisher*, ID **`ai-testing-marketplace`** (must match `publisher` in
   `vscode-extension/package.json`; if the ID is taken, pick another and update that field).
2. **Create a Personal Access Token** at https://dev.azure.com → User settings → Personal access tokens →
   *New Token*: Organization **All accessible organizations**, Scopes → **Marketplace: Manage**. Copy it.
3. **Push the code to the public repo** referenced in `package.json`
   (`https://github.com/galentmarketplace/ai-testing-marketplace`) — the Marketplace links README/issues there.

## Publish
```bash
cd vscode-extension
npm ci && npm run compile
npx @vscode/vsce login ai-testing-marketplace      # paste the PAT once
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
