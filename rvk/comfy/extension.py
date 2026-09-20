"""Combined ComfyUI extension for the current RVK nodes."""

from __future__ import annotations

from typing_extensions import override

from rvk.comfy.final_video import RVKFinalizeSegments
from rvk.comfy.segment_video import RVKSaveSegmentVideo, SegmentVideoExtension
from rvk.comfy.wan_loop import RVKWanAdvance, RVKWanCollect, RVKWanLoopEntry, install_wan_loop_routes


class RVKExtension(SegmentVideoExtension):
    async def on_load(self) -> None:
        await install_wan_loop_routes()

    @override
    async def get_node_list(self):
        return [RVKSaveSegmentVideo, RVKFinalizeSegments, RVKWanLoopEntry, RVKWanCollect, RVKWanAdvance]


__all__ = ["RVKExtension"]
