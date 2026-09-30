export function firstPayload(output, key) {
  const value = output?.[key];
  return Array.isArray(value) ? value[0] ?? null : value && typeof value === "object" ? value : null;
}

export function entryScope(prompt) {
  const entries = Object.entries(prompt?.output ?? {}).filter(([, node]) => node?.class_type === "RVKWanLoopEntry");
  if (!entries.length) return null;
  if (entries.length !== 1) throw new Error("RVK 每次运行只能包含一个 Loop Entry");
  const workflowId = prompt.workflow?.id;
  if (typeof workflowId !== "string" || !workflowId) throw new Error("RVK 无法识别工作流");
  const [entryNodeId, node] = entries[0];
  if (entryNodeId.includes(":")) throw new Error("请将 RVK Loop Entry 放在工作流顶层");
  return { workflowId, entryNodeId, mode: node.inputs?.mode, outputDirectory: node.inputs?.output_directory };
}

const isLink = (value) => Array.isArray(value) && value.length === 2 && Number.isInteger(value[1]);

// Only the submitted API graph changes. The editable canvas graph stays intact.
export function finalizeOnlyPrompt(prompt, entryNodeId, outputDirectory) {
  const output = { ...prompt.output };
  const candidates = Object.entries(output).filter(([, node]) => {
    if (node.class_type !== "RVKFinalizeSegments") return false;
    const directory = node.inputs?.output_directory;
    const status = node.inputs?.loop_status;
    const advance = isLink(status) ? output[String(status[0])] : null;
    const plan = advance?.inputs?.plan;
    return (isLink(directory) && String(directory[0]) === entryNodeId && directory[1] === 9) ||
      (advance?.class_type === "RVKWanAdvance" && isLink(plan) && String(plan[0]) === entryNodeId && plan[1] === 4);
  });
  if (candidates.length !== 1) throw new Error("已有片段已完成：请连接唯一的 RVK Finalize Segments 以直接合成");
  if (typeof outputDirectory !== "string" || !outputDirectory) throw new Error("无法确定已完成片段的工作目录");
  const [finalizeId, originalFinalize] = candidates[0];
  const finalize = { ...originalFinalize, inputs: { ...originalFinalize.inputs } };
  output[finalizeId] = finalize;
  finalize.inputs.output_directory = outputDirectory;
  delete finalize.inputs.loop_status;
  const retained = {};
  const visiting = new Set();
  const visit = (id) => {
    if (retained[id]) return;
    if (visiting.has(id) || !output[id]) throw new Error("合成节点的上游连接无效");
    if (id !== finalizeId && output[id].class_type !== "LoadVideo") {
      throw new Error("无法确认独立合成的上游无需模型；请直连 LoadVideo，并使用明确的成品目录和文件名");
    }
    visiting.add(id);
    for (const value of Object.values(output[id].inputs ?? {})) if (isLink(value)) visit(String(value[0]));
    visiting.delete(id);
    retained[id] = output[id];
  };
  visit(finalizeId);
  return { ...prompt, output: retained };
}

export function migrateLegacyWorkflow(workflow) {
  let migrated = 0;
  let removedLinks = 0;
  const graphs = [workflow, ...(workflow?.definitions?.subgraphs ?? [])];
  for (const graph of graphs) {
    const removed = new Set();
    for (const node of graph?.nodes ?? []) {
      if (node.type !== "RVKWanLoopEntry") continue;
      const oldInput = node.inputs?.find((input) => input.name === "run_token");
      const oldOutput = node.outputs?.[5]?.name === "run_token";
      if (!oldInput && !oldOutput) continue;
      migrated += 1;
      if (oldInput) {
        if (oldInput.link != null) removed.add(oldInput.link);
        oldInput.name = "mode";
        oldInput.localized_name = "mode";
        oldInput.type = "COMBO";
        oldInput.widget = { name: "mode" };
        oldInput.link = null;
      }
      if (oldOutput) {
        for (const id of node.outputs[5].links ?? []) removed.add(id);
        node.outputs[5].name = "status_text";
        node.outputs[5].localized_name = "status_text";
        node.outputs[5].links = [];
      }
      if (Array.isArray(node.widgets_values)) node.widgets_values[2] = "new";
    }
    if (!removed.size) continue;
    removedLinks += removed.size;
    graph.links = (graph.links ?? []).filter((link) => !removed.has(Array.isArray(link) ? link[0] : link.id));
    for (const node of graph.nodes ?? []) {
      for (const input of node.inputs ?? []) if (removed.has(input.link)) input.link = null;
      for (const output of node.outputs ?? []) if (Array.isArray(output.links)) output.links = output.links.filter((id) => !removed.has(id));
    }
  }
  return { migrated, removedLinks };
}

