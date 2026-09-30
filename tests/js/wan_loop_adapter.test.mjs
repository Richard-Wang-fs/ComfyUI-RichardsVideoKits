import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";
import * as core from "../../web/wan_loop_core.mjs";

const adapter = await readFile(new URL("../../web/wan_loop.js", import.meta.url), "utf8");
const source = adapter.replace(/import\s+[\s\S]*?\s+from\s+"[^"]+";\s*/g, "");
assert.doesNotMatch(source, /^import /m);

function graph(mode = "new") {
  return { workflow: { id: "a", extra: {} }, output: {
    "1": { class_type: "LoadVideo", inputs: { file: "drive.mp4" } },
    "7": { class_type: "RVKWanLoopEntry", inputs: { drive_video: ["1", 0], mode, output_directory: "work", segment_length: 9 } },
    "8": { class_type: "KSampler", inputs: { latent_image: ["7", 0] } },
    "9": { class_type: "RVKSaveSegmentVideo", inputs: { images: ["8", 0] } },
    "10": { class_type: "RVKWanAdvance", inputs: { plan: ["7", 4], receipt: ["9", 0] } },
    "11": { class_type: "RVKFinalizeSegments", inputs: { source_video: ["1", 0], output_directory: ["7", 9], loop_status: ["10", 0], destination_directory: "finished", final_filename: "final.mp4" } },
  } };
}
function entry(promptId = "p1", segment = 0) {
  return { prompt_id: promptId, output: { rvk_wan_entry: [{ version: 3, kind: "entry", session_id: "private-session", workflow_id: "a", entry_node_id: "7", segment_index: segment, completed_segments: segment, total_segments: 3, completed_frames: segment ? 9 : 0, total_frames: 20, reference_video: "drive.mp4", reference_image: "ref.png" }] } };
}
function advance(more = true) {
  return { prompt_id: "p1", output: { rvk_wan_loop: [{ version: 3, kind: "advance", session_id: "private-session", workflow_id: "a", entry_node_id: "7", next_segment: 1, completed_segment: 0, completed_segments: 1, completed_frames: 9, total_frames: 20, has_more: more, stopped: false, path: "segment_0000.mp4" }] } };
}
async function fixture({ mode = "new", inspection = {}, prepared = {}, beforeResponse, inspectHook } = {}) {
  let current = graph(mode);
  const submitted = [], requests = [], notices = [], listeners = new Map();
  let extension;
  const node = {
    id: 7, comfyClass: "RVKWanLoopEntry", widgets: [{ name: "segment_length", value: 9 }, { name: "output_directory", value: "work" }, { name: "mode", value: mode }],
    addWidget(type, name, value, callback) { const widget = { type, name, value, callback, options: {} }; this.widgets.push(widget); return widget; },
    addDOMWidget(name, type, element, options) { const widget = { name, type, element, options }; Object.defineProperty(widget, "value", { get: options.getValue, set: options.setValue }); this.widgets.push(widget); return widget; },
  };
  const state = { ok: true, workflow_id: "a", entry_node_id: "7", completed: false, can_run: true, completed_segments: 0, total_segments: 3, completed_frames: 0, total_frames: 20, reference_video: "drive.mp4", reference_image: "ref.png", recorded_video: null, recorded_image: null, output_directory: "work", partials: [], ...inspection };
  const api = {
    addEventListener: (name, callback) => listeners.set(name, callback),
    fetchApi: async (path, options) => {
      requests.push({ path, body: JSON.parse(options.body) });
      let result = { ok: true };
      if (path === "/rvk/wan-loop/inspect") result = inspectHook ? await inspectHook() : state;
      else if (path === "/rvk/wan-loop/prepare-complete") result = { ...state, partials: [], ...prepared };
      return { ok: result.ok !== false, status: result.ok === false ? 400 : 200, json: async () => structuredClone(result) };
    },
    queuePrompt: async (_number, prompt) => {
      submitted.push(structuredClone(prompt));
      const id = `p${submitted.length}`;
      if (beforeResponse) await beforeResponse({ id, listeners, node });
      return { prompt_id: id, number: submitted.length, node_errors: {} };
    },
  };
  const app = {
    rootGraph: { serialize: () => current.workflow, _nodes: [node], getNodeById: (id) => String(id) === "7" ? node : null, setDirtyCanvas: () => {} },
    registerExtension: (value) => { extension = value; },
    extensionManager: { toast: { add: (value) => notices.push(value) } },
    graphToPrompt: async () => structuredClone(current),
    queuePrompt: async () => { try { await api.queuePrompt(0, structuredClone(current)); return true; } catch { return false; } },
  };
  const document = { createElement: () => ({ value: "", style: {}, setAttribute() {} }) };
  vm.runInNewContext(source, { app, api, document, structuredClone, ...core });
  await extension.setup(); await extension.nodeCreated(node);
  const emit = async (name, detail) => { listeners.get(name)?.({ detail }); await new Promise((resolve) => setImmediate(resolve)); };
  return { node, submitted, requests, notices, extension, emit, api, state, get current() { return current; }, setCurrent: (value) => { current = value; }, submit: () => api.queuePrompt(0, structuredClone(current)) };
}

