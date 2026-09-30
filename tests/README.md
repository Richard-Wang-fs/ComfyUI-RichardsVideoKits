# Frontend regression tests

Run from the repository root with Node.js 22 (no npm packages required):

```sh
node --test tests/js/wan_loop_core.test.mjs tests/js/wan_loop_adapter.test.mjs tests/js/wan_loop_loading.test.mjs tests/js/wan_loop_timers.test.mjs tests/js/wan_loop_isolation.test.mjs
```

The tests cover prompt confirmation and automatic continuation, workflow isolation,
Stop and reconnect handling, status widgets, legacy workflow migration, native ESM
cache invalidation, and browser timer receiver constraints. The module-loading
fixture creates temporary files under the ignored `.local/` directory and removes
only its own temporary directory.

ComfyUI APIs and execution events are simulated. Timer receiver checks run in an
isolated JavaScript realm; they are not a real browser run. These tests do not load
models, submit ComfyUI prompts, or establish GPU or long-video memory performance.

Queue isolation checks pass raw inputs into the shipping adapter, including
reactive proxies and objects that structuredClone cannot copy. They verify that
workflows without Loop Entry keep the original arguments, receiver, result and
error behavior without RVK requests or graph mutation. RVK paths must also accept
JSON-compatible proxies and leave the supplied graph unchanged.
