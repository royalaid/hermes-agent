# Reasoning layout harness

This fixture renders one completed assistant message with four consecutive reasoning parts. It isolates the assistant-ui grouping and CSS layout from the gateway and Electron shell.

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