export function formatStatus(status = {}) {
  const value = (item) => item === null || item === undefined || item === "" ? "未知" : String(item);
  const names = { inspecting: "检查中", ready: "待运行", queued: "已排队", generating: "生成中", saving: "保存中", finalizing: "合成中", completed: "已完成", stopped: "已停止", error: "错误" };
  const lines = [
    `状态：${names[status.state] ?? (status.completed ? "已完成" : "待运行")}`,
    `当前段：${status.state === "completed" || status.completed ? "全部完成" : Number.isInteger(status.segment_index) ? status.segment_index + 1 : "未知"}`,
    `片段：已完成 ${value(status.completed_segments)} / 总计 ${value(status.total_segments)}`,
    `帧数：已完成 ${value(status.completed_frames)} / 总计 ${value(status.total_frames)}`,
    `当前视频：${value(status.reference_video)}`,
    `当前参考图：${value(status.reference_image)}`,
    `历史视频：${value(status.recorded_video)}`,
    `历史参考图：${value(status.recorded_image)}`,
  ];
  if (status.message) lines.push(String(status.message));
  if (Array.isArray(status.warnings)) lines.push(...status.warnings.map(String));
  if (Array.isArray(status.partials) && status.partials.length) lines.push(`未完成文件：${status.partials.join(", ")}`);
  return lines.join("\n");
}

const scopeKey = (workflowId, entryNodeId) => JSON.stringify([String(workflowId), String(entryNodeId)]);

export class WanLoopFrontendController {
  constructor({ queuePrompt, controlRun, isEntryActive, setEntryStatus = () => {}, notify = () => {}, now = () => Date.now(), setTimer = (callback, delay) => globalThis.setTimeout(callback, delay), clearTimer = (timer) => globalThis.clearTimeout(timer), queueTimeoutMs = 15_000, blockedSessionTtlMs = 300_000, trackingLimit = 256 }) {
    // Browser timers require the global receiver, even when called through this controller.
    Object.assign(this, { queuePrompt, controlRun, isEntryActive, setEntryStatus, notify, now, setTimer, clearTimer, queueTimeoutMs, blockedSessionTtlMs, trackingLimit });
    this.submissions = new Map();
    this.prompts = new Map();
    this.sessions = new Map();
    this.blocked = new Map();
    this.earlyEvents = new Map();
  }

  status(scope, update) {
    scope.status = { ...scope.status, ...update };
    this.setEntryStatus(scope.workflowId, scope.entryNodeId, scope.status);
  }

  beginSubmission({ workflowId, entryNodeId, sessionId = null, inspection = {}, finalizing = false, output = {} }) {
    const key = scopeKey(workflowId, entryNodeId);
    if (!this.isEntryActive(workflowId, entryNodeId)) throw new Error("RVK 工作流已切换，停止提交");
    if (this.submissions.has(key)) throw new Error("RVK 已有排队或运行中的片段");
    const existing = this.sessions.get(key);
    if (existing && (!sessionId || existing.sessionId !== sessionId || !existing.awaitingNext || existing.stopped)) throw new Error("RVK 已有运行；停止后再重新开始");
    if (sessionId && (!existing || this.isBlocked(sessionId))) throw new Error("RVK 的续排会话已失效，请使用 resume");
    const scope = { workflowId, entryNodeId, key, sessionId, promptId: null, observedPromptId: null, finalizing, status: existing?.status ?? inspection, expectedSegment: existing?.nextSegment ?? null, output, stopped: false, confirmed: false, advance: null, finished: false };
    this.submissions.set(key, scope);
    this.status(scope, { state: finalizing ? "finalizing" : "queued", message: "" });
    return scope;
  }

  async confirmSubmission(scope, promptId) {
    if (typeof promptId !== "string" || !promptId || (scope.observedPromptId && scope.observedPromptId !== promptId)) throw new Error("RVK 未收到匹配的 prompt ID");
    if (this.prompts.has(promptId) && this.prompts.get(promptId) !== scope) throw new Error("RVK 收到重复的 prompt ID");
    scope.promptId = promptId;
    scope.confirmed = true;
    if (!scope.finished) this.prompts.set(promptId, scope);
    const events = this.earlyEvents.get(promptId) ?? [];
    this.earlyEvents.delete(promptId);
    scope.replaying = true;
    try {
      for (const [kind, detail] of events) {
        if (kind === "executed") await this.handleExecuted(detail);
        else if (kind === "failure") await this.handleFailure(detail);
        else if (kind === "success") await this.handleSuccess(detail);
        else this.handleExecuting(detail);
      }
    } finally {
      scope.replaying = false;
    }
    if (![...this.submissions.values()].some((item) => !item.confirmed)) this.earlyEvents.clear();
    // Give the public app.queuePrompt call its response before a very fast
    // completed prompt asks it to submit another item.
    if (scope.finished && !scope.failed && !scope.detached) this.setTimer(() => void this.complete(scope), 0);
  }

