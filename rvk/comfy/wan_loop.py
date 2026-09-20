"""ComfyUI V3 nodes for the Wan Animate 2 per-prompt loop."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from comfy_api.latest import io

from rvk.comfy.wan_video import ExactFrameWindowVideo, inspect_wan_video_source
from rvk.segment_video import SegmentVideoReceipt, preflight_new_segment_run
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
_FRONTEND_EVENT_VERSION = 2


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
                    default="rvk/wan_animate2_new_run",
                    tooltip="Fresh run directory relative to the ComfyUI output directory; connect the output to Save and Finalize.",
                ),
                io.String.Input(
                    "run_token",
                    default="",
                    tooltip="Managed by the RVK frontend. Clear it to start a new run.",
                ),
            ],
            outputs=[
                io.Video.Output("pose_video_window"),
                io.Int.Output("length"),
                io.Image.Output("continue_motion"),
                io.Int.Output("video_frame_offset"),
                RVK_WAN_SEGMENT_PLAN.Output("plan"),
                io.String.Output("run_token"),
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
    def execute(cls, drive_video, segment_length, output_directory, run_token) -> io.NodeOutput:
        import folder_paths

        token_input = run_token.strip() if isinstance(run_token, str) else ""
        workflow_id, entry_node_id = _entry_execution_identity(cls.hidden)
        if not token_input:
            preflight_new_segment_run(Path(folder_paths.get_output_directory()), output_directory)
        source = inspect_wan_video_source(drive_video, refresh_timeline=not token_input)
        entry = WAN_RUNS.enter(
            run_token=run_token,
            workflow_id=workflow_id,
            entry_node_id=entry_node_id,
            source_key=source.key,
            output_directory=output_directory,
            segment_length=segment_length,
            total_frames=source.frame_count,
            fps=source.fps,
        )
        plan = entry.plan
        window = ExactFrameWindowVideo(source, plan)
        payload = {
            "version": _FRONTEND_EVENT_VERSION,
            "kind": "entry",
            "run_token": plan.run_token,
            "submitted_run_token": token_input,
            "workflow_id": plan.workflow_id,
            "entry_node_id": plan.entry_node_id,
            "segment_index": plan.segment_index,
            "segment_length": plan.segment_length,
            "global_start": plan.global_start,
            "delivery_frames": plan.delivery_frames,
            "total_frames": plan.total_frames,
            "output_directory": output_directory,
        }
        return io.NodeOutput(
            window,
            plan.segment_length,
            entry.continuation,
            plan.local_offset,
            plan,
            plan.run_token,
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
        payload = {"version": _FRONTEND_EVENT_VERSION, "kind": "advance", **asdict(status)}
        return io.NodeOutput(
            status,
            status.has_more,
            status.path,
            ui=_event_payload("loop", payload),
        )


async def install_wan_loop_routes() -> None:
    """Install the same-origin Stop/abort endpoint when ComfyUI loads the extension."""

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
        token = body.get("run_token") if isinstance(body, dict) else None
        action = body.get("action") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token or action not in {"stop", "abort"}:
            return web.json_response({"ok": False, "error": "invalid_request"}, status=400)
        changed = WAN_RUNS.request_stop(token) if action == "stop" else WAN_RUNS.abort(token)
        return web.json_response({"ok": True, "changed": changed, "action": action})

    WAN_RUNS.start_janitor()
    PromptServer.instance.routes.post("/rvk/wan-loop/control")(control)
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
