"""ComfyUI V3 nodes for the Wan Animate 2 per-prompt loop."""

from __future__ import annotations

import json
import asyncio
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from comfy_api.latest import Caching, ComfyAPI, InputImpl, io

from rvk.comfy.wan_video import ExactFrameWindowVideo, inspect_wan_video_source
from rvk.errors import AssetInvalid, RVKError
from rvk.segment_video import SegmentVideoReceipt, _relative_directory
from rvk.wan_resume import RunDirectoryLease, inspect_run, quarantine_partials, read_last_frame, write_progress
from rvk.wan_loop import (
    DEFAULT_SEGMENT_LENGTH,
    MAX_SEGMENT_LENGTH,
    MIN_SEGMENT_LENGTH,
    WanContinuationCandidate,
    WanLoopError,
    WanLoopStatus,
    WanRunRegistry,
    WanSegmentPlan,
    collect_wan_segment,
)


RVK_WAN_SEGMENT_PLAN = io.Custom("RVK_WAN_SEGMENT_PLAN")
RVK_WAN_CONTINUATION = io.Custom("RVK_WAN_CONTINUATION")
RVK_WAN_LOOP_STATUS = io.Custom("RVK_WAN_LOOP_STATUS")
RVK_VIDEO_RECEIPT = io.Custom("RVK_VIDEO_RECEIPT")

WAN_RUNS = WanRunRegistry()
_ROUTES_INSTALLED = False
_FRONTEND_EVENT_VERSION = 3


class WanPromptLifecycle(Caching.CacheProvider):
    """Observe execution completion through ComfyUI's public lifecycle API.

    This provider neither caches nor retains node values, images, or history.
    """

    async def on_lookup(self, context):
        return None

    async def on_store(self, context, value):
        return None

    def should_cache(self, context, value=None):
        return False

    def on_prompt_end(self, prompt_id):
        WAN_RUNS.finish_prompt(prompt_id)


_LIFECYCLE = WanPromptLifecycle()


def _event_payload(kind: str, value: dict) -> dict:
    return {f"rvk_wan_{kind}": [value], "text": [json.dumps(value, ensure_ascii=False, sort_keys=True)]}


def _entry_execution_identity(hidden) -> tuple[str, str]:
    entry_node_id = str(getattr(hidden, "unique_id", "") or "")
    extra_pnginfo = getattr(hidden, "extra_pnginfo", None)
    workflow = extra_pnginfo.get("workflow") if isinstance(extra_pnginfo, dict) else None
    workflow_id = workflow.get("id") if isinstance(workflow, dict) else None
    if not isinstance(workflow_id, str) or not workflow_id.strip() or len(workflow_id) > 256:
        raise WanLoopError("workflow identity is missing or invalid", reason="invalid_workflow_id")
    if not entry_node_id:
        raise WanLoopError("entry node identity is missing", reason="invalid_entry_node")

    prompt = getattr(hidden, "prompt", None)
    if not isinstance(prompt, dict):
        raise WanLoopError("prompt identity is missing", reason="invalid_prompt")
    entry_nodes = [
        (str(node_id), node)
        for node_id, node in prompt.items()
        if isinstance(node, dict) and node.get("class_type") == "RVKWanLoopEntry"
    ]
    if len(entry_nodes) != 1:
        raise WanLoopError(
            "a Wan loop prompt must contain exactly one RVK entry",
            reason="invalid_entry_count",
            actual=len(entry_nodes),
            expected=1,
        )
    if entry_nodes[0][0] != entry_node_id:
        raise WanLoopError(
            "the executing entry does not match the prompt entry",
            reason="entry_identity_mismatch",
            actual=entry_node_id,
            expected=entry_nodes[0][0],
        )
    return workflow_id, entry_node_id


def _linked_node_id(value, output_slot: int) -> str | None:
    if isinstance(value, (list, tuple)) and len(value) == 2 and value[1] == output_slot:
        return str(value[0])
    return None