  bufferEvent(kind, detail) {
    if (!detail?.prompt_id || ![...this.submissions.values()].some((scope) => !scope.confirmed)) return;
    const rvkKind = kind === "executed" ? firstPayload(detail.output, "rvk_wan_entry") ? "entry" : firstPayload(detail.output, "rvk_wan_loop") ? "advance" : null : null;
    if (kind === "executed" && !rvkKind) return;
    const events = this.earlyEvents.get(detail.prompt_id) ?? [];
    const previous = events.findIndex(([eventKind, value]) => eventKind === kind && (kind !== "executed" || Boolean(firstPayload(value.output, "rvk_wan_entry")) === (rvkKind === "entry")));
    if (previous >= 0) events[previous] = [kind, detail];
    else if (events.length < 16) events.push([kind, detail]);
    this.earlyEvents.set(detail.prompt_id, events);
    while (this.earlyEvents.size > this.trackingLimit) this.earlyEvents.delete(this.earlyEvents.keys().next().value);
  }

  async failSubmission(scope, error) {
    if (scope.detached) return;
    await this.abort(scope, `提交失败：${error}`);
  }

  discardDisconnectedTracking() {
    for (const scope of new Set([...this.submissions.values(), ...this.sessions.values(), ...this.prompts.values()])) {
      scope.detached = true;
      scope.stopped = true;
      scope.finished = true;
      if (scope.sessionId) this.blockSession(scope.sessionId);
      this.status(scope, { state: "stopped", message: "连接已恢复，自动续排已停止；检查后可选择 resume。" });
    }
    this.submissions.clear();
    this.sessions.clear();
    this.prompts.clear();
    this.earlyEvents.clear();
  }

  async handleExecuted(detail) {
    const promptId = detail?.prompt_id;
    const tracked = this.prompts.get(promptId);
    if (!tracked?.confirmed) {
      this.bufferEvent("executed", detail);
      return false;
    }
    const entry = firstPayload(detail?.output, "rvk_wan_entry");
    if (entry?.version === 3 && entry.kind === "entry" && typeof entry.session_id === "string" && entry.session_id && promptId) {
      if (this.isBlocked(entry.session_id)) return false;
      const key = scopeKey(entry.workflow_id, entry.entry_node_id);
      const scope = this.submissions.get(key);
      if (!scope || scope !== tracked) return false;
      if (scope.promptId && scope.promptId !== promptId) return false;
      if (!this.isEntryActive(scope.workflowId, scope.entryNodeId) ||
        (scope.promptId && scope.promptId !== promptId) ||
        (scope.observedPromptId && scope.observedPromptId !== promptId) ||
        (scope.sessionId && scope.sessionId !== entry.session_id) ||
        (scope.expectedSegment !== null && scope.expectedSegment !== entry.segment_index)) {
        if (!scope.sessionId) scope.sessionId = entry.session_id;
        await this.abort(scope, "收到不匹配的续排结果，已停止自动续排");
        return false;
      }
      scope.sessionId = entry.session_id;
      scope.observedPromptId = promptId;
      this.prompts.set(promptId, scope);
      this.sessions.set(key, scope);
      this.status(scope, { ...entry, state: scope.stopped ? "stopped" : "generating", message: scope.stopped ? "当前片段结束后停止，可使用 resume 继续。" : "" });
      if (scope.stopped) {
        try { await this.controlRun(scope.sessionId, "stop"); }
        catch (error) { this.notify("error", `停止请求未确认：${error}`); }
      }
      return true;
    }
    const advance = firstPayload(detail?.output, "rvk_wan_loop");
    const scope = this.prompts.get(promptId);
    if (!scope || advance?.version !== 3 || advance.kind !== "advance" || scope.sessionId !== advance.session_id ||
      scope.workflowId !== String(advance.workflow_id) || scope.entryNodeId !== String(advance.entry_node_id)) return false;
    if (scope.advance) return true;
    scope.advance = advance;
    this.status(scope, { ...advance, state: scope.stopped || advance.stopped ? "stopped" : advance.has_more ? "saving" : "finalizing" });
    return true;
  }

  handleExecuting(detail) {
    const scope = this.prompts.get(detail?.prompt_id);
    if (!scope) { this.bufferEvent("executing", detail); return false; }
    if (detail.node == null) return false;
    const node = scope.output[String(detail.node)];
    const state = scope.stopped ? "stopped" : node?.class_type === "RVKSaveSegmentVideo" ? "saving" : node?.class_type === "RVKFinalizeSegments" ? "finalizing" : "generating";
    this.status(scope, { state });
    return true;
  }

  async handleSuccess(detail) {
    const scope = this.prompts.get(detail?.prompt_id);
    if (!scope) { this.bufferEvent("success", detail); return false; }
    if (scope.finished) return false;
    scope.finished = true;
    this.prompts.delete(detail.prompt_id);
    if (!scope.confirmed || scope.replaying) return true;
    return this.complete(scope);
  }

