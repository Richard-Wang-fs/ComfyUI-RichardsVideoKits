"""ComfyUI V3 node for finalizing existing RVK segment videos."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from comfy_api.latest import InputImpl, io

from rvk.errors import AssetInvalid
from rvk.final_video import FinalVideoResult, finalize_segment_videos, resolve_run_directory
from rvk.source_video import inspect_source_video_timeline
from rvk.wan_loop import WanLoopStatus


RVK_FINAL_VIDEO_RESULT = io.Custom("RVK_FINAL_VIDEO_RESULT")
RVK_WAN_LOOP_STATUS = io.Custom("RVK_WAN_LOOP_STATUS")


def _source_video_details(video) -> tuple[Path, int, object]:
    if not isinstance(video, InputImpl.VideoFromFile):
        raise AssetInvalid(
            "finalization requires an official file-backed VIDEO input",
            reason="unsupported_video_source",
            actual=type(video).__name__,
        )
    start_time, duration = video.get_active_trim_window()
    if start_time != 0.0 or duration != 0.0:
        raise AssetInvalid(
            "connect the untrimmed Load Video output",
            reason="pretrimmed_video_not_supported",
            actual={"start_time": start_time, "duration": duration},
        )
    source = video.get_stream_source()
    if not isinstance(source, (str, Path)):
        raise AssetInvalid("in-memory VIDEO sources are not supported", reason="unsupported_video_source")
    try:
        timeline = inspect_source_video_timeline(source)
    except AssetInvalid:
        raise
    except Exception as exc:
        raise AssetInvalid(
            "source video timeline cannot be read",
            reason="source_video_unavailable",
            actual=str(source),
        ) from exc
    return timeline.path, timeline.frame_count, timeline.fps


class RVKFinalizeSegments(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="RVKFinalizeSegments",
            display_name="RVK Finalize Segments",
            category="RVK/Video",
            description=(
                "Combine a complete segment_0000.mp4 sequence and add audio from the untrimmed source VIDEO. "
                "Connect loop_status for automatic finalization, or leave it unconnected to finalize existing segments after restart."
            ),
            inputs=[
                io.Video.Input("source_video"),
                io.String.Input(
                    "output_directory",
                    default="rvk/run",
                    tooltip="Existing segment directory relative to the ComfyUI output directory.",
                ),
                io.String.Input("final_filename", default="final.mp4"),
                RVK_WAN_LOOP_STATUS.Input("loop_status", optional=True),
            ],
            outputs=[
                RVK_FINAL_VIDEO_RESULT.Output("result"),
                io.String.Output("path"),
                io.Boolean.Output("completed"),
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
        source_video,
        output_directory,
        final_filename,
        loop_status: WanLoopStatus | None = None,
    ) -> io.NodeOutput:
        expected_segment_count = None
        if loop_status is not None:
            if not isinstance(loop_status, WanLoopStatus):
                raise AssetInvalid(
                    "loop_status is not an RVK Wan completion status",
                    reason="invalid_loop_status",
                    actual=type(loop_status).__name__,
                )
            if loop_status.has_more:
                result = FinalVideoResult.waiting(
                    f"Waiting for segment {loop_status.next_segment}; {loop_status.completed_frames}/{loop_status.total_frames} frames saved."
                )
                return cls._output(result)
            if loop_status.stopped:
                result = FinalVideoResult.waiting(
                    f"Loop stopped after segment {loop_status.completed_segment}; finalization was not started."
                )
                return cls._output(result)
            if loop_status.completed_frames != loop_status.total_frames:
                raise AssetInvalid(
                    "completed loop status does not cover the source video",
                    reason="incomplete_loop_status",
                    actual=loop_status.completed_frames,
                    expected=loop_status.total_frames,
                )
            expected_segment_count = loop_status.completed_segment + 1

        source_path, source_frame_count, source_fps = _source_video_details(source_video)
        if loop_status is not None and loop_status.total_frames != source_frame_count:
            raise AssetInvalid(
                "loop status belongs to a different source video length",
                reason="loop_source_mismatch",
                actual=loop_status.total_frames,
                expected=source_frame_count,
            )

        import folder_paths
        from comfy.model_management import throw_exception_if_processing_interrupted

        output_root = Path(folder_paths.get_output_directory())
        if loop_status is not None:
            directory = resolve_run_directory(output_root, output_directory)
            try:
                saved = Path(loop_status.path).resolve(strict=True)
            except OSError as exc:
                raise AssetInvalid(
                    "loop status segment cannot be resolved",
                    reason="loop_output_mismatch",
                    actual=loop_status.path,
                ) from exc
            expected_name = f"segment_{loop_status.completed_segment:04d}.mp4"
            if saved.parent != directory or saved.name != expected_name:
                raise AssetInvalid(
                    "loop status points to a different segment directory",
                    reason="loop_output_mismatch",
                    actual=str(saved),
                    expected=str(directory / expected_name),
                )

        result = finalize_segment_videos(
            output_root=output_root,
            output_directory=output_directory,
            source_video_path=source_path,
            source_frame_count=source_frame_count,
            source_fps=source_fps,
            final_filename=final_filename,
            expected_segment_count=expected_segment_count,
            cancel_check=throw_exception_if_processing_interrupted,
        )
        return cls._output(result)

    @staticmethod
    def _output(result: FinalVideoResult) -> io.NodeOutput:
        summary = json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
        return io.NodeOutput(result, result.path, result.completed, ui={"text": [summary]})


__all__ = ["RVKFinalizeSegments", "RVK_FINAL_VIDEO_RESULT", "FinalVideoResult"]