def _directory_for_comparison(output_root: Path, output_directory: str) -> Path:
    # The writer's resolver creates directories; this preflight must only inspect paths.
    parts = _relative_directory(output_directory)
    root = output_root.resolve(strict=False)
    directory = root.joinpath(*parts).resolve(strict=False)
    if directory == root or root not in directory.parents:
        raise AssetInvalid(
            "output_directory escapes the configured output root",
            path=str(directory),
            reason="invalid_output_directory",
        )
    return directory


def _preflight_save_directory(prompt: dict, entry_node_id: str, output_root: Path, output_directory: str) -> None:
    nodes = {str(node_id): node for node_id, node in prompt.items() if isinstance(node, dict)}
    for advance_id, advance in nodes.items():
        if advance.get("class_type") != "RVKWanAdvance":
            continue
        inputs = advance.get("inputs", {})
        if not isinstance(inputs, dict) or _linked_node_id(inputs.get("plan"), 4) != entry_node_id:
            continue
        save_id = _linked_node_id(inputs.get("receipt"), 0)
        save = nodes.get(save_id, {})
        if save.get("class_type") != "RVKSaveSegmentVideo":
            continue
        save_inputs = save.get("inputs", {})
        save_directory = save_inputs.get("output_directory") if isinstance(save_inputs, dict) else None
        # Dynamic STRING links remain subject to Advance's final receipt/path validation.
        if not isinstance(save_directory, str):
            continue
        expected = _directory_for_comparison(output_root, output_directory)
        actual = _directory_for_comparison(output_root, save_directory)
        if actual != expected:
            raise WanLoopError(
                f"Save Segment Video {save_id} uses a different output directory from Loop Entry {entry_node_id} "
                f"(Advance {advance_id}). Connect Entry {entry_node_id}.output_directory to "
                f"Save {save_id}.output_directory.",
                reason="loop_output_directory_mismatch",
                actual=save_directory,
                expected=output_directory,
            )


def _workflow(hidden) -> dict:
    extra = getattr(hidden, "extra_pnginfo", None)
    value = extra.get("workflow") if isinstance(extra, dict) else None
    return value if isinstance(value, dict) else {}


def _session_id(hidden, entry_node_id: str) -> str:
    extra = _workflow(hidden).get("extra", {})
    runtime = extra.get("rvk_runtime") if isinstance(extra, dict) else None
    if not isinstance(runtime, dict):
        return ""
    if str(runtime.get("entry_node_id")) != entry_node_id:
        raise WanLoopError("continuation belongs to another Entry", reason="entry_identity_mismatch")
    session = runtime.get("session_id")
    if not isinstance(session, str) or not session or len(session) > 128:
        raise WanLoopError("invalid internal continuation session", reason="invalid_session")
    return session


def _prompt_activity():
    """Track only queue identifiers, never graph tensors or a recoverable task ID."""
    from comfy_execution.utils import get_executing_context

    context = get_executing_context()
    session = {}
    if context is None:
        return None, session, None  # Direct node calls in no-service CPU tests.
    from server import PromptServer
    queue = PromptServer.instance.prompt_queue
    prompt_id = context.prompt_id

    def active():
        running, pending = queue.get_current_queue()
        for item in (*running, *pending):
            if item[1] == prompt_id:
                return True
            extra_data = item[3]
            workflow = extra_data.get("extra_pnginfo", {}).get("workflow", {})
            runtime = workflow.get("extra", {}).get("rvk_runtime", {})
            if session.get("id") and runtime.get("session_id") == session["id"]:
                return True
        return False

    return active, session, prompt_id


