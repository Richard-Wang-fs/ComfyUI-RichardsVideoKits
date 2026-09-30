import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";
// ComfyUI sets no-store for .js, but not .mjs. Keep this LF-normalized source
// SHA-256 prefix in sync whenever the core body changes to avoid stale modules.
import {
  WanLoopFrontendController,
  entryScope,
  finalizeOnlyPrompt,
  formatStatus,
  migrateLegacyWorkflow,
} from "./wan_loop_core.mjs?v=53471d551522b040";

const ENTRY_CLASS = "RVKWanLoopEntry";
const statuses = new Map();
let continuationIntent = null;
let installed = false;
let inspectionSequence = 0;
let executingPromptId = null;

function unknownInspection() {
  return {
    segment_index: null, completed_segments: null, total_segments: null,
    completed_frames: null, total_frames: null, completed: false,
    reference_video: null, reference_image: null, recorded_video: null, recorded_image: null,
    output_directory: null, warnings: [], partials: [], message: "",
  };
}

function activeScope(workflowId, nodeId) {
  const key = JSON.stringify([String(workflowId), String(nodeId)]);
  return controller.submissions.get(key) ?? controller.sessions.get(key);
}

function activeWorkflowId() {
  try { return app.rootGraph?.serialize?.()?.id ?? null; } catch { return null; }
}

function entryNode(workflowId, nodeId) {
  if (!workflowId || activeWorkflowId() !== String(workflowId)) return null;
  const node = app.rootGraph?.getNodeById(nodeId) ?? app.rootGraph?.getNodeById(Number(nodeId));
  return node?.comfyClass === ENTRY_CLASS ? node : null;
}

function notify(severity, detail) {
  app.extensionManager?.toast?.add?.({ severity, summary: "Richard's Video Kits", detail, life: severity === "error" ? 10_000 : 5000 });
}

function setEntryStatus(workflowId, entryNodeId, update) {
  const key = JSON.stringify([String(workflowId), String(entryNodeId)]);
  const status = { ...statuses.get(key), ...update };
  statuses.delete(key);
  statuses.set(key, status);
  while (statuses.size > 256) statuses.delete(statuses.keys().next().value);
  const node = entryNode(workflowId, entryNodeId);
  if (node?.__rvkStatusWidget) node.__rvkStatusWidget.value = formatStatus(status);
  app.rootGraph?.setDirtyCanvas?.(true, true);
}

