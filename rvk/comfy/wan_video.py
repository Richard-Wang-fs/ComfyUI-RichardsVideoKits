"""Exact frame-index VIDEO windows for the Wan loop entry node."""

from __future__ import annotations

import io
import threading
from collections import OrderedDict
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO

import numpy as np
import torch
from comfy_api.latest import Input, InputImpl, Types

from rvk.errors import RVKError
from rvk.source_video import (
    SourceVideoTimeline,
    frame_pts_in_time_base,
    inspect_source_video_timeline,
    resolve_source_video_path,
)
from rvk.wan_loop import WanLoopError, WanSegmentPlan


_TIMELINE_CACHE_LIMIT = 8
_TIMELINE_CACHE: OrderedDict[str, SourceVideoTimeline] = OrderedDict()
_TIMELINE_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class WanVideoSource:
    path: Path
    key: str
    frame_count: int
    fps: Fraction
    width: int
    height: int
    time_base: Fraction
    first_pts: int
    pts_step: int


def _cached_timeline(path: Path, key: str, *, refresh: bool) -> SourceVideoTimeline:
    if not refresh:
        with _TIMELINE_CACHE_LOCK:
            cached = _TIMELINE_CACHE.get(key)
            if cached is not None:
                _TIMELINE_CACHE.move_to_end(key)
                return cached
    timeline = inspect_source_video_timeline(path)
    if timeline.file_key != key:
        raise WanLoopError("video source changed during inspection", reason="video_source_changed")
    with _TIMELINE_CACHE_LOCK:
        _TIMELINE_CACHE[key] = timeline
        _TIMELINE_CACHE.move_to_end(key)
        while len(_TIMELINE_CACHE) > _TIMELINE_CACHE_LIMIT:
            _TIMELINE_CACHE.popitem(last=False)
    return timeline


def inspect_wan_video_source(video: Input.Video, *, refresh_timeline: bool = True) -> WanVideoSource:
    """Resolve an untrimmed official file VIDEO without materializing its frames."""

    if not isinstance(video, InputImpl.VideoFromFile):
        raise WanLoopError(
            "Wan loop currently requires an official file-backed VIDEO input",
            reason="unsupported_video_source",
            actual=type(video).__name__,
        )
    start_time, duration = video.get_active_trim_window()
    if start_time != 0.0 or duration != 0.0:
        raise WanLoopError(
            "connect the untrimmed Load Video output; RVK applies exact frame windows",
            reason="pretrimmed_video_not_supported",
            actual={"start_time": start_time, "duration": duration},
        )
    source = video.get_stream_source()
    if not isinstance(source, (str, Path)):
        raise WanLoopError(
            "in-memory video sources are not supported by the exact window path",
            reason="unsupported_video_source",
        )
    try:
        path, key = resolve_source_video_path(source)
        timeline = _cached_timeline(path, key, refresh=refresh_timeline)
    except RVKError as exc:
        raise WanLoopError(
            "video source timeline cannot be verified",
            path=exc.path,
            reason=exc.reason,
            actual=exc.actual,
            expected=exc.expected,
        ) from exc
    return WanVideoSource(
        timeline.path,
        timeline.file_key,
        timeline.frame_count,
        timeline.fps,
        timeline.width,
        timeline.height,
        timeline.time_base,
        timeline.first_pts,
        timeline.pts_step,
    )