def _reference_image_name(prompt: dict, entry_node_id: str) -> str | None:
    """Follow the actual Wan reference-image branch; never pick an unrelated loader."""
    nodes = {str(key): value for key, value in prompt.items() if isinstance(value, dict)}
    roots = []
    for node in nodes.values():
        inputs = node.get("inputs", {})
        if node.get("class_type") == "WanAnimate2ToVideo" and isinstance(inputs, dict):
            if (_linked_node_id(inputs.get("length"), 1) == entry_node_id
                    or _linked_node_id(inputs.get("continue_motion"), 2) == entry_node_id):
                value = inputs.get("reference_image")
                if isinstance(value, (list, tuple)) and len(value) == 2:
                    roots.append(str(value[0]))
    names, visited = set(), set()
    while roots:
        key = roots.pop()
        if key in visited:
            continue
        visited.add(key)
        node = nodes.get(key, {})
        inputs = node.get("inputs", {})
        if not isinstance(inputs, dict):
            continue
        if node.get("class_type") in {"LoadImage", "LoadImageOutput"}:
            name = inputs.get("image")
            if isinstance(name, str):
                names.add(Path(name.replace("\\", "/")).name)
            continue
        for value in inputs.values():
            if isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[1], int):
                roots.append(str(value[0]))
    return next(iter(names)) if len(names) == 1 else None


def _inspection_payload(inspection, source, *, workflow_id, entry_node_id, mode, output_directory, reference_image):
    return {
        "ok": True, "can_run": True, "completed": inspection.complete,
        "workflow_id": workflow_id, "entry_node_id": entry_node_id,
        "mode": mode, "output_directory": output_directory,
        "reference_video": source.path.name, "reference_image": reference_image,
        "recorded_video": inspection.recorded_video_name, "recorded_image": inspection.recorded_image_name,
        "completed_segments": inspection.completed_segments, "completed_frames": inspection.completed_frames,
        "total_frames": inspection.total_frames, "total_segments": inspection.total_segments,
        "segment_index": inspection.completed_segments,
        "partials": [path.name for path in inspection.partial_paths], "warnings": list(inspection.warnings),
    }


def inspect_prompt(body: dict, *, prepare_complete: bool = False) -> dict:
    """Inspect the submitted graph's standard LoadVideo without evaluating any nodes."""
    import folder_paths

    prompt = body.get("prompt") if isinstance(body, dict) else None
    workflow = body.get("workflow") if isinstance(body, dict) else None
    if not isinstance(prompt, dict) or len(prompt) > 10000 or not isinstance(workflow, dict):
        raise WanLoopError("prompt and workflow are required", reason="invalid_prompt")
    entries = [(str(key), node) for key, node in prompt.items()
               if isinstance(node, dict) and node.get("class_type") == "RVKWanLoopEntry"]
    if len(entries) != 1:
        raise WanLoopError("exactly one Entry is required", reason="invalid_entry_count")
    entry_id, entry_node = entries[0]
    hidden = SimpleNamespace(unique_id=entry_id, prompt=prompt, extra_pnginfo={"workflow": workflow})
    workflow_id, _ = _entry_execution_identity(hidden)
    inputs = entry_node.get("inputs", {})
    if not isinstance(inputs, dict):
        raise WanLoopError("Entry inputs are invalid", reason="invalid_prompt")
    video_id = _linked_node_id(inputs.get("drive_video"), 0)
    video_node = {str(key): value for key, value in prompt.items()}.get(video_id, {})
    video_inputs = video_node.get("inputs", {}) if isinstance(video_node, dict) else {}
    file = video_inputs.get("file") if isinstance(video_inputs, dict) else None
    if not isinstance(video_node, dict) or video_node.get("class_type") != "LoadVideo" or not isinstance(file, str):
        raise WanLoopError("Connect the untrimmed official Load Video directly to Entry for task inspection",
                           reason="source_filename_unavailable")
    path = Path(folder_paths.get_annotated_filepath(file)).resolve(strict=True)
    roots = [Path(getter()).resolve() for getter in
             (folder_paths.get_input_directory, folder_paths.get_output_directory, folder_paths.get_temp_directory)]
    if not any(root in path.parents for root in roots):
        raise WanLoopError("source video is outside ComfyUI media directories", reason="invalid_source_path")
    source = inspect_wan_video_source(InputImpl.VideoFromFile(str(path)))
    directory = inputs.get("output_directory")
    mode = inputs.get("mode", "new")
    length = inputs.get("segment_length", DEFAULT_SEGMENT_LENGTH)
    output_root = Path(folder_paths.get_output_directory())
    _preflight_save_directory(prompt, entry_id, output_root, directory)
    image_name = _reference_image_name(prompt, entry_id)
    def inspect():
        return inspect_run(output_root, directory, source.frame_count, source.fps, length, mode=mode)
    if prepare_complete:
        if mode != "resume":
            raise WanLoopError("completion preparation requires resume mode", reason="invalid_mode")
        WAN_RUNS.reclaim_finished_directory(_directory_for_comparison(output_root, directory))
        with RunDirectoryLease(output_root, directory):
            inspection = inspect()
            if not inspection.complete:
                raise WanLoopError("directory is no longer complete; refresh its status", reason="resume_not_complete")
            quarantine_partials(inspection)
            write_progress(inspection.directory, completed_segments=inspection.completed_segments,
                           completed_frames=inspection.completed_frames, total_frames=source.frame_count,
                           segment_length=length, reference_video=source.path.name, reference_image=image_name)
            inspection = inspect()
    else:
        inspection = inspect()
    return _inspection_payload(inspection, source, workflow_id=workflow_id, entry_node_id=entry_id,
                               mode=mode, output_directory=directory, reference_image=image_name)


