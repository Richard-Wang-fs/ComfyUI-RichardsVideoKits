import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";
import * as core from "../../web/wan_loop_core.mjs";

// Exercise the shipping adapter and controller together. Keep the supplied
// objects intact: cloning them in this harness would mask the original bug.
const adapter = await readFile(new URL("../../web/wan_loop.js", import.meta.url), "utf8");
const source = '"use strict";\n' + adapter.replace(/import\s+[\s\S]*?\s+from\s+"[^"]+";\s*/g, "");
assert.doesNotMatch(source, /^import /m);

function ordinary() {
  return {
    workflow: { id: "plain", extra: { rvk_runtime: { owner: "unrelated-workflow" }, ui: { zoom: 1 } }, nodes: [] },
    output: { "1": { class_type: "EmptyImage", inputs: { width: 64 } } },
  };
}

function loop(mode = "new") {
  return {
    workflow: { id: "loop", extra: { rvk_runtime: { session_id: "saved-stale-session" }, ui: { zoom: 1 } }, nodes: [] },
    output: {
      "1": { class_type: "LoadVideo", inputs: { file: "drive.mp4" } },
      "7": { class_type: "RVKWanLoopEntry", inputs: { drive_video: ["1", 0], mode, output_directory: "work", segment_length: 9 } },
      "8": { class_type: "KSampler", inputs: { latent_image: ["7", 0] } },
      "9": { class_type: "RVKSaveSegmentVideo", inputs: { images: ["8", 0] } },
      "10": { class_type: "RVKWanAdvance", inputs: { plan: ["7", 4], receipt: ["9", 0] } },
      "11": { class_type: "RVKFinalizeSegments", inputs: { source_video: ["1", 0], output_directory: ["7", 9], loop_status: ["10", 0], destination_directory: "final", final_filename: "final.mp4" } },
    },
  };
}

function proxies(value, seen = new WeakMap()) {
  if (!value || typeof value !== "object") return value;
  if (seen.has(value)) return seen.get(value);
  const result = new Proxy(value, { get: (target, key, receiver) => proxies(Reflect.get(target, key, receiver), seen) });
  seen.set(value, result);
  return result;
}

function freeze(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    for (const item of Object.values(value)) freeze(item);
    Object.freeze(value);
  }
  return value;
}

async function fixture(current = ordinary(), { implementation, completed = false } = {}) {
  const calls = [], requests = [], notices = [], listeners = new Map();
  const graphReads = { serialize: 0, prompt: 0 };
  let extension, serializing = false;
  const node = { id: 7, comfyClass: "RVKWanLoopEntry" };
  const hasEntry = Object.values(current.output).some((item) => item.class_type === "RVKWanLoopEntry");
  const api = {
    addEventListener: (name, callback) => listeners.set(name, callback),
    fetchApi: async (path, options) => {
      requests.push({ path, body: JSON.parse(options.body) });
      return { ok: true, json: async () => ({ ok: true, completed, can_run: !completed, completed_segments: completed ? 3 : 0, total_segments: 3, completed_frames: completed ? 20 : 0, total_frames: 20, output_directory: "work", partials: [] }) };
    },
    queuePrompt: function(...args) {
      // Match Comfy's JSON serialization boundary, without structuredClone.
      let json;
      serializing = true;
      try { json = JSON.stringify(args[1]); } finally { serializing = false; }
      calls.push({ receiver: this, args, json });
      return implementation ? implementation.apply(this, args) : Promise.resolve({ prompt_id: `p${calls.length}` });
    },
  };
  const app = {
    rootGraph: {
      serialize: () => { graphReads.serialize++; return current.workflow; },
      _nodes: hasEntry ? [node] : [],
      getNodeById: (id) => hasEntry && String(id) === "7" ? node : null,
      setDirtyCanvas() {},
    },
    registerExtension: (value) => { extension = value; },
    extensionManager: { toast: { add: (value) => notices.push(value) } },
    graphToPrompt: async () => { graphReads.prompt++; return current; },
    queuePrompt: async () => { await api.queuePrompt(0, current); return true; },
  };
  vm.runInNewContext(source, { app, api, structuredClone, ...core });
  await extension.setup();
  const emit = async (name, detail) => {
    listeners.get(name)?.({ detail });
    await new Promise((resolve) => setImmediate(resolve));
  };
  return { api, calls, requests, notices, extension, emit, graphReads, get serializing() { return serializing; } };
}

function assertUntouched(f, prompt, before, receiver, args) {
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].receiver, receiver);
  assert.equal(f.calls[0].args.length, args.length);
  for (let index = 0; index < args.length; index++) assert.equal(f.calls[0].args[index], args[index]);
  assert.equal(JSON.stringify(prompt), before);
  assert.equal(f.calls[0].json, before);
  assert.deepEqual(f.requests, []);
  assert.deepEqual(f.notices, []);
}