test("shipping adapter queues new without any user or serialized session token", async () => {
  const f = await fixture(); f.current.workflow.extra.rvk_runtime = { session_id: "stale", entry_node_id: "7" };
  await f.submit(); await f.emit("executed", entry());
  assert.equal(f.submitted[0].workflow.extra.rvk_runtime, undefined);
  assert.equal(f.submitted[0].output["7"].inputs.run_token, undefined);
  assert.equal(f.node.widgets.some((widget) => widget.name === "run_token"), false);
  assert.match(f.node.__rvkStatusWidget.value, /生成中/);
  assert.doesNotMatch(f.node.__rvkStatusWidget.value, /private-session/);
});

test("confirmed continuation injects private metadata once and bypasses new admission inspect", async () => {
  const f = await fixture(); await f.submit(); await f.emit("executed", entry()); await f.emit("executed", advance()); await f.emit("execution_success", { prompt_id: "p1" });
  assert.equal(f.submitted.length, 2);
  assert.deepEqual(f.submitted[1].workflow.extra.rvk_runtime, { entry_node_id: "7", session_id: "private-session" });
  assert.equal(f.submitted[1].output["7"].inputs.mode, "new");
  assert.equal(f.current.workflow.extra.rvk_runtime, undefined);
  assert.equal(f.requests.filter((request) => request.path.endsWith("/inspect")).length, 1);
  await f.emit("executed", entry("p2", 1));
  assert.match(f.node.__rvkStatusWidget.value, /已完成 1/);
});

test("check button is read-only even with complete videos and partial leftovers", async () => {
  const f = await fixture({ mode: "resume", inspection: { completed: true, completed_segments: 3, partials: ["segment_0003.partial.mp4"] } });
  f.node.widgets.find((widget) => widget.name === "检查/刷新状态").callback();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(f.requests.map((request) => request.path), ["/rvk/wan-loop/inspect"]);
  assert.equal(f.submitted.length, 0);
  assert.match(f.node.__rvkStatusWidget.value, /未完成文件/);
  assert.equal(f.node.__rvkStatusWidget.element.readOnly, true);
  for (const widget of f.node.widgets.slice(3)) assert.equal(widget.serialize, false);
});

test("complete resume prepares once then submits only independent Finalize and video source", async () => {
  const f = await fixture({ mode: "resume", inspection: { completed: true, can_run: false, completed_segments: 3, completed_frames: 20, partials: ["segment_0003.partial.mp4"] } });
  await f.submit();
  assert.deepEqual(f.requests.map((request) => request.path), ["/rvk/wan-loop/inspect", "/rvk/wan-loop/prepare-complete"]);
  assert.deepEqual(Object.keys(f.submitted[0].output), ["1", "11"]);
  const inputs = f.submitted[0].output["11"].inputs;
  assert.equal(inputs.output_directory, "work"); assert.equal(inputs.loop_status, undefined);
  assert.equal(inputs.destination_directory, "finished"); assert.equal(inputs.final_filename, "final.mp4");
  assert.ok(f.current.output["8"]); assert.deepEqual(f.current.output["11"].inputs.loop_status, ["10", 0]);
  await f.emit("execution_success", { prompt_id: "p1" });
  assert.match(f.node.__rvkStatusWidget.value, /已完成/); assert.equal(f.submitted.length, 1);
});