class RVKWanLoopEntry(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="RVKWanLoopEntry",
            display_name="RVK Wan Animate 2 Loop Entry",
            category="RVK/Wan Animate 2",
            description=(
                "Start or continue one Wan Animate 2 segment. A new run requires an empty output directory "
                "before the editable official Motion Transfer graph can execute."
            ),
            inputs=[
                io.Video.Input("drive_video"),
                io.Int.Input(
                    "segment_length",
                    default=DEFAULT_SEGMENT_LENGTH,
                    min=MIN_SEGMENT_LENGTH,
                    max=MAX_SEGMENT_LENGTH,
                    step=4,
                ),
                io.String.Input(
                    "output_directory",
                    display_name="work_directory",
                    default="rvk/wan_animate2_new_run",
                    tooltip="Work directory relative to ComfyUI output; connect this output to Save and Finalize's segment directory.",
                ),
                io.Combo.Input("mode", options=["new", "resume"], default="new",
                               tooltip="New: empty work directory. Resume: continue from completed videos; no identity matching."),
            ],
            outputs=[
                io.Video.Output("pose_video_window"),
                io.Int.Output("length"),
                io.Image.Output("continue_motion"),
                io.Int.Output("video_frame_offset"),
                RVK_WAN_SEGMENT_PLAN.Output("plan"),
                io.String.Output("status_text"),
                io.Int.Output("segment_index"),
                io.Int.Output("delivery_frames"),
                io.Float.Output("fps"),
                io.String.Output("output_directory"),
            ],
            hidden=[io.Hidden.unique_id, io.Hidden.prompt, io.Hidden.extra_pnginfo],
            not_idempotent=True,
        )

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return float("nan")

    @classmethod
    def execute(cls, drive_video, segment_length, output_directory, mode="new") -> io.NodeOutput:
        import folder_paths

        workflow_id, entry_node_id = _entry_execution_identity(cls.hidden)
        session = _session_id(cls.hidden, entry_node_id)
        output_root = Path(folder_paths.get_output_directory())
        _preflight_save_directory(cls.hidden.prompt, entry_node_id, output_root, output_directory)
        if mode not in {"new", "resume"}:
            raise WanLoopError("select new or resume mode", reason="invalid_mode")
        if not session and mode == "new":
            directory = _directory_for_comparison(output_root, output_directory)
            if directory.exists() and (not directory.is_dir() or next(directory.iterdir(), None) is not None):
                raise AssetInvalid("new run requires an empty directory", path=str(directory),
                                   reason="output_directory_not_empty")
        source = inspect_wan_video_source(drive_video, refresh_timeline=not session)
        options = {}
        lease = None
        if not session:
            WAN_RUNS.reclaim_finished_directory(_directory_for_comparison(output_root, output_directory))
            lease = RunDirectoryLease(output_root, output_directory).acquire()
            try:
                inspection = inspect_run(output_root, output_directory, source.frame_count, source.fps,
                                         segment_length, mode=mode)
                if inspection.complete:
                    raise WanLoopError("All segments are complete; use Check/Queue to finalize without generation",
                                       reason="generation_complete")
                inspection.directory.mkdir(parents=True, exist_ok=True)
                quarantine_partials(inspection)
                image_name = _reference_image_name(cls.hidden.prompt, entry_node_id)
                def saved(status):
                    write_progress(inspection.directory, completed_segments=status.completed_segment + 1,
                                   completed_frames=status.completed_frames, total_frames=source.frame_count,
                                   segment_length=segment_length, reference_video=source.path.name,
                                   reference_image=image_name)
                write_progress(inspection.directory, completed_segments=inspection.completed_segments,
                               completed_frames=inspection.completed_frames, total_frames=source.frame_count,
                               segment_length=segment_length, reference_video=source.path.name, reference_image=image_name)
                options = dict(start_segment=inspection.completed_segments, start_offset=inspection.completed_frames,
                               continuation=read_last_frame(inspection.last_path) if inspection.last_path else None,
                               resource=lease, on_saved=saved,
                               display_info=_inspection_payload(inspection, source, workflow_id=workflow_id,
                                   entry_node_id=entry_node_id, mode=mode, output_directory=output_directory,
                                   reference_image=image_name))
            except Exception:
                lease.release()
                raise
        try:
            is_active, session_holder, prompt_id = _prompt_activity()
            entry = WAN_RUNS.enter(
                run_token=session,
                workflow_id=workflow_id,
                entry_node_id=entry_node_id,
                source_key=source.key,
                output_directory=output_directory,
                segment_length=segment_length,
                total_frames=source.frame_count,
                fps=source.fps,
                is_prompt_active=is_active,
                prompt_id=prompt_id,
                **options,
            )
            session_holder["id"] = entry.plan.run_token
        except Exception:
            if lease is not None:
                lease.release()
            raise
        plan = entry.plan
        window = ExactFrameWindowVideo(source, plan)
        payload = {
            **(entry.display_info or {}),
            "version": _FRONTEND_EVENT_VERSION,
            "kind": "entry",
            "session_id": plan.run_token,
            "workflow_id": plan.workflow_id,
            "entry_node_id": plan.entry_node_id,
            "segment_index": plan.segment_index,
            "segment_length": plan.segment_length,
            "global_start": plan.global_start,
            "delivery_frames": plan.delivery_frames,
            "total_frames": plan.total_frames,
            "output_directory": output_directory,
            "total_segments": entry.total_segments,
            "completed_segments": plan.segment_index,
            "completed_frames": 0 if plan.segment_index == 0 else plan.global_start + 1,
        }
        status_text = f"Generating segment {plan.segment_index + 1}/{entry.total_segments}"
        return io.NodeOutput(
            window,
            plan.segment_length,
            entry.continuation,
            plan.local_offset,
            plan,
            status_text,
            plan.segment_index,
            plan.delivery_frames,
            float(plan.fps),
            output_directory,
            ui=_event_payload("entry", payload),
        )


