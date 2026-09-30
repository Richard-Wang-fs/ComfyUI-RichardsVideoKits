import assert from "node:assert/strict";
import test from "node:test";
import { WanLoopFrontendController, entryScope, finalizeOnlyPrompt, formatStatus, migrateLegacyWorkflow } from "../../web/wan_loop_core.mjs";

function entry(promptId = "p1", sessionId = "s1", segment = 0, workflow = "a") {
  return { prompt_id: promptId, output: { rvk_wan_entry: [{ version: 3, kind: "entry", session_id: sessionId, workflow_id: workflow, entry_node_id: "7", segment_index: segment, completed_segments: segment, completed_frames: segment ? 9 : 0, total_frames: 20, total_segments: 3 }] } };
}
function advance(promptId = "p1", sessionId = "s1", more = true, workflow = "a") {
  return { prompt_id: promptId, output: { rvk_wan_loop: [{ version: 3, kind: "advance", session_id: sessionId, workflow_id: workflow, entry_node_id: "7", completed_segment: 0, completed_segments: 1, completed_frames: 9, total_frames: 20, next_segment: 1, has_more: more, path: "segment_0000.mp4" }] } };
}
function fixture(options = {}) {
  const controls = [], queued = [], notices = [], updates = [];
  let active = "a";
  const controller = new WanLoopFrontendController({
    isEntryActive: (workflow, node) => workflow === active && node === "7",
    setEntryStatus: (workflow, node, value) => { if (workflow === active) updates.push(value); },
    controlRun: async (session, action) => controls.push([session, action]),
    queuePrompt: async (workflowId, entryNodeId, sessionId) => {
      queued.push([workflowId, entryNodeId, sessionId]);
      const next = controller.beginSubmission({ workflowId, entryNodeId, sessionId });
      controller.confirmSubmission(next, "p2");
      return "p2";
    },
    notify: (...args) => notices.push(args),
    ...options,
  });
  const begin = (confirmed = true, extra = {}) => {
    const scope = controller.beginSubmission({ workflowId: "a", entryNodeId: "7", inspection: { completed_segments: 0, completed_frames: 0 }, ...extra });
    if (confirmed) controller.confirmSubmission(scope, "p1");
    return scope;
  };
  return { controller, controls, queued, notices, updates, begin, switchWorkflow: () => { active = "b"; } };
}

test("private session queues exactly once after matching advance and whole-prompt success", async () => {
  const f = fixture(); f.begin();
  assert.equal(await f.controller.handleExecuted(entry()), true);
  await f.controller.handleExecuted(advance());
  assert.equal(f.queued.length, 0);
  assert.equal(await f.controller.handleSuccess({ prompt_id: "p1" }), true);
  assert.deepEqual(f.queued, [["a", "7", "s1"]]);
  assert.equal(await f.controller.handleSuccess({ prompt_id: "p1" }), false);
  assert.equal(await f.controller.handleExecuted(entry("p2", "s1", 1)), true);
  assert.equal(f.controller.prompts.get("p2").sessionId, "s1");
});

test("foreign session and workflow advances cannot schedule another segment", async () => {
  const f = fixture(); f.begin(); await f.controller.handleExecuted(entry());
  assert.equal(await f.controller.handleExecuted(advance("p1", "other")), false);
  assert.equal(await f.controller.handleExecuted(advance("p1", "s1", true, "b")), false);
  await f.controller.handleSuccess({ prompt_id: "p1" });
  assert.equal(f.queued.length, 0);
  assert.deepEqual(f.controls, [["s1", "stop"]]);
});

test("completion releases active tracking without queueing", async () => {
  const f = fixture(); f.begin(); await f.controller.handleExecuted(entry());
  await f.controller.handleExecuted(advance("p1", "s1", false));
  await f.controller.handleSuccess({ prompt_id: "p1" });
  assert.equal(f.controller.sessions.size, 0);
  assert.equal(f.controller.submissions.size, 0);
  assert.equal(f.controller.prompts.size, 0);
  assert.equal(f.updates.at(-1).state, "completed");
  assert.equal(f.queued.length, 0);
});

test("Stop prevents late success and entry events from queueing", async () => {
  const f = fixture(); f.begin(); await f.controller.handleExecuted(entry());
  await f.controller.handleExecuted(advance());
  assert.equal(await f.controller.stopEntry("a", "7"), true);
  await f.controller.handleSuccess({ prompt_id: "p1" });
  assert.equal(await f.controller.handleExecuted(entry()), false);
  assert.deepEqual(f.controls, [["s1", "stop"]]);
  assert.equal(f.queued.length, 0);
  assert.equal(f.updates.at(-1).state, "stopped");
  f.begin(); // A later manual resume is admitted; the backend still checks its directory lease.
});