  async complete(scope) {
    if (scope.completing || scope.detached) return false;
    scope.completing = true;
    if (this.submissions.get(scope.key) === scope) this.submissions.delete(scope.key);
    const advance = scope.advance;
    if (scope.finalizing || (advance && (!advance.has_more || advance.stopped || scope.stopped))) {
      this.sessions.delete(scope.key);
      this.status(scope, { state: advance?.stopped || scope.stopped ? "stopped" : "completed" });
      return true;
    }
    if (!advance) {
      await this.abort(scope, "本轮未得到有效保存回执；修复后使用 resume 继续");
      return false;
    }
    if (!this.isEntryActive(scope.workflowId, scope.entryNodeId)) {
      await this.abort(scope, "工作流已切换，自动续排已停止；返回后可使用 resume");
      return false;
    }
    scope.awaitingNext = true;
    scope.nextSegment = advance.next_segment ?? advance.completed_segments ?? advance.completed_segment + 1;
    try {
      const accepted = await this.withTimeout(this.queuePrompt(scope.workflowId, scope.entryNodeId, scope.sessionId));
      if (typeof accepted !== "string" || !accepted) throw new Error("未确认下一段 prompt ID");
      return true;
    } catch (error) {
      if (scope.detached) return false;
      await this.abort(scope, `无法续排：${error}。已保存片段保留，可使用 resume。`);
      return false;
    }
  }

  async handleFailure(detail) {
    const scope = this.prompts.get(detail?.prompt_id);
    if (!scope) { this.bufferEvent("failure", detail); return false; }
    await this.abort(scope, `执行失败：${detail.exception_message ?? "执行被中断"}。已完成片段保留，修复后选择 resume。`, "abort");
    return true;
  }

  async stopEntry(workflowId, entryNodeId) {
    const key = scopeKey(workflowId, entryNodeId);
    const scope = this.submissions.get(key) ?? this.sessions.get(key);
    if (!scope) return false;
    scope.stopped = true;
    this.status(scope, { state: "stopped", message: "当前片段结束后停止，可使用 resume 继续。" });
    if (!scope.sessionId) return true;
    try {
      await this.controlRun(scope.sessionId, "stop");
    } catch (error) {
      this.notify("error", `停止请求未确认：${error}`);
    }
    this.blockSession(scope.sessionId);
    // Keep the admitted prompt until its terminal event so an in-flight Save
    // can still update completed counts or report its failure after Stop.
    if (scope.finished && !this.submissions.has(key)) this.sessions.delete(key);
    return true;
  }

  async abort(scope, message, action = "stop") {
    scope.failed = true;
    scope.finished = true;
    scope.stopped = true;
    if (scope.sessionId) this.blockSession(scope.sessionId);
    for (const [id, item] of this.prompts) if (item.key === scope.key && (!scope.sessionId || item.sessionId === scope.sessionId)) this.prompts.delete(id);
    const submission = this.submissions.get(scope.key);
    if (submission === scope || submission?.sessionId === scope.sessionId) this.submissions.delete(scope.key);
    const active = this.sessions.get(scope.key);
    if (active === scope || active?.sessionId === scope.sessionId) this.sessions.delete(scope.key);
    this.status(scope, { state: "error", message });
    if (scope.sessionId) {
      // Uncertain/active executions must retain their writer lease until Save
      // and Advance finish. Only a terminal execution failure may abort it.
      try { await this.controlRun(scope.sessionId, action); } catch { /* Backend idle TTL remains a leak guard. */ }
    }
    this.notify("error", message);
  }

  isBlocked(sessionId) {
    for (const [id, value] of this.blocked) if (value.expiresAt <= this.now()) this.releaseBlocked(id);
    return this.blocked.has(sessionId);
  }

  releaseBlocked(sessionId) {
    const item = this.blocked.get(sessionId);
    if (item) this.clearTimer(item.timer);
    this.blocked.delete(sessionId);
  }

  blockSession(sessionId) {
    this.releaseBlocked(sessionId);
    const timer = this.setTimer(() => this.releaseBlocked(sessionId), this.blockedSessionTtlMs);
    timer?.unref?.();
    this.blocked.set(sessionId, { timer, expiresAt: this.now() + this.blockedSessionTtlMs });
    while (this.blocked.size > this.trackingLimit) this.releaseBlocked(this.blocked.keys().next().value);
  }

  async withTimeout(promise) {
    let timer;
    try {
      return await Promise.race([promise, new Promise((_, reject) => {
        timer = this.setTimer(() => reject(new Error("RVK 排队确认超时")), this.queueTimeoutMs);
      })]);
    } finally { if (timer !== undefined) this.clearTimer(timer); }
  }
}