class RVKWanCollect(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="RVKWanCollect",
            display_name="RVK Wan Animate 2 Collect",
            category="RVK/Wan Animate 2",
            description=(
                "Place directly after the raw VAE decode. It removes the continuation overlap and padded tail, "
                "then clones only one raw frame for the next segment."
            ),
            inputs=[
                io.Image.Input("raw_images"),
                RVK_WAN_SEGMENT_PLAN.Input("plan"),
            ],
            outputs=[
                io.Image.Output("delivery_images"),
                RVK_WAN_CONTINUATION.Output("continuation_candidate"),
            ],
        )

    @classmethod
    def execute(cls, raw_images, plan: WanSegmentPlan) -> io.NodeOutput:
        delivery, candidate = collect_wan_segment(raw_images, plan)
        return io.NodeOutput(delivery, candidate)


class RVKWanAdvance(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="RVKWanAdvance",
            display_name="RVK Wan Animate 2 Advance",
            category="RVK/Wan Animate 2",
            description=(
                "Advance the in-memory Wan run only after RVK Save Segment Video returned a matching receipt."
            ),
            inputs=[
                RVK_VIDEO_RECEIPT.Input("receipt"),
                RVK_WAN_SEGMENT_PLAN.Input("plan"),
                RVK_WAN_CONTINUATION.Input("continuation_candidate"),
            ],
            outputs=[
                RVK_WAN_LOOP_STATUS.Output("status"),
                io.Boolean.Output("has_more"),
                io.String.Output("saved_path"),
            ],
            is_output_node=True,
            not_idempotent=True,
        )

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return float("nan")

    @classmethod
    def execute(
        cls,
        receipt: SegmentVideoReceipt,
        plan: WanSegmentPlan,
        continuation_candidate: WanContinuationCandidate,
    ) -> io.NodeOutput:
        import folder_paths

        status = WAN_RUNS.advance(
            plan=plan,
            candidate=continuation_candidate,
            receipt=receipt,
            output_root=Path(folder_paths.get_output_directory()),
        )
        payload = {"version": _FRONTEND_EVENT_VERSION, "kind": "advance", **asdict(status),
                   "session_id": status.run_token, "completed_segments": status.completed_segment + 1}
        payload.pop("run_token", None)
        return io.NodeOutput(
            status,
            status.has_more,
            status.path,
            ui=_event_payload("loop", payload),
        )