test("changed completion preflight cannot submit a model or stale finalize graph", async () => {
  const f = await fixture({ mode: "resume", inspection: { completed: true }, prepared: { completed: false } });
  await assert.rejects(f.submit(), /状态已变化/);
  assert.equal(f.submitted.length, 0);
});

test("source inspection failure shows a diagnostic without queueing", async () => {
  const f = await fixture({ inspection: { ok: false, message: "source not statically resolvable", completed_segments: 2 } });
  await assert.rejects(f.submit(), /not statically/);
  assert.equal(f.submitted.length, 0);
  assert.match(f.node.__rvkStatusWidget.value, /已完成 2/);
});

test("saving phase and failed Finalize retain completed segment count", async () => {
  const f = await fixture(); await f.submit(); await f.emit("executed", entry());
  await f.emit("executing", { prompt_id: "p1", node: "9" }); assert.match(f.node.__rvkStatusWidget.value, /保存中/);
  await f.emit("executed", advance(false));
  await f.emit("execution_error", { prompt_id: "p1", exception_message: "audio missing" });
  assert.match(f.node.__rvkStatusWidget.value, /已完成 1/);
  assert.match(f.node.__rvkStatusWidget.value, /audio missing.*resume/);
  const control = f.requests.find((request) => request.path.endsWith("/control"));
  assert.deepEqual(control.body, { session_id: "private-session", action: "abort" });
  assert.equal(f.node.widgets.find((widget) => widget.name === "mode").value, "new");
});

test("workflow switch stops in-flight session and never paints the foreign canvas", async () => {
  const f = await fixture(); await f.submit(); const text = f.node.__rvkStatusWidget.value;
  const changed = graph(); changed.workflow.id = "b"; f.setCurrent(changed);
  await f.emit("executed", entry());
  assert.equal(f.node.__rvkStatusWidget.value, text);
  assert.deepEqual(f.requests.find((request) => request.path.endsWith("/control")).body, { session_id: "private-session", action: "stop" });
});

test("initial validation error before queue response is shown and can be retried", async () => {
  const f = await fixture({ beforeResponse: async ({ id, listeners }) => {
    listeners.get("execution_error")({ detail: { prompt_id: id, exception_message: "entry validation failed" } });
  } });
  await f.submit();
  assert.match(f.node.__rvkStatusWidget.value, /entry validation failed/);
  await f.submit(); assert.equal(f.submitted.length, 2);
});

test("duplicate queue while inspecting is rejected before another inspect or write", async () => {
  let finish;
  const f = await fixture({ inspectHook: () => new Promise((resolve) => { finish = resolve; }) });
  const first = f.submit();
  await assert.rejects(f.submit(), /已有/);
  assert.equal(f.requests.length, 1);
  finish({ ok: true, can_run: true, completed: false, completed_segments: 0 });
  await first; assert.equal(f.submitted.length, 1);
});

test("graph load and reconnect refresh status using only the inspection route", async () => {
  const f = await fixture({ inspection: { recorded_video: "old.mp4", recorded_image: "old.png", completed_segments: 1 } });
  await f.extension.afterConfigureGraph(); await f.emit("reconnected");
  assert.equal(f.requests.length, 2); assert.ok(f.requests.every((request) => request.path.endsWith("/inspect")));
  assert.match(f.node.__rvkStatusWidget.value, /历史视频：old.mp4/);
  assert.match(f.node.__rvkStatusWidget.value, /当前视频：drive.mp4/);
  assert.equal(f.submitted.length, 0);
});

test("legacy graph migration warns and removes saved runtime metadata", async () => {
  const f = await fixture();
  const workflow = { extra: { rvk_runtime: { session_id: "old" } }, nodes: [{ type: "RVKWanLoopEntry", widgets_values: [81, "old-dir", "old-token"], inputs: [{ name: "run_token", link: null }], outputs: Array.from({ length: 6 }, (_, i) => ({ name: i === 5 ? "run_token" : `out${i}`, links: [] })) }], links: [] };
  await f.extension.beforeConfigureGraph(workflow);
  assert.equal(workflow.extra.rvk_runtime, undefined);
  assert.deepEqual(workflow.nodes[0].widgets_values, [81, "old-dir", "new"]);
  assert.match(f.notices.at(-1).detail, /迁移.*resume/);
});