test("Stop before Entry keeps the eventual prompt tracked through its last saved count", async () => {
  const f = fixture(); f.begin(); await f.controller.stopEntry("a", "7");
  assert.equal(await f.controller.handleExecuted(entry()), true);
  assert.deepEqual(f.controls, [["s1", "stop"]]);
  assert.equal(f.controller.prompts.has("p1"), true);
  await f.controller.handleExecuted(advance());
  assert.equal(f.updates.at(-1).completed_segments, 1);
  assert.equal(f.updates.at(-1).state, "stopped");
  await f.controller.handleSuccess({ prompt_id: "p1" });
  assert.equal(f.controller.prompts.size, 0);
  assert.equal(f.controller.sessions.size, 0);
  assert.deepEqual(f.queued, []);
});

test("Stop during generation still reports a later Save failure", async () => {
  const f = fixture(); f.begin(); await f.controller.handleExecuted(entry("p1", "s1", 2));
  await f.controller.stopEntry("a", "7");
  await f.controller.handleFailure({ prompt_id: "p1", exception_message: "Save failed after stop" });
  assert.equal(f.updates.at(-1).state, "error");
  assert.equal(f.updates.at(-1).completed_segments, 2);
  assert.match(f.updates.at(-1).message, /Save failed after stop/);
  assert.deepEqual(f.controls, [["s1", "stop"], ["s1", "abort"]]);
});

test("failure keeps saved progress and directs the user to resume", async () => {
  const f = fixture(); f.begin(); await f.controller.handleExecuted(entry()); await f.controller.handleExecuted(advance());
  await f.controller.handleFailure({ prompt_id: "p1", exception_message: "disk full" });
  assert.equal(f.updates.at(-1).completed_segments, 1);
  assert.equal(f.updates.at(-1).completed_frames, 9);
  assert.match(f.updates.at(-1).message, /disk full.*resume/);
  assert.deepEqual(f.controls, [["s1", "abort"]]);
  assert.equal(f.controller.submissions.size, 0);
});

test("switching workflow before Entry aborts without writing the other canvas", async () => {
  const f = fixture(); f.begin(); const count = f.updates.length; f.switchWorkflow();
  assert.equal(await f.controller.handleExecuted(entry()), false);
  assert.equal(f.updates.length, count);
  assert.deepEqual(f.controls, [["s1", "stop"]]);
});

test("switching before success stops automatic continuation", async () => {
  const f = fixture(); f.begin(); await f.controller.handleExecuted(entry()); await f.controller.handleExecuted(advance()); f.switchWorkflow();
  assert.equal(await f.controller.handleSuccess({ prompt_id: "p1" }), false);
  assert.equal(f.queued.length, 0);
  assert.deepEqual(f.controls, [["s1", "stop"]]);
});

test("unadmitted events and duplicate submissions cannot claim the canvas", async () => {
  const f = fixture();
  assert.equal(await f.controller.handleExecuted(entry()), false);
  f.begin();
  assert.throws(() => f.begin(), /已有/);
  assert.equal(await f.controller.handleExecuted(entry("foreign")), false);
  assert.equal(await f.controller.handleExecuted(entry()), true);
});

test("continuation checks segment number and confirmed prompt ID", async () => {
  const f = fixture(); f.begin(); await f.controller.handleExecuted(entry()); await f.controller.handleExecuted(advance()); await f.controller.handleSuccess({ prompt_id: "p1" });
  assert.equal(await f.controller.handleExecuted(entry("foreign", "s1", 1)), false);
  assert.equal(await f.controller.handleExecuted(entry("p2", "s1", 3)), false);
  assert.deepEqual(f.controls, [["s1", "stop"]]);
  assert.equal(f.controller.sessions.size, 0);
});

for (const [name, queuePrompt] of [["false", async () => false], ["rejection", async () => { throw new Error("offline"); }], ["timeout", async () => new Promise(() => {})]]) {
  test(`queue ${name} aborts instead of claiming continuation`, async () => {
    const f = fixture({ queuePrompt, queueTimeoutMs: 5 }); f.begin(); await f.controller.handleExecuted(entry()); await f.controller.handleExecuted(advance());
    assert.equal(await f.controller.handleSuccess({ prompt_id: "p1" }), false);
    assert.deepEqual(f.controls, [["s1", "stop"]]);
    assert.equal(f.controller.sessions.size, 0);
  });
}