async def install_wan_loop_routes() -> None:
    """Install same-origin inspection and control endpoints without executing a graph."""

    global _ROUTES_INSTALLED
    if _ROUTES_INSTALLED:
        return
    from aiohttp import web
    from server import PromptServer

    async def control(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
        token = body.get("session_id") if isinstance(body, dict) else None
        action = body.get("action") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token or len(token) > 128 or action not in {"stop", "abort"}:
            return web.json_response({"ok": False, "error": "invalid_request"}, status=400)
        changed = WAN_RUNS.request_stop(token) if action == "stop" else WAN_RUNS.abort_after_prompt(token)
        return web.json_response({"ok": True, "changed": changed, "action": action})

    async def inspection(request, *, prepare_complete=False):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "can_run": False, "error": "invalid_json"}, status=400)
        try:
            result = await asyncio.to_thread(inspect_prompt, body, prepare_complete=prepare_complete)
        except (RVKError, OSError, ValueError, TypeError, KeyError) as exc:
            return web.json_response({"ok": False, "can_run": False, "error": str(exc),
                                      "reason": getattr(exc, "reason", "inspection_failed")}, status=400)
        return web.json_response(result)

    async def prepare_complete(request):
        return await inspection(request, prepare_complete=True)

    await ComfyAPI().caching.register_provider(_LIFECYCLE)
    WAN_RUNS.start_janitor()
    PromptServer.instance.routes.post("/rvk/wan-loop/control")(control)
    PromptServer.instance.routes.post("/rvk/wan-loop/inspect")(inspection)
    PromptServer.instance.routes.post("/rvk/wan-loop/prepare-complete")(prepare_complete)
    _ROUTES_INSTALLED = True


__all__ = [
    "RVKWanAdvance",
    "RVKWanCollect",
    "RVKWanLoopEntry",
    "RVK_WAN_CONTINUATION",
    "RVK_WAN_LOOP_STATUS",
    "RVK_WAN_SEGMENT_PLAN",
    "WAN_RUNS",
    "install_wan_loop_routes",
]
