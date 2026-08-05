# Reasoning layout harness

This fixture starts from the persisted native OpenAI Responses shape that exposed the bug: two Codex reasoning items containing four total `summary_text` records plus a lossy flattened `reasoning` fallback. It passes that record through production `toChatMessages`, then renders the resulting production assistant transcript. This proves hydration, item preservation, assistant-ui grouping, and CSS layout together while remaining isolated from the gateway and Electron shell.

From `apps/desktop`:

```bash
npm run harness:reasoning
# In another terminal:
npm run harness:reasoning:assert
```

The assertion expands the disclosure and fails unless all four reasoning parts render as `display: block` at four distinct vertical positions. Pass a URL and optional screenshot path directly when using a non-default port:

```bash
node harness/reasoning-layout/assert.mjs http://127.0.0.1:5574/harness/reasoning-layout/ reasoning-layout.png
```
