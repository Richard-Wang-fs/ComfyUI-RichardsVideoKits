import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const coreSource = readFileSync(new URL("../../web/wan_loop_core.mjs", import.meta.url), "utf8");

function fixture() {
  // Node timers accept arbitrary receivers. This isolated realm models the
  // browser receiver check without replacing timer defaults in the controller.
  const realm = vm.createContext({});
  vm.runInContext(`
    "use strict";
    let nextTimer = 0;
    globalThis.pendingTimers = new Map();
    globalThis.clearedTimers = [];
    globalThis.setTimeout = function(callback, delay) {
      if (this !== globalThis) throw new TypeError("Illegal invocation: setTimeout");
      const handle = ++nextTimer;
      pendingTimers.set(handle, { callback, delay });
      return handle;
    };
    globalThis.clearTimeout = function(handle) {
      if (this !== globalThis) throw new TypeError("Illegal invocation: clearTimeout");
      clearedTimers.push(handle);
      pendingTimers.delete(handle);
    };
    globalThis.fireTimer = function(handle) {
      const timer = pendingTimers.get(handle);
      if (!timer) throw new Error("Unknown timer " + handle);
      pendingTimers.delete(handle);
      return timer.callback();
    };
  `, realm);
  // Only ESM export markers are removed; the shipped constructor and timer
  // call sites execute unchanged in the realm with browser-like host methods.
  vm.runInContext(coreSource.replace(/^export /gm, "") + "\nglobalThis.Controller = WanLoopFrontendController;", realm, { filename: "wan_loop_core.mjs" });
  const queued = [], controls = [], updates = [];
  let now = 0;
  const controller = new realm.Controller({
    isEntryActive: (workflow, node) => workflow === "workflow" && node === "7",
    setEntryStatus: (_workflow, _node, status) => updates.push(status),
    controlRun: async (...args) => controls.push(args),
    now: () => now,
    queueTimeoutMs: 15,
    blockedSessionTtlMs: 30,
    queuePrompt: async (workflowId, entryNodeId, sessionId) => {
      queued.push([workflowId, entryNodeId, sessionId]);
      const next = controller.beginSubmission({ workflowId, entryNodeId, sessionId });
      await controller.confirmSubmission(next, "prompt-2");
      return "prompt-2";
    },
  });
  return {
    controller, queued, controls, updates,
    timers: realm.pendingTimers,
    cleared: realm.clearedTimers,
    fire: (handle) => realm.fireTimer(handle),
    setNow: (value) => { now = value; },
  };
}

test("browser timer receiver survives a fulfilled queue promise and clears its timeout", async () => {
  const f = fixture();
  const pending = f.controller.withTimeout(Promise.resolve("prompt-1"));
  const scheduledCount = f.timers.size;
  const handle = [...f.timers.keys()][0];
  const delay = f.timers.get(handle)?.delay;
  assert.equal(await pending, "prompt-1");
  assert.equal(scheduledCount, 1);
  assert.equal(delay, 15);
  assert.equal(f.timers.size, 0);
  assert.deepEqual([...f.cleared], [handle]);
});

test("browser timer receiver preserves queue rejection and clears its timeout", async () => {
  const f = fixture();
  const pending = f.controller.withTimeout(Promise.reject(new Error("queue offline")));
  const handle = [...f.timers.keys()][0];
  await assert.rejects(pending, /queue offline/);
  assert.equal(f.timers.size, 0);
  assert.deepEqual([...f.cleared], [handle]);
});

test("browser timer receiver allows queue timeout rejection and cleanup", async () => {
  const f = fixture();
  const pending = f.controller.withTimeout(new Promise(() => {}));
  const rejected = assert.rejects(pending, /RVK 排队确认超时/);
  const scheduledCount = f.timers.size;
  const handle = [...f.timers.keys()][0];
  if (handle !== undefined) f.fire(handle);
  await rejected;
  assert.equal(scheduledCount, 1);
  assert.equal(f.timers.size, 0);
  assert.deepEqual([...f.cleared], [handle]);
});

test("blocked sessions release and expire through browser timer methods", () => {
  const f = fixture();
  f.controller.blockSession("released");
  const released = [...f.timers.keys()][0];
  assert.equal(f.timers.get(released).delay, 30);
  assert.equal(f.controller.isBlocked("released"), true);
  f.controller.releaseBlocked("released");
  assert.equal(f.controller.isBlocked("released"), false);
  assert.equal(f.timers.size, 0);

  f.controller.blockSession("timer-expired");
  const expired = [...f.timers.keys()][0];
  f.fire(expired);
  assert.equal(f.controller.isBlocked("timer-expired"), false);
  assert.equal(f.timers.size, 0);

  f.controller.blockSession("clock-expired");
  const clockExpired = [...f.timers.keys()][0];
  f.setNow(31);
  assert.equal(f.controller.isBlocked("clock-expired"), false);
  assert.equal(f.timers.size, 0);
  assert.deepEqual([...f.cleared], [released, expired, clockExpired]);
});

test("early buffered success defers once with browser timers before continuing", async () => {
  const f = fixture();
  const scope = f.controller.beginSubmission({ workflowId: "workflow", entryNodeId: "7" });
  await f.controller.handleExecuted({
    prompt_id: "prompt-1",
    output: { rvk_wan_entry: [{ version: 3, kind: "entry", session_id: "session", workflow_id: "workflow", entry_node_id: "7", segment_index: 0 }] },
  });
  await f.controller.handleExecuted({
    prompt_id: "prompt-1",
    output: { rvk_wan_loop: [{ version: 3, kind: "advance", session_id: "session", workflow_id: "workflow", entry_node_id: "7", has_more: true, next_segment: 1 }] },
  });
  await f.controller.handleSuccess({ prompt_id: "prompt-1" });
  assert.equal(f.queued.length, 0);
  await f.controller.confirmSubmission(scope, "prompt-1");
  assert.equal(f.queued.length, 0);
  assert.equal(f.timers.size, 1);
  const deferred = [...f.timers.keys()][0];
  assert.equal(f.timers.get(deferred).delay, 0);

  // Capture the returned completion promise because the production deferred
  // callback deliberately uses void instead of returning it to the timer host.
  let completion;
  const complete = f.controller.complete.bind(f.controller);
  f.controller.complete = (value) => (completion = complete(value));
  f.fire(deferred);
  assert.equal(await completion, true);
  assert.deepEqual(f.queued, [["workflow", "7", "session"]]);
  assert.equal(f.controller.prompts.get("prompt-2").confirmed, true);
  assert.equal(f.timers.size, 0);
  assert.equal(f.cleared.length, 1);
  assert.equal(await f.controller.handleSuccess({ prompt_id: "prompt-1" }), false);
  assert.equal(f.queued.length, 1);
  assert.deepEqual(f.controls, []);
});