test("reconnect releases stale browser admission and permits a manual resume", async () => {
  const f = await fixture(); await f.submit(); await f.emit("executed", entry());
  await f.emit("reconnected");
  await f.emit("executed", advance()); await f.emit("execution_success", { prompt_id: "p1" });
  assert.equal(f.submitted.length, 1);
  assert.equal(f.requests.some((request) => request.path.endsWith("/control")), false);
  f.current.output["7"].inputs.mode = "resume";
  await f.submit(); assert.equal(f.submitted.length, 2);
  assert.equal(f.submitted[1].workflow.extra.rvk_runtime, undefined);
});

test("foreign early Entry cannot claim or stop the actual API submission", async () => {
  const f = await fixture({ beforeResponse: async ({ listeners, node }) => {
    const foreign = entry("old-prompt"); foreign.output.rvk_wan_entry[0].session_id = "foreign-session";
    listeners.get("executed")({ detail: foreign });
    listeners.get("executed")({ detail: entry("p1") });
    assert.match(node.__rvkStatusWidget.value, /已排队/);
  } });
  await f.submit();
  assert.match(f.node.__rvkStatusWidget.value, /生成中/);
  assert.equal(f.requests.some((request) => request.path.endsWith("/control")), false);
  await f.emit("execution_error", { prompt_id: "p1", exception_message: "Save failed" });
  assert.deepEqual(f.requests.find((request) => request.path.endsWith("/control")).body, { session_id: "private-session", action: "abort" });
});

function assertUnknownInspection(text) {
  assert.match(text, /当前段：未知/);
  assert.match(text, /片段：已完成 未知 \/ 总计 未知/);
  assert.match(text, /帧数：已完成 未知 \/ 总计 未知/);
  for (const label of ["当前视频", "当前参考图", "历史视频", "历史参考图"]) assert.match(text, new RegExp(`${label}：未知`));
}

test("changed inputs and failed check clear old filenames and progress immediately", async () => {
  let result;
  const f = await fixture({ inspectHook: () => result });
  result = { ...f.state, completed: true, completed_segments: 3, recorded_video: "old.mp4", recorded_image: "old.png" };
  await f.extension.afterConfigureGraph();
  assert.match(f.node.__rvkStatusWidget.value, /历史视频：old.mp4/);
  f.current.output["1"].inputs.file = "changed.mp4";
  f.current.output["7"].inputs.output_directory = "changed-work";
  result = { ok: false, message: "changed source cannot be inspected" };
  const checking = f.extension.afterConfigureGraph();
  assert.match(f.node.__rvkStatusWidget.value, /检查中/);
  assertUnknownInspection(f.node.__rvkStatusWidget.value);
  await checking;
  assertUnknownInspection(f.node.__rvkStatusWidget.value);
  assert.match(f.node.__rvkStatusWidget.value, /changed source cannot be inspected/);
  assert.ok(f.requests.every((request) => request.path.endsWith("/inspect")));
});

test("a fresh submission clears a previous inspection before failed admission", async () => {
  let result;
  const f = await fixture({ inspectHook: () => result });
  result = { ...f.state, completed_segments: 2, recorded_video: "old.mp4", recorded_image: "old.png" };
  await f.extension.afterConfigureGraph();
  f.current.output["7"].inputs.output_directory = "changed-work";
  result = { ok: false, message: "new directory unavailable" };
  const submission = f.submit();
  assertUnknownInspection(f.node.__rvkStatusWidget.value);
  await assert.rejects(submission, /new directory unavailable/);
  assertUnknownInspection(f.node.__rvkStatusWidget.value);
  assert.equal(f.submitted.length, 0);
});

for (const oldOk of [true, false]) test(`late ${oldOk ? "success" : "error"} inspection cannot overwrite the latest check`, async () => {
  const pending = [];
  const f = await fixture({ inspectHook: () => new Promise((resolve) => pending.push(resolve)) });
  const first = f.extension.afterConfigureGraph();
  await new Promise((resolve) => setImmediate(resolve));
  f.current.output["1"].inputs.file = "latest.mp4";
  const second = f.extension.afterConfigureGraph();
  await new Promise((resolve) => setImmediate(resolve));
  pending[1]({ ...f.state, reference_video: "latest.mp4", completed_segments: 2 });
  await second;
  const latest = f.node.__rvkStatusWidget.value;
  pending[0]({ ...f.state, ok: oldOk, reference_video: "stale.mp4", message: "stale result" });
  await first;
  assert.equal(f.node.__rvkStatusWidget.value, latest);
  assert.match(latest, /当前视频：latest.mp4/);
  assert.match(latest, /已完成 2/);
});

