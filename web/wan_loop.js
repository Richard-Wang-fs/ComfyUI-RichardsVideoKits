import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";
import {
  WanLoopFrontendController,
  matchingQueuedPromptIds,
} from "./wan_loop_core.mjs";

const ENTRY_CLASS = "RVKWanLoopEntry";

function activeWorkflowId() {
  try {
    const workflowId = app.rootGraph?.serialize?.()?.id;
    return typeof workflowId === "string" && workflowId ? workflowId : null;
  } catch {
    return null;
  }
}

function entryNode(workflowId, nodeId) {
  if (!workflowId || activeWorkflowId() !== String(workflowId)) return null;
  const graph = app.rootGraph;
  if (!graph) return null;
  const node = graph.getNodeById(nodeId) ?? graph.getNodeById(Number(nodeId));
  return node?.comfyClass === ENTRY_CLASS ? node : null;
}

function tokenWidget(node) {
  return node?.widgets?.find((widget) => widget.name === "run_token") ?? null;
}

function updateEntryToken(workflowId, nodeId, expectedValue, value) {
  const node = entryNode(workflowId, nodeId);
  const widget = tokenWidget(node);
  if (!widget) return false;
  if (String(widget.value ?? "") !== String(expectedValue)) return false;
  widget.value = value;
  widget.callback?.(value, app.canvas, node);
  app.rootGraph?.setDirtyCanvas?.(true, true);
  return true;
}

function getEntryToken(workflowId, nodeId) {
  const widget = tokenWidget(entryNode(workflowId, nodeId));
  return widget ? String(widget.value ?? "") : null;
}

function compareAndSetEntryToken(workflowId, nodeId, expectedValue, value) {
  return updateEntryToken(workflowId, nodeId, expectedValue, value);
}

function compareAndClearEntryToken(workflowId, nodeId, expectedValue) {
  return updateEntryToken(workflowId, nodeId, expectedValue, "");
}

function notify(severity, detail) {
  app.extensionManager?.toast?.add?.({
    severity,
    summary: "Richard's Video Kits",
    detail,
    life: severity === "error" ? 8000 : 4000,
  });
}

async function controlRun(runToken, action) {
  const response = await api.fetchApi("/rvk/wan-loop/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_token: runToken, action }),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

const controller = new WanLoopFrontendController({
  queuePrompt: async (workflowId, entryNodeId, token, excludedPromptId) => {
    if (getEntryToken(workflowId, entryNodeId) !== token) {
      throw new Error("the original workflow entry is no longer active");
    }
    if ((await app.queuePrompt(0, 1)) !== true) return null;
    const response = await api.fetchApi("/queue", { cache: "no-store" });
    if (!response.ok) throw new Error(`Queue confirmation failed: HTTP ${response.status}`);
    const promptIds = matchingQueuedPromptIds(
      await response.json(),
      workflowId,
      entryNodeId,
      token,
      excludedPromptId,
    );
    if (promptIds.length !== 1) {
      throw new Error(`Queue confirmation found ${promptIds.length} matching prompts`);
    }
    return promptIds[0];
  },
  controlRun,
  getEntryToken,
  compareAndSetEntryToken,
  compareAndClearEntryToken,
  notify,
});

app.registerExtension({
  name: "richards-video-kits.wan-loop",

  async setup() {
    api.addEventListener("executed", (event) => void controller.handleExecuted(event.detail));
    api.addEventListener("execution_success", (event) => void controller.handleSuccess(event.detail));
    api.addEventListener("execution_error", (event) => void controller.handleFailure(event.detail));
    api.addEventListener("execution_interrupted", (event) => void controller.handleFailure(event.detail));
  },

  async afterConfigureGraph() {
    const workflowId = activeWorkflowId();
    if (!workflowId) return;
    for (const node of app.rootGraph?._nodes ?? []) {
      if (node?.comfyClass === ENTRY_CLASS) {
        controller.reconcileEntry(workflowId, String(node.id));
      }
    }
  },

  async nodeCreated(node) {
    if (node.comfyClass !== ENTRY_CLASS || node.__rvkStopWidget) return;
    node.__rvkStopWidget = node.addWidget("button", "Stop RVK after current segment", null, () => {
      const workflowId = activeWorkflowId();
      if (!workflowId) {
        notify("error", "RVK cannot identify the active workflow");
        return;
      }
      void controller.stopEntry(workflowId, String(node.id));
    });
  },
});