class ExactFrameWindowVideo(Input.Video):
    """A lazy VIDEO that decodes only one exact CFR frame window and no audio."""

    def __init__(self, source: WanVideoSource, plan: WanSegmentPlan) -> None:
        if source.key != plan.source_key or source.frame_count != plan.total_frames or source.fps != plan.fps:
            raise WanLoopError("window source does not match the segment plan", reason="video_source_changed")
        self._source = source
        self._plan = plan

    def get_components(self) -> Types.VideoComponents:
        try:
            import av

            _, current_key = resolve_source_video_path(self._source.path)
            if current_key != self._source.key:
                raise WanLoopError("video source changed after planning", reason="video_source_changed")
            with av.open(str(self._source.path), mode="r") as container:
                if len(container.streams.video) != 1:
                    raise WanLoopError(
                        "video source must contain exactly one video stream",
                        reason="unsupported_video_streams",
                    )
                stream = container.streams.video[0]
                if stream.time_base is None:
                    raise WanLoopError("video timing metadata is incomplete", reason="video_metadata_unavailable")
                actual_time_base = Fraction(stream.time_base)
                if actual_time_base != self._source.time_base:
                    raise WanLoopError(
                        "video time base changed after planning",
                        reason="video_source_changed",
                        actual=str(actual_time_base),
                        expected=str(self._source.time_base),
                    )
                step = self._source.pts_step
                target_pts = self._source.first_pts + self._plan.global_start * step
                container.seek(target_pts, stream=stream, backward=True, any_frame=False)
                frames: list[torch.Tensor] = []
                converted_pts: list[int] = []
                for frame in container.decode(stream):
                    try:
                        frame_pts = frame_pts_in_time_base(frame, actual_time_base, path=self._source.path)
                    except RVKError as exc:
                        raise WanLoopError(
                            "decoded window timestamp is invalid",
                            reason=exc.reason,
                            actual=exc.actual,
                            expected=exc.expected,
                        ) from exc
                    if frame_pts < target_pts:
                        continue
                    expected_pts = target_pts + len(frames) * step
                    if frame_pts != expected_pts:
                        raise WanLoopError(
                            "video is not exact CFR in the requested frame window",
                            reason="non_cfr_window",
                            actual=frame_pts,
                            expected=expected_pts,
                        )
                    array = frame.to_ndarray(format="rgb24")
                    rotation = int(round(frame.rotation // 90)) % 4 if frame.rotation else 0
                    if rotation:
                        array = np.rot90(array, k=rotation, axes=(0, 1)).copy()
                    frames.append(torch.from_numpy(np.ascontiguousarray(array)).to(dtype=torch.float32).div_(255.0))
                    converted_pts.append(frame_pts)
                    if len(frames) == self._plan.valid_raw_frames:
                        break
                if len(frames) != self._plan.valid_raw_frames:
                    raise WanLoopError(
                        "video ended before the planned frame window was decoded",
                        reason="short_video_window",
                        actual=len(frames),
                        expected=self._plan.valid_raw_frames,
                    )
                images = torch.stack(frames)
                if images.shape[1:3] != (self._source.height, self._source.width):
                    raise WanLoopError(
                        "decoded video dimensions changed",
                        reason="video_source_changed",
                        actual=tuple(images.shape[1:3]),
                        expected=(self._source.height, self._source.width),
                    )
                padding = self._plan.segment_length - self._plan.valid_raw_frames
                if padding:
                    images = torch.cat((images, images[-1:].expand(padding, -1, -1, -1)), dim=0).contiguous()
                metadata = {
                    "rvk_exact_window": {
                        "global_start": self._plan.global_start,
                        "valid_raw_frames": self._plan.valid_raw_frames,
                        "output_frames": self._plan.segment_length,
                        "converted_pts": converted_pts,
                    }
                }
                return Types.VideoComponents(images=images, frame_rate=self._plan.fps, audio=None, metadata=metadata)
        except WanLoopError:
            raise
        except Exception as exc:
            raise WanLoopError(
                "exact video window decoding failed",
                reason="video_window_decode_failed",
                actual=str(exc),
            ) from exc

    def save_to(
        self,
        path: str | BinaryIO,
        format: Types.VideoContainer = Types.VideoContainer.AUTO,
        codec: Types.VideoCodec = Types.VideoCodec.AUTO,
        metadata: dict | None = None,
        bit_depth: int | None = None,
        crf: float | None = None,
        color_space: str | None = None,
        preset: str | None = None,
    ):
        video = InputImpl.VideoFromComponents(self.get_components(), bit_depth=8)
        return video.save_to(
            path,
            format=format,
            codec=codec,
            metadata=metadata,
            bit_depth=bit_depth,
            crf=crf,
            color_space=color_space,
            preset=preset,
        )

    def as_trimmed(
        self,
        start_time: float | None = None,
        duration: float | None = None,
        strict_duration: bool = False,
    ) -> Input.Video | None:
        if (start_time in (None, 0, 0.0)) and (duration in (None, 0, 0.0)):
            return self
        raise WanLoopError(
            "an RVK exact frame window cannot be time-trimmed again",
            reason="window_retrim_not_supported",
        )

    def get_dimensions(self) -> tuple[int, int]:
        return self._source.width, self._source.height

    def get_bit_depth(self) -> int:
        return 8

    def get_color_space(self) -> str:
        return "sRGB"

    def get_duration(self) -> float:
        return float(Fraction(self._plan.segment_length, 1) / self._plan.fps)

    def get_frame_count(self) -> int:
        return self._plan.segment_length

    def get_frame_rate(self) -> Fraction:
        return self._plan.fps

    def get_container_format(self) -> str:
        return self._source.path.suffix.lstrip(".").lower()

    def get_stream_source(self) -> str | io.BytesIO:
        buffer = io.BytesIO()
        self.save_to(buffer)
        buffer.seek(0)
        return buffer


__all__ = ["ExactFrameWindowVideo", "WanVideoSource", "inspect_wan_video_source"]
