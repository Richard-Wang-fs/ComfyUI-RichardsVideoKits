export function firstPayload(output, key) {
  const value = output?.[key];
  if (Array.isArray(value)) return value[0] ?? null;
  return value && typeof value === "object" ? value : null;
}

export function matchingQueuedPromptIds(
  queueState,
  workflowId,
  entryNodeId,
  token,
  excludedPromptId = null,
) {
  const items = [
    ...(Array.isArray(queueState?.queue_running) ? queueState.queue_running : []),
    ...(Array.isArray(queueState?.queue_pending) ? queueState.queue_pending : []),
  ];
  const matches = new Set();
  for (const item of items) {
    if (!Array.isArray(item) || typeof item[1] !== "string" || item[1] === excludedPromptId) {
      continue;
    }
    const prompt = item[2];
    const workflow = item[3]?.extra_pnginfo?.workflow;
    const node = prompt?.[String(entryNodeId)];
    if (
      String(workflow?.id ?? "") === String(workflowId) &&
      node?.class_type === "RVKWanLoopEntry" &&
      node?.inputs?.run_token === token
    ) {
      matches.add(item[1]);
    }
  }
  return [...matches];
}

const DEFAULT_QUEUE_TIMEOUT_MS = 15_000;
const DEFAULT_BLOCKED_TOKEN_TTL_MS = 5 * 60_000;
const DEFAULT_TRACKING_LIMIT = 256;

export class WanLoopFrontendController {
  constructor({
    queuePrompt,
    controlRun,
    getEntryToken,
    compareAndSetEntryToken,
    compareAndClearEntryToken,
    notify = () => {},
    now = () => Date.now(),
    setTimer = (callback, delay) => setTimeout(callback, delay),
    clearTimer = (timer) => clearTimeout(timer),
    queueTimeoutMs = DEFAULT_QUEUE_TIMEOUT_MS,
    blockedTokenTtlMs = DEFAULT_BLOCKED_TOKEN_TTL_MS,
    trackingLimit = DEFAULT_TRACKING_LIMIT,
  }) {
    this.queuePrompt = queuePrompt;
    this.controlRun = controlRun;
    this.getEntryToken = getEntryToken;
    this.compareAndSetEntryToken = compareAndSetEntryToken;
    this.compareAndClearEntryToken = compareAndClearEntryToken;
    this.notify = notify;
    this.now = now;
    this.setTimer = setTimer;
    this.clearTimer = clearTimer;
    this.queueTimeoutMs = queueTimeoutMs;
    this.blockedTokenTtlMs = blockedTokenTtlMs;
    this.trackingLimit = trackingLimit;
    this.promptRuns = new Map();
    this.pending = new Map();
    this.blockedTokens = new Map();
    this.lastScheduled = new Map();
    this.deferredClears = new Map();
  }

  async handleExecuted(detail) {
    const promptId = detail?.prompt_id;
    if (!promptId) return false;
    const entry = firstPayload(detail.output, "rvk_wan_entry");
    if (
      entry?.version === 2 &&
      entry.kind === "entry" &&
      entry.run_token &&
      entry.workflow_id &&
      entry.entry_node_id &&
      typeof entry.submitted_run_token === "string"
    ) {
      if (this._isBlocked(entry.run_token)) return false;
      const workflowId = String(entry.workflow_id);
      const entryNodeId = String(entry.entry_node_id);
      const existing = this.promptRuns.get(promptId);
      if (
        existing?.runToken === entry.run_token &&
        existing.workflowId === workflowId &&
        existing.entryNodeId === entryNodeId
      ) {
        return true;
      }
      if (entry.submitted_run_token) {
        const scheduled = this.lastScheduled.get(entry.run_token);
        if (
          entry.submitted_run_token !== entry.run_token ||
          !scheduled ||
          scheduled.workflowId !== workflowId ||
          scheduled.entryNodeId !== entryNodeId ||
          scheduled.nextSegment !== entry.segment_index ||
          (scheduled.promptId !== null && scheduled.promptId !== promptId) ||
          (scheduled.observedPromptId !== null && scheduled.observedPromptId !== promptId)
        ) {
          await this._abort(
            entry.run_token,
            workflowId,
            entryNodeId,
            "RVK received an unexpected continuation prompt; automatic continuation stopped",
          );
          return false;
        }
        scheduled.observedPromptId = promptId;
      }
      if (
        !this.compareAndSetEntryToken(
          workflowId,
          entryNodeId,
          entry.submitted_run_token,
          entry.run_token,
        )
      ) {
        await this._abort(
          entry.run_token,
          workflowId,
          entryNodeId,
          "RVK workflow changed before the run token could be attached; automatic continuation stopped",
        );
        return false;
      }
      const scheduled = this.lastScheduled.get(entry.run_token);
      if (entry.submitted_run_token && scheduled?.promptId !== null) {
        this.lastScheduled.delete(entry.run_token);
      }
      this.promptRuns.set(promptId, {
        runToken: entry.run_token,
        workflowId,
        entryNodeId,
      });
      this.notify("info", `RVK segment ${entry.segment_index} started`);
      return true;
    }

    const advance = firstPayload(detail.output, "rvk_wan_loop");
    if (advance?.version !== 2 || advance.kind !== "advance") return false;
    const active = this.promptRuns.get(promptId);
    if (
      !active ||
      active.runToken !== advance.run_token ||
      active.workflowId !== String(advance.workflow_id) ||
      active.entryNodeId !== String(advance.entry_node_id)
    ) {
      return false;
    }
    this.pending.set(promptId, advance);
    this.notify(
      "success",
      `RVK saved segment ${advance.completed_segment}: ${advance.path}`,
    );
    return true;
  }

