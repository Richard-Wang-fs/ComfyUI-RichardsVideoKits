import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const adapter = await readFile(new URL("../../web/wan_loop.js", import.meta.url), "utf8");
const core = await readFile(new URL("../../web/wan_loop_core.mjs", import.meta.url));
const coreHash = createHash("sha256").update(core.toString("utf8").replaceAll("\r\n", "\n")).digest("hex");

test("shipping core import carries its current content hash", () => {
  const dependency = adapter.match(/from\s+"(\.\/wan_loop_core\.mjs[^\"]*)"/);
  assert.ok(dependency, "the real relative core import must remain present");
  assert.equal(new URL(dependency[1], "https://fixture.invalid/").searchParams.get("v"), coreHash.slice(0, 16));
});

test("native ESM loading bypasses an already-cached old core and registers a working loop", async (t) => {
  const taskRoot = fileURLToPath(new URL("../../.local/r3/r304_frontend_loading_20260930/", import.meta.url));
  await mkdir(taskRoot, { recursive: true });
  const directory = await mkdtemp(path.join(taskRoot, "esm-loading-"));
  t.after(async () => {
    const resolved = path.resolve(directory);
    assert.equal(path.dirname(resolved), path.resolve(taskRoot));
    assert.match(path.basename(resolved), /^esm-loading-/);
    await rm(resolved, { recursive: true });
  });

  const corePath = path.join(directory, "wan_loop_core.mjs");
  await writeFile(corePath, "export function firstPayload() {}\nexport class WanLoopFrontendController {}\n");
  const staleCore = await import(pathToFileURL(corePath).href);
  assert.equal(staleCore.entryScope, undefined);
  await writeFile(corePath, core);

  const listeners = new Map(), submitted = [], requests = [];
  const prompt = { workflow: { id: "native-esm", extra: {} }, output: {
    "1": { class_type: "LoadVideo", inputs: { file: "drive.mp4" } },
    "7": { class_type: "RVKWanLoopEntry", inputs: { drive_video: ["1", 0], mode: "new", output_directory: "work", segment_length: 9 } },
    "9": { class_type: "RVKSaveSegmentVideo", inputs: { images: ["7", 0] } },
    "10": { class_type: "RVKWanAdvance", inputs: { plan: ["7", 4], receipt: ["9", 0] } },
  } };
  const node = {
    id: 7, comfyClass: "RVKWanLoopEntry", widgets: [],
    addWidget(type, name, value, callback) { const widget = { type, name, value, callback }; this.widgets.push(widget); return widget; },
    addDOMWidget(name, type, element, options) {
      const widget = { name, type, element, options };
      Object.defineProperty(widget, "value", { get: options.getValue, set: options.setValue });
      this.widgets.push(widget); return widget;
    },
  };
  let extension;
  const api = {
    addEventListener: (name, callback) => listeners.set(name, callback),
    fetchApi: async (url) => {
      requests.push(url);
      return { ok: true, json: async () => ({ ok: true, can_run: true, completed: false, completed_segments: 0, total_segments: 2, reference_video: "drive.mp4" }) };
    },
    queuePrompt: async (_number, queued) => {
      submitted.push(structuredClone(queued));
      return { prompt_id: `p${submitted.length}`, node_errors: {} };
    },
  };
  const app = {
    rootGraph: { serialize: () => prompt.workflow, _nodes: [node], getNodeById: (id) => String(id) === "7" ? node : null, setDirtyCanvas() {} },
    registerExtension(value) { extension = value; },
    graphToPrompt: async () => structuredClone(prompt),
    queuePrompt: async () => { await api.queuePrompt(0, structuredClone(prompt)); return true; },
  };
  const fixtureKey = `__rvk_${path.basename(directory)}`;
  globalThis[fixtureKey] = { app, api };
  const previousDocument = globalThis.document;
  globalThis.document = { createElement: () => ({ value: "", style: {}, setAttribute() {} }) };
  t.after(() => {
    delete globalThis[fixtureKey];
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  });
  await writeFile(path.join(directory, "app.mjs"), `export const app = globalThis[${JSON.stringify(fixtureKey)}].app;\n`);
  await writeFile(path.join(directory, "api.mjs"), `export const api = globalThis[${JSON.stringify(fixtureKey)}].api;\n`);
  // Only ComfyUI's absolute public shims need relocation for Node. Keep all
  // imports, including the shipping relative core URL, for native ESM linking.
  const loadableAdapter = adapter.replace('"/scripts/app.js"', '"./app.mjs"').replace('"/scripts/api.js"', '"./api.mjs"');
  const unversionedPath = path.join(directory, "wan_loop_unversioned.mjs");
  await writeFile(unversionedPath, loadableAdapter.replace(/\.\/wan_loop_core\.mjs\?[^"\s]+/, "./wan_loop_core.mjs"));
  await assert.rejects(import(pathToFileURL(unversionedPath).href), /does not provide an export named 'entryScope'/);
  assert.equal(extension, undefined, "a stale dependency prevents all extension registration");
  const adapterPath = path.join(directory, "wan_loop.mjs");
  await writeFile(adapterPath, loadableAdapter);
  await import(pathToFileURL(adapterPath).href);
  assert.equal(extension?.name, "richards-video-kits.wan-loop");
  await extension.setup();
  await extension.nodeCreated(node);
  assert.ok(node.__rvkStatusWidget);
  assert.equal(node.widgets.filter((widget) => widget.type === "button").length, 2);

  await app.queuePrompt();
  const emit = async (name, detail) => { listeners.get(name)({ detail }); await new Promise((resolve) => setImmediate(resolve)); };
  const identity = { version: 3, workflow_id: "native-esm", entry_node_id: "7", session_id: "private-session" };
  await emit("executed", { prompt_id: "p1", output: { rvk_wan_entry: [{ ...identity, kind: "entry", segment_index: 0 }] } });
  await emit("executed", { prompt_id: "p1", output: { rvk_wan_loop: [{ ...identity, kind: "advance", has_more: true, stopped: false, completed_segments: 1, next_segment: 1 }] } });
  await emit("execution_success", { prompt_id: "p1" });
  assert.equal(submitted.length, 2);
  assert.deepEqual(submitted[1].workflow.extra.rvk_runtime, { entry_node_id: "7", session_id: "private-session" });
  assert.deepEqual(requests, ["/rvk/wan-loop/inspect"]);
});