test("success arriving before the API response waits for confirmation", async () => {
  const f = fixture(); const scope = f.begin(false); await f.controller.handleExecuted(entry()); await f.controller.handleExecuted(advance());
  await f.controller.handleSuccess({ prompt_id: "p1" }); assert.equal(f.queued.length, 0);
  await f.controller.confirmSubmission(scope, "p1");
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(f.queued.length, 1);
});

test("pre-Entry error arriving before submission response is retained", async () => {
  const f = fixture(); const scope = f.begin(false);
  await f.controller.handleFailure({ prompt_id: "p1", exception_message: "invalid source" });
  await f.controller.confirmSubmission(scope, "p1");
  assert.equal(f.controller.submissions.size, 0);
  assert.equal(f.updates.at(-1).state, "error");
  assert.match(f.updates.at(-1).message, /invalid source/);
});

test("completed-only prompt can finish before the API response", async () => {
  const f = fixture(); const scope = f.begin(false, { finalizing: true, inspection: { completed_segments: 3 } });
  await f.controller.handleSuccess({ prompt_id: "p1" });
  await f.controller.confirmSubmission(scope, "p1");
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(f.updates.at(-1).state, "completed");
  assert.equal(f.controller.submissions.size, 0);
  assert.equal(f.queued.length, 0);
});

test("executing Save/Finalize changes phase without losing progress", async () => {
  const f = fixture(); f.begin(true, { output: { "8": { class_type: "RVKSaveSegmentVideo" }, "9": { class_type: "RVKFinalizeSegments" } } });
  await f.controller.handleExecuted(entry());
  f.controller.handleExecuting({ prompt_id: "p1", node: "8" }); assert.equal(f.updates.at(-1).state, "saving");
  f.controller.handleExecuting({ prompt_id: "p1", node: "9" }); assert.equal(f.updates.at(-1).state, "finalizing");
  assert.equal(f.updates.at(-1).completed_segments, 0);
});

test("blocked sessions expire and tracking remains bounded", () => {
  let now = 0; const callbacks = new Map(); let index = 0;
  const f = fixture({ trackingLimit: 3, blockedSessionTtlMs: 10, now: () => now, setTimer: (callback) => { callbacks.set(++index, callback); return index; }, clearTimer: (id) => callbacks.delete(id) });
  for (let i = 0; i < 8; i++) f.controller.blockSession(`s${i}`);
  assert.equal(f.controller.blocked.size, 3); assert.equal(callbacks.size, 3);
  now = 11; assert.equal(f.controller.isBlocked("s7"), false); assert.equal(callbacks.size, 0);
  f.controller.blockSession("timer"); [...callbacks.values()][0](); assert.equal(f.controller.blocked.size, 0);
});

test("status shows current and historical filenames without inventing provenance", () => {
  const value = formatStatus({ state: "error", completed_segments: 2, total_segments: 4, reference_video: "new.mp4", reference_image: "new.png", recorded_video: "old.mp4", session_id: "secret" });
  assert.match(value, /历史视频：old.mp4/); assert.match(value, /历史参考图：未知/); assert.match(value, /已完成 2 \/ 总计 4/); assert.doesNotMatch(value, /secret/);
  const completed = formatStatus({ state: "completed", segment_index: 3, completed_segments: 3, total_segments: 3 });
  assert.match(completed, /当前段：全部完成/);
  assert.doesNotMatch(completed, /当前段：4/);
});

test("reconnect discards stale local admission without backend writes or late requeue", async () => {
  const f = fixture(); f.begin(); await f.controller.handleExecuted(entry()); await f.controller.handleExecuted(advance());
  f.controller.discardDisconnectedTracking();
  assert.equal(f.controller.sessions.size, 0); assert.equal(f.controller.submissions.size, 0);
  await f.controller.handleSuccess({ prompt_id: "p1" });
  assert.equal(await f.controller.handleExecuted(entry()), false);
  assert.deepEqual(f.controls, []); assert.deepEqual(f.queued, []);
  f.begin();
});

export function samplePrompt() {
  return { workflow: { id: "a", extra: {} }, output: {
    "1": { class_type: "LoadVideo", inputs: { file: "drive.mp4" } },
    "7": { class_type: "RVKWanLoopEntry", inputs: { drive_video: ["1", 0], mode: "resume", output_directory: "work", segment_length: 9 } },
    "8": { class_type: "KSampler", inputs: { latent_image: ["7", 0] } },
    "9": { class_type: "RVKSaveSegmentVideo", inputs: { images: ["8", 0] } },
    "10": { class_type: "RVKWanAdvance", inputs: { plan: ["7", 4], receipt: ["9", 0] } },
    "11": { class_type: "RVKFinalizeSegments", inputs: { source_video: ["1", 0], output_directory: ["7", 9], loop_status: ["10", 0], destination_directory: "finished", final_filename: "final.mp4" } },
  } };
}