async function postJson(path, body) {
  const response = await api.fetchApi(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const result = await response.json();
  if (!response.ok || result.ok === false) {
    const error = new Error(result.message ?? result.error?.message ?? result.error ?? `HTTP ${response.status}`);
    error.status = result;
    throw error;
  }
  return result;
}

function inspectPrompt(prompt, path = "/rvk/wan-loop/inspect") {
  return postJson(path, { prompt: prompt.output, workflow: prompt.workflow });
}

async function refreshStatus(nodeId = null) {
  const workflowId = activeWorkflowId();
  if (!workflowId) return;
  const sequence = ++inspectionSequence;
  const targets = (app.rootGraph?._nodes ?? []).filter((node) => node.comfyClass === ENTRY_CLASS && (nodeId === null || String(node.id) === String(nodeId)));
  const pending = targets.filter((node) => {
    const running = activeScope(workflowId, node.id);
    setEntryStatus(workflowId, node.id, running?.status ?? { ...unknownInspection(), state: "inspecting" });
    return !running;
  });
  // The submitted prompt owns its displayed sources and progress until it ends.
  if (!pending.length) return;
  const current = () => sequence === inspectionSequence && activeWorkflowId() === workflowId;
  try {
    const prompt = await app.graphToPrompt();
    if (!current()) return;
    const scope = entryScope(prompt);
    if (!scope || (nodeId !== null && scope.entryNodeId !== String(nodeId))) throw new Error("当前工作流未包含可检查的 Loop Entry");
    if (!entryNode(workflowId, scope.entryNodeId) || scope.workflowId !== workflowId) return;
    const result = await inspectPrompt(prompt);
    if (current() && !activeScope(workflowId, scope.entryNodeId) && entryNode(workflowId, scope.entryNodeId)) setEntryStatus(workflowId, scope.entryNodeId, { ...unknownInspection(), ...result, state: result.completed ? "completed" : result.can_run === false ? "error" : "ready", message: result.message ?? "" });
  } catch (error) {
    if (!current()) return;
    for (const node of pending) if (!activeScope(workflowId, node.id)) setEntryStatus(workflowId, node.id, { ...unknownInspection(), ...error.status, state: "error", message: String(error.message) });
  }
}

const controller = new WanLoopFrontendController({
  queuePrompt: async (workflowId, entryNodeId, sessionId) => {
    if (continuationIntent || !entryNode(workflowId, entryNodeId)) throw new Error("RVK 无法续排已切换的工作流");
    const intent = { workflowId, entryNodeId, sessionId, consumed: false, promptId: null };
    continuationIntent = intent;
    try {
      if ((await app.queuePrompt(0, 1)) !== true || !intent.consumed || !intent.promptId) throw new Error("ComfyUI 未确认续排请求");
      return intent.promptId;
    } finally { if (continuationIntent === intent) continuationIntent = null; }
  },
  controlRun: (sessionId, action) => postJson("/rvk/wan-loop/control", { session_id: sessionId, action }),
  isEntryActive: (workflowId, entryNodeId) => Boolean(entryNode(workflowId, entryNodeId)),
  setEntryStatus,
  notify,
});

function installQueueHook() {
  if (installed) return;
  installed = true;
  const original = api.queuePrompt;
  api.queuePrompt = async function(number, supplied, ...args) {
    let prompt = structuredClone(supplied);
    // Never trust a session serialized by a saved workflow or another extension.
    if (prompt.workflow?.extra) delete prompt.workflow.extra.rvk_runtime;
    const identity = entryScope(prompt);
    if (!identity) return original.call(this, number, prompt, ...args);
    const intent = continuationIntent;
    const continuing = intent && !intent.consumed && intent.workflowId === identity.workflowId && intent.entryNodeId === identity.entryNodeId;
    if (intent && !continuing) throw new Error("RVK 续排期间工作流或入口发生变化");
    if (continuing) intent.consumed = true;
    const scope = controller.beginSubmission({ ...identity, sessionId: continuing ? intent.sessionId : null, inspection: unknownInspection(), output: prompt.output });
    ++inspectionSequence;
    try {
      if (continuing) {
        prompt.workflow.extra ??= {};
        prompt.workflow.extra.rvk_runtime = { entry_node_id: identity.entryNodeId, session_id: intent.sessionId };
      } else {
        if (!["new", "resume"].includes(identity.mode)) throw new Error("请选择 new 或 resume 模式；旧工作流请重新载入以迁移");
        const inspection = await inspectPrompt(prompt);
        controller.status(scope, inspection);
        if (identity.mode === "resume" && inspection.completed) {
          const prepared = await inspectPrompt(prompt, "/rvk/wan-loop/prepare-complete");
          controller.status(scope, prepared);
          if (!prepared.completed || prepared.partials?.length) throw new Error("工作目录状态已变化；请检查/刷新状态后重试");
          prompt = finalizeOnlyPrompt(prompt, identity.entryNodeId, prepared.output_directory ?? identity.outputDirectory);
          if (args[0]?.partialExecutionTargets) {
            args[0] = { ...args[0] };
            delete args[0].partialExecutionTargets;
          }
          scope.finalizing = true;
          scope.output = prompt.output;
          controller.status(scope, { state: "finalizing" });
        } else {
          if (inspection.can_run === false) throw new Error(inspection.message ?? "RVK 当前输入或工作目录无法运行");
          scope.expectedSegment = inspection.completed_segments ?? 0;
        }
      }
      if (!entryNode(identity.workflowId, identity.entryNodeId) || scope.stopped) throw new Error("RVK 工作流已切换或运行已停止");
      const response = await controller.withTimeout(original.call(this, number, prompt, ...args));
      await controller.confirmSubmission(scope, response?.prompt_id);
      if (continuing) intent.promptId = response.prompt_id;
      return response;
    } catch (error) {
      if (error.status) controller.status(scope, error.status);
      await controller.failSubmission(scope, error.message ?? error);
      throw error;
    }
  };
}

app.registerExtension({
  name: "richards-video-kits.wan-loop",
  async setup() {
    installQueueHook();
    const listen = (event, handler) => api.addEventListener(event, (value) => {
      Promise.resolve(handler(value.detail)).catch((error) => notify("error", String(error)));
    });
    listen("executed", (detail) => controller.handleExecuted(detail));
    listen("execution_start", (detail) => {
      executingPromptId = typeof detail?.prompt_id === "string" ? detail.prompt_id : null;
    });
    listen("executing", (detail) => {
      // ComfyUI's public API dispatches executing as a node ID, while
      // execution_start retains the prompt ID from the same ordered socket.
      if (typeof detail === "string" || typeof detail === "number") {
        if (!executingPromptId) return;
        return controller.handleExecuting({ prompt_id: executingPromptId, node: detail });
      }
      if (detail && typeof detail === "object") return controller.handleExecuting(detail);
    });
    const terminal = (handler) => (detail) => {
      if (executingPromptId === detail?.prompt_id) executingPromptId = null;
      return handler(detail);
    };
    listen("execution_success", terminal((detail) => controller.handleSuccess(detail)));
    listen("execution_error", terminal((detail) => controller.handleFailure(detail)));
    listen("execution_interrupted", terminal((detail) => controller.handleFailure(detail)));
    listen("reconnected", () => {
      executingPromptId = null;
      controller.discardDisconnectedTracking();
      return refreshStatus();
    });
  },
  async beforeConfigureGraph(workflow) {
    ++inspectionSequence;
    if (workflow.extra) delete workflow.extra.rvk_runtime;
    const result = migrateLegacyWorkflow(workflow);
    if (result.migrated) notify("warn", `已迁移 ${result.migrated} 个旧 Loop Entry：移除 run_token，模式设为 new。请按需要选择 resume。${result.removedLinks ? `已断开 ${result.removedLinks} 条旧 token 连线；其他连接保留。` : ""}`);
  },
  async afterConfigureGraph() { await refreshStatus(); },
  async nodeCreated(node) {
    if (node.comfyClass !== ENTRY_CLASS || node.__rvkStatusWidget) return;
    const element = document.createElement("textarea");
    element.readOnly = true;
    element.value = formatStatus();
    element.setAttribute("aria-label", "RVK 运行状态");
    element.style.cssText = "width:100%;height:100%;resize:none;box-sizing:border-box;font:12px monospace;white-space:pre-wrap";
    // A dedicated type keeps this live element in both renderers. The Vue
    // customtext alias substitutes a different textarea backed by a value store.
    node.__rvkStatusWidget = node.addDOMWidget("rvk_status", "rvk-status", element, { serialize: false, getMinHeight: () => 160, getValue: () => element.value, setValue: (value) => { element.value = value; } });
    node.__rvkStatusWidget.serialize = false;
    const inspect = node.addWidget("button", "检查/刷新状态", null, () => void refreshStatus(String(node.id)));
    const stop = node.addWidget("button", "当前片段结束后停止", null, () => {
      const workflowId = activeWorkflowId();
      if (workflowId) void controller.stopEntry(workflowId, String(node.id));
    });
    for (const widget of [inspect, stop]) { widget.serialize = false; widget.options ??= {}; widget.options.serialize = false; }
  },
});