test("refresh during an active prompt preserves submitted sources and progress", async () => {
  const f = await fixture();
  await f.submit(); await f.emit("executed", entry());
  const running = f.node.__rvkStatusWidget.value;
  f.current.output["1"].inputs.file = "edited-after-queue.mp4";
  f.state.reference_video = "edited-after-queue.mp4";
  f.state.completed_segments = 2;
  await f.extension.afterConfigureGraph();
  assert.equal(f.node.__rvkStatusWidget.value, running);
  assert.match(running, /生成中/);
  assert.equal(f.requests.length, 1);
});

test("public executing node IDs update only the prompt selected by execution_start", async () => {
  const f = await fixture();
  await f.submit(); await f.emit("executed", entry());
  await f.emit("executing", "9");
  assert.match(f.node.__rvkStatusWidget.value, /生成中/);
  await f.emit("execution_start", { prompt_id: "p1" });
  await f.emit("executing", "9");
  assert.match(f.node.__rvkStatusWidget.value, /保存中/);
  await f.emit("executing", null);
  assert.match(f.node.__rvkStatusWidget.value, /保存中/);
  await f.emit("execution_start", { prompt_id: "foreign-prompt" });
  await f.emit("executing", "11");
  assert.match(f.node.__rvkStatusWidget.value, /保存中/);
  await f.emit("execution_start", { prompt_id: "p1" });
  await f.emit("executing", 11);
  assert.match(f.node.__rvkStatusWidget.value, /合成中/);
  assert.equal(f.submitted.length, 1);
});

test("early public executing events wait for confirmation of their actual prompt ID", async () => {
  const f = await fixture({ beforeResponse: async ({ id, listeners, node }) => {
    listeners.get("execution_start")({ detail: { prompt_id: "foreign-prompt" } });
    listeners.get("executing")({ detail: "11" });
    listeners.get("execution_start")({ detail: { prompt_id: id } });
    listeners.get("executed")({ detail: entry(id) });
    listeners.get("executing")({ detail: "9" });
    assert.match(node.__rvkStatusWidget.value, /已排队/);
  } });
  await f.submit();
  assert.match(f.node.__rvkStatusWidget.value, /保存中/);
  assert.equal(f.requests.some((request) => request.path.endsWith("/control")), false);
});

test("terminal and reconnect events discard the public executing association", async () => {
  const f = await fixture();
  await f.submit(); await f.emit("executed", entry());
  await f.emit("execution_start", { prompt_id: "p1" });
  await f.emit("executed", advance(false));
  await f.emit("execution_success", { prompt_id: "p1" });
  const completed = f.node.__rvkStatusWidget.value;
  await f.emit("executing", "9");
  assert.equal(f.node.__rvkStatusWidget.value, completed);
  await f.emit("reconnected");
  const refreshed = f.node.__rvkStatusWidget.value;
  await f.emit("executing", "11");
  assert.equal(f.node.__rvkStatusWidget.value, refreshed);
});

test("status uses a dedicated DOM type whose mounted element receives live read-only updates", async () => {
  const f = await fixture();
  const widget = f.node.__rvkStatusWidget;
  // Frontend 1.53.6 maps customtext to a separate Vue textarea; an unknown
  // DOM type takes WidgetDOM's fallback and mounts this original element.
  assert.equal(widget.type, "rvk-status");
  const mountedElement = widget.element;
  assert.equal(mountedElement.readOnly, true);
  assert.match(mountedElement.value, /当前视频：未知/);
  await f.extension.afterConfigureGraph();
  assert.match(mountedElement.value, /当前视频：drive.mp4/);
  await f.submit(); await f.emit("executed", entry());
  await f.emit("execution_start", { prompt_id: "p1" });
  await f.emit("executing", "9");
  assert.match(mountedElement.value, /保存中/);
  assert.equal(mountedElement, widget.element);
  assert.equal(widget.serialize, false);
  assert.equal(widget.options.serialize, false);
});