const ordinaryCases = [
  ["plain objects", (prompt) => prompt],
  ["transparent root Proxy", (prompt) => new Proxy(prompt, {})],
  ["nested reactive-style Proxies", proxies],
  ["function and Symbol UI metadata", (prompt) => { prompt.workflow.extra.ui.callback = () => {}; prompt.workflow.extra.ui.marker = Symbol("ui"); return prompt; }],
  ["frozen workflow and stale RVK-shaped metadata", (prompt) => { freeze(prompt.workflow); return prompt; }],
];

for (const [name, transform] of ordinaryCases) {
  test(`non-RVK ${name} reaches the original API unchanged`, async () => {
    const prompt = transform(ordinary());
    const before = JSON.stringify(prompt);
    const response = { prompt_id: "ordinary" };
    const returned = Promise.resolve(response);
    const f = await fixture(prompt, { implementation: () => returned });
    const receiver = { owner: "other-extension" }, options = { partialExecutionTargets: ["1"] };
    const args = [42, prompt, options, "future-option"];
    const actual = f.api.queuePrompt.apply(receiver, args);
    // A global wrapper must not even replace another extension's Promise.
    const resolved = await actual;
    assert.equal(actual, returned);
    assert.equal(resolved, response);
    assertUntouched(f, prompt, before, receiver, args);
  });
}

test("non-RVK submissions do not inspect workflow metadata before the original API", async () => {
  const prompt = ordinary(), workflow = prompt.workflow;
  let f, extensionReads = 0;
  Object.defineProperty(prompt, "workflow", { enumerable: true, get() { if (!f.serializing) extensionReads++; return workflow; } });
  f = await fixture(prompt);
  await f.api.queuePrompt(0, prompt);
  assert.equal(extensionReads, 0);
  assert.equal(f.calls.length, 1);
  assert.deepEqual(f.requests, []);
});

test("non-RVK synchronous return value is preserved", async () => {
  const returned = { supportedByAnotherExtension: true };
  const f = await fixture(ordinary(), { implementation: () => returned });
  const actual = f.api.queuePrompt(0, ordinary());
  if (actual instanceof Promise) await actual;
  assert.equal(actual, returned);
});

test("non-RVK synchronous exception remains synchronous and identical", async () => {
  const failure = { origin: "original-api" };
  const f = await fixture(ordinary(), { implementation: () => { throw failure; } });
  let caught, returned;
  try { returned = f.api.queuePrompt(0, ordinary()); } catch (error) { caught = error; }
  // Consume the buggy async wrapper's rejection so the test reports the actual
  // synchronous contract failure instead of an unrelated unhandled rejection.
  if (returned?.catch) await returned.catch(() => {});
  assert.equal(caught, failure);
  assert.equal(returned, undefined);
  assert.deepEqual(f.requests, []);
});

test("non-RVK rejected Promise and rejection object are preserved", async () => {
  const failure = { origin: "original-api-rejection" };
  const returned = Promise.reject(failure);
  returned.catch(() => {});
  const f = await fixture(ordinary(), { implementation: () => returned });
  const actual = f.api.queuePrompt(0, ordinary());
  await assert.rejects(actual, (error) => error === failure);
  assert.equal(actual, returned);
  assert.deepEqual(f.requests, []);
});

for (const classType of ["RVKSaveSegmentVideo", "RVKFinalizeSegments"]) {
  test(`${classType} without Loop Entry is not admitted to the loop controller`, async () => {
    const plain = ordinary(); plain.output["1"].class_type = classType;
    const prompt = proxies(plain), before = JSON.stringify(prompt);
    const f = await fixture(prompt);
    const args = [0, prompt, { partialExecutionTargets: ["1"] }];
    await f.api.queuePrompt(...args);
    assertUntouched(f, prompt, before, f.api, args);
  });
}

for (const frozen of [false, true]) {
  test(`loading a ${frozen ? "frozen" : "mutable"} workflow without Entry preserves RVK-shaped metadata`, async () => {
    const workflow = ordinary().workflow, metadata = workflow.extra.rvk_runtime;
    if (frozen) freeze(workflow);
    const before = JSON.stringify(workflow), f = await fixture();
    await f.extension.beforeConfigureGraph(workflow);
    assert.equal(workflow.extra.rvk_runtime, metadata);
    assert.equal(JSON.stringify(workflow), before);
    assert.deepEqual(f.requests, []);
    assert.deepEqual(f.notices, []);
  });
}

