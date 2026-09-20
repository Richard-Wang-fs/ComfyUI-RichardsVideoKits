"""ComfyUI V3 node for the generic one-segment video writer."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from comfy_api.latest import ComfyExtension, io
from typing_extensions import override

from rvk.segment_video import ALLOWED_PRESETS, SegmentVideoReceipt, save_segment_video


RVK_VIDEO_RECEIPT = io.Custom("RVK_VIDEO_RECEIPT")


class RVKSaveSegmentVideo(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="RVKSaveSegmentVideo",
            display_name="RVK Save Segment Video",
            category="RVK/Video",
            description="Save the current IMAGE batch as one verified silent MP4 without overwriting an existing segment.",
            inputs=[
                io.Image.Input("images"),
                io.Float.Input("fps", default=24.0, min=0.001, max=1000.0, step=0.001),
                io.String.Input("output_directory", default="rvk/run", tooltip="Relative to the ComfyUI output directory."),
                io.Int.Input("segment_index", default=0, min=0, max=999_999),
                io.Int.Input("crf", default=23, min=0, max=51),
                io.Combo.Input("preset", options=list(ALLOWED_PRESETS), default="medium"),
            ],
            outputs=[
                RVK_VIDEO_RECEIPT.Output("receipt"),
                io.String.Output("path"),
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
        images,
        fps,
        output_directory,
        segment_index,
        crf,
        preset,
    ) -> io.NodeOutput:
        import folder_paths

        receipt = save_segment_video(
            images,
            output_root=Path(folder_paths.get_output_directory()),
            output_directory=output_directory,
            segment_index=segment_index,
            fps=fps,
            crf=crf,
            preset=preset,
        )
        summary = json.dumps(asdict(receipt), ensure_ascii=False, sort_keys=True)
        return io.NodeOutput(receipt, receipt.path, ui={"text": [summary]})


class SegmentVideoExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [RVKSaveSegmentVideo]


__all__ = [
    "RVKSaveSegmentVideo",
    "RVK_VIDEO_RECEIPT",
    "SegmentVideoExtension",
    "SegmentVideoReceipt",
]