  async handleSuccess(detail) {
    const promptId = detail?.prompt_id;
    const advance = this.pending.get(promptId);
    if (!advance) return false;
    this.pending.delete(promptId);
    this.promptRuns.delete(promptId);
    const token = advance.run_token;
    const workflowId = String(advance.workflow_id);
    const entryNodeId = String(advance.entry_node_id);
    if (!advance.has_more || advance.stopped || this._isBlocked(token)) {
      this._clearToken(workflowId, entryNodeId, token);
      this._releaseBlockedToken(token);
      this.lastScheduled.delete(token);
      this.notify("info", advance.stopped ? "RVK run stopped" : "RVK run completed");
      return true;
    }
    if (this.getEntryToken(workflowId, entryNodeId) !== token) {
      await this._abort(
        token,
        workflowId,
        entryNodeId,
        "RVK workflow changed; automatic continuation stopped",
      );
      return false;
    }
    const existingSchedule = this.lastScheduled.get(token);
    if (existingSchedule?.completedSegment === advance.completed_segment) return false;
    this._boundedSet(this.lastScheduled, token, {
      workflowId,
      entryNodeId,
      completedSegment: advance.completed_segment,
      nextSegment: advance.completed_segment + 1,
      promptId: null,
      observedPromptId: null,
    });
    try {
      const queuedPromptId = await this._queueWithTimeout(
        workflowId,
        entryNodeId,
        token,
        promptId,
      );
      const scheduled = this.lastScheduled.get(token);
      if (!scheduled || (scheduled.observedPromptId && scheduled.observedPromptId !== queuedPromptId)) {
        throw new Error("the confirmed prompt does not match the observed continuation");
      }
      scheduled.promptId = queuedPromptId;
      if (scheduled.observedPromptId) this.lastScheduled.delete(token);
      return true;
    } catch (error) {
      await this._abort(
        token,
        workflowId,
        entryNodeId,
        `RVK could not queue the next segment: ${error}`,
      );
      return false;
    }
  }

  async handleFailure(detail) {
    const promptId = detail?.prompt_id;
    const active = this.promptRuns.get(promptId);
    const advance = this.pending.get(promptId);
    const token = active?.runToken ?? advance?.run_token;
    const workflowId = active?.workflowId ?? (advance ? String(advance.workflow_id) : null);
    const entryNodeId = active?.entryNodeId ?? (advance ? String(advance.entry_node_id) : null);
    this.promptRuns.delete(promptId);
    this.pending.delete(promptId);
    if (!token || !workflowId || !entryNodeId) return false;
    await this._abort(
      token,
      workflowId,
      entryNodeId,
      "RVK prompt failed; automatic continuation stopped",
    );
    return true;
  }