test("complete resume submits only Finalize and LoadVideo, preserving canvas and destination", () => {
  const prompt = samplePrompt(); const original = structuredClone(prompt);
  const pruned = finalizeOnlyPrompt(prompt, "7", "work");
  assert.deepEqual(Object.keys(pruned.output), ["1", "11"]);
  assert.equal(pruned.output["11"].inputs.output_directory, "work");
  assert.equal(pruned.output["11"].inputs.destination_directory, "finished");
  assert.equal(pruned.output["11"].inputs.final_filename, "final.mp4");
  assert.equal(pruned.output["11"].inputs.loop_status, undefined);
  assert.deepEqual(prompt, original);
});

test("complete resume refuses ambiguous or generation-dependent Finalize routes", () => {
  const prompt = samplePrompt(); prompt.output["11"].inputs.source_video = ["8", 0];
  assert.throws(() => finalizeOnlyPrompt(prompt, "7", "work"), /上游/);
  delete prompt.output["11"]; assert.throws(() => finalizeOnlyPrompt(prompt, "7", "work"), /唯一/);
});

test("legacy migration replaces token widget and slot 5 links, preserving other links", () => {
  const outputs = Array.from({ length: 10 }, (_, i) => ({ name: i === 5 ? "run_token" : `out${i}`, links: i === 5 ? [22] : i === 9 ? [23] : [] }));
  const workflow = { nodes: [
    { id: 7, type: "RVKWanLoopEntry", widgets_values: [81, "old-work", "legacy-token"], inputs: [{ name: "run_token", link: 21 }], outputs },
    { id: 8, type: "Any", inputs: [{ name: "token", link: 22 }, { name: "directory", link: 23 }], outputs: [{ links: [21] }] },
  ], links: [[21, 8, 0, 7, 3, "STRING"], [22, 7, 5, 8, 0, "STRING"], [23, 7, 9, 8, 1, "STRING"]] };
  assert.deepEqual(migrateLegacyWorkflow(workflow), { migrated: 1, removedLinks: 2 });
  assert.deepEqual(workflow.nodes[0].widgets_values, [81, "old-work", "new"]);
  assert.equal(workflow.nodes[0].outputs[5].name, "status_text");
  assert.deepEqual(workflow.links.map((link) => link[0]), [23]);
  assert.equal(workflow.nodes[1].inputs[0].link, null); assert.equal(workflow.nodes[1].inputs[1].link, 23);
  assert.deepEqual(migrateLegacyWorkflow(workflow), { migrated: 0, removedLinks: 0 });
});

test("scope rejects multiple and nested entries before admission", () => {
  const prompt = samplePrompt(); assert.equal(entryScope(prompt).entryNodeId, "7");
  prompt.output["12"] = prompt.output["7"]; assert.throws(() => entryScope(prompt), /一个/);
  delete prompt.output["7"]; prompt.output["12:7"] = prompt.output["12"]; delete prompt.output["12"]; assert.throws(() => entryScope(prompt), /顶层/);
});



test("early foreign events never bind or control a session before the response prompt ID", async () => {
  const f = fixture(); const scope = f.begin(false);
  await f.controller.handleExecuted(entry("old-prompt", "foreign-session"));
  await f.controller.handleExecuted(advance("old-prompt", "foreign-session"));
  await f.controller.handleSuccess({ prompt_id: "old-prompt" });
  assert.equal(scope.sessionId, null);
  assert.equal(f.controller.sessions.size, 0);
  assert.deepEqual(f.controls, []);
  await f.controller.handleExecuted(entry("p1", "s1"));
  assert.equal(scope.sessionId, null);
  await f.controller.confirmSubmission(scope, "p1");
  assert.equal(scope.sessionId, "s1");
  assert.deepEqual(f.controls, []);
  assert.deepEqual(f.queued, []);
  assert.equal(f.controller.earlyEvents.size, 0);
});

test("many irrelevant early node events cannot evict matching Entry and Advance", async () => {
  const f = fixture(); const scope = f.begin(false);
  for (let i = 0; i < 40; i++) {
    f.controller.handleExecuting({ prompt_id: "p1", node: String(i) });
    await f.controller.handleExecuted({ prompt_id: "p1", node: String(i), output: { images: [] } });
  }
  await f.controller.handleExecuted(entry()); await f.controller.handleExecuted(advance());
  await f.controller.handleSuccess({ prompt_id: "p1" });
  await f.controller.confirmSubmission(scope, "p1");
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(f.queued.length, 1);
  assert.deepEqual(f.controls, []);
});