test("non-RVK graph load, reconnect, and foreign execution events do not inspect or control anything", async () => {
  const prompt = ordinary(), before = JSON.stringify(prompt), f = await fixture(prompt);
  await f.extension.beforeConfigureGraph(prompt.workflow);
  await f.extension.afterConfigureGraph();
  await f.emit("reconnected");
  await f.emit("execution_start", { prompt_id: "foreign" });
  await f.emit("executing", "1");
  await f.emit("executed", { prompt_id: "foreign", output: { images: [] } });
  await f.emit("execution_success", { prompt_id: "foreign" });
  await f.emit("execution_error", { prompt_id: "foreign", exception_message: "another node failed" });
  await f.emit("execution_interrupted", { prompt_id: "foreign" });
  assert.deepEqual(f.graphReads, { serialize: 0, prompt: 0 });
  assert.deepEqual(f.requests, []);
  assert.deepEqual(f.notices, []);
  assert.deepEqual(f.calls, []);
  assert.equal(JSON.stringify(prompt), before);
});

for (const mode of ["new", "resume"]) {
  test(`RVK ${mode} accepts nested Proxies without mutating submitted metadata`, async () => {
    const plain = loop(mode); plain.workflow.extra.ui.callback = () => {};
    const prompt = proxies(plain), before = JSON.stringify(prompt), f = await fixture(prompt);
    await f.api.queuePrompt(0, prompt);
    const submitted = f.calls[0].args[1];
    assert.equal(JSON.stringify(prompt), before);
    assert.equal(submitted.workflow.extra.rvk_runtime, undefined);
    assert.notEqual(submitted, prompt);
    assert.notEqual(submitted.workflow, prompt.workflow);
    assert.notEqual(submitted.workflow.extra, prompt.workflow.extra);
    assert.equal(submitted.workflow.extra.ui, prompt.workflow.extra.ui);
    assert.equal(submitted.output, prompt.output);
    assert.deepEqual(f.requests.map((request) => request.path), ["/rvk/wan-loop/inspect"]);
  });
}

test("completed RVK resume prunes a Proxy graph without changing Finalize inputs or queue options", async () => {
  const prompt = proxies(loop("resume")), before = JSON.stringify(prompt), f = await fixture(prompt, { completed: true });
  const options = freeze({ partialExecutionTargets: ["7"], anotherOption: { enabled: true } });
  await f.api.queuePrompt(0, prompt, options);
  const submitted = f.calls[0].args[1], sentOptions = f.calls[0].args[2];
  assert.equal(JSON.stringify(prompt), before);
  assert.deepEqual(Object.keys(submitted.output), ["1", "11"]);
  assert.equal(submitted.output["1"], prompt.output["1"]);
  assert.notEqual(submitted.output["11"], prompt.output["11"]);
  assert.notEqual(submitted.output["11"].inputs, prompt.output["11"].inputs);
  assert.equal(submitted.output["11"].inputs.output_directory, "work");
  assert.equal(submitted.output["11"].inputs.loop_status, undefined);
  assert.deepEqual(options.partialExecutionTargets, ["7"]);
  assert.equal(sentOptions.partialExecutionTargets, undefined);
  assert.equal(sentOptions.anotherOption, options.anotherOption);
  assert.deepEqual(f.requests.map((request) => request.path), ["/rvk/wan-loop/inspect", "/rvk/wan-loop/prepare-complete"]);
});

test("RVK automatic continuation handles Proxy prompts while its session remains private", async () => {
  const prompt = proxies(loop()), before = JSON.stringify(prompt), f = await fixture(prompt);
  await f.api.queuePrompt(0, prompt);
  await f.emit("executed", { prompt_id: "p1", output: { rvk_wan_entry: [{ version: 3, kind: "entry", session_id: "active-session", workflow_id: "loop", entry_node_id: "7", segment_index: 0 }] } });
  await f.emit("executed", { prompt_id: "p1", output: { rvk_wan_loop: [{ version: 3, kind: "advance", session_id: "active-session", workflow_id: "loop", entry_node_id: "7", next_segment: 1, completed_segment: 0, has_more: true, stopped: false }] } });
  await f.emit("execution_success", { prompt_id: "p1" });
  assert.equal(f.calls.length, 2);
  assert.equal(JSON.stringify(prompt), before);
  assert.equal(f.calls[0].args[1].workflow.extra.rvk_runtime, undefined);
  assert.deepEqual(JSON.parse(f.calls[1].json).workflow.extra.rvk_runtime, { entry_node_id: "7", session_id: "active-session" });
  assert.equal(f.calls[1].args[1].output, prompt.output);
  assert.deepEqual(f.requests.map((request) => request.path), ["/rvk/wan-loop/inspect"]);
});