  async stopEntry(workflowId, entryNodeId) {
    const scopedWorkflowId = String(workflowId);
    const nodeId = String(entryNodeId);
    const token = this.getEntryToken(scopedWorkflowId, nodeId);
    if (!token) return false;
    this._blockToken(token);
    this._clearToken(scopedWorkflowId, nodeId, token);
    this._dropPromptTracking(token);
    this.lastScheduled.delete(token);
    try {
      await this.controlRun(token, "stop");
      this._releaseBlockedToken(token);
      this.notify("info", "RVK will not queue another segment");
    } catch (error) {
      this.notify("error", `RVK stop request failed: ${error}`);
    }
    return true;
  }

  reconcileEntry(workflowId, entryNodeId) {
    const scopedWorkflowId = String(workflowId);
    const nodeId = String(entryNodeId);
    const token = this.getEntryToken(scopedWorkflowId, nodeId);
    if (!token) return false;
    const pending = this.deferredClears.get(token);
    if (!pending || pending.workflowId !== scopedWorkflowId || pending.entryNodeId !== nodeId) {
      return false;
    }
    if (!this.compareAndClearEntryToken(scopedWorkflowId, nodeId, token)) return false;
    this.deferredClears.delete(token);
    return true;
  }

  _clearToken(workflowId, entryNodeId, token) {
    if (this.compareAndClearEntryToken(workflowId, entryNodeId, token)) {
      this.deferredClears.delete(token);
      return true;
    }
    this._boundedSet(this.deferredClears, token, { workflowId, entryNodeId });
    return false;
  }

  async _abort(token, workflowId, entryNodeId, message) {
    this._blockToken(token);
    this._clearToken(String(workflowId), String(entryNodeId), token);
    this.lastScheduled.delete(token);
    this._dropPromptTracking(token);
    try {
      await this.controlRun(token, "abort");
      this._releaseBlockedToken(token);
    } catch {
      // TTL remains the backend leak guard if the browser loses the server.
    }
    this.notify("error", message);
  }

  async _queueWithTimeout(workflowId, entryNodeId, token, excludedPromptId) {
    let timer = null;
    const timeout = new Promise((_, reject) => {
      timer = this.setTimer(
        () => reject(new Error(`Queue submission was not confirmed within ${this.queueTimeoutMs} ms`)),
        this.queueTimeoutMs,
      );
    });
    try {
      const accepted = await Promise.race([
        Promise.resolve().then(() =>
          this.queuePrompt(workflowId, entryNodeId, token, excludedPromptId),
        ),
        timeout,
      ]);
      if (typeof accepted !== "string" || !accepted) {
        throw new Error("ComfyUI did not confirm a unique prompt ID for the Queue submission");
      }
      return accepted;
    } finally {
      if (timer !== null) this.clearTimer(timer);
    }
  }

  _dropPromptTracking(token) {
    for (const [promptId, active] of this.promptRuns) {
      if (active.runToken === token) this.promptRuns.delete(promptId);
    }
    for (const [promptId, advance] of this.pending) {
      if (advance.run_token === token) this.pending.delete(promptId);
    }
  }

  _blockToken(token) {
    this._pruneBlockedTokens();
    this._releaseBlockedToken(token);
    const blocked = {
      expiresAt: this.now() + this.blockedTokenTtlMs,
      timer: null,
    };
    blocked.timer = this.setTimer(() => {
      if (this.blockedTokens.get(token) === blocked) this.blockedTokens.delete(token);
    }, this.blockedTokenTtlMs);
    blocked.timer?.unref?.();
    this.blockedTokens.set(token, blocked);
    while (this.blockedTokens.size > this.trackingLimit) {
      this._releaseBlockedToken(this.blockedTokens.keys().next().value);
    }
  }

  _isBlocked(token) {
    this._pruneBlockedTokens();
    return this.blockedTokens.has(token);
  }

  _pruneBlockedTokens() {
    const current = this.now();
    for (const [token, blocked] of this.blockedTokens) {
      if (blocked.expiresAt <= current) this._releaseBlockedToken(token);
    }
  }

  _releaseBlockedToken(token) {
    const blocked = this.blockedTokens.get(token);
    if (!blocked) return false;
    if (blocked.timer !== null) this.clearTimer(blocked.timer);
    return this.blockedTokens.delete(token);
  }

  _boundedSet(map, key, value) {
    map.delete(key);
    map.set(key, value);
    while (map.size > this.trackingLimit) {
      map.delete(map.keys().next().value);
    }
  }
}
