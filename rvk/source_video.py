"""Constant-memory inspection of a file-backed source video timeline."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from rvk.errors import AssetInvalid, EncoderUnavailable
from rvk.video_codec import MAX_IMAGE_DIMENSION, MAX_IMAGE_PIXELS, av_module


@dataclass(frozen=True, slots=True)
class SourceVideoTimeline:
    path: Path
    file_key: str
    frame_count: int
    width: int
    height: int
    fps: Fraction
    time_base: Fraction
    first_pts: int
    pts_step: int


def resolve_source_video_path(source: str | Path) -> tuple[Path, str]:
    """Resolve one regular file and return its lightweight change key."""

    try:
        path = Path(source).resolve(strict=True)
        stat = path.stat()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise AssetInvalid(
            "source video cannot be resolved",
            reason="source_video_unavailable",
            actual=str(source),
        ) from exc
    if not path.is_file():
        raise AssetInvalid(
            "source video is not a regular file",
            path=str(path),
            reason="source_video_unavailable",
        )
    return path, f"{path}|{stat.st_size}|{stat.st_mtime_ns}"


def frame_pts_in_time_base(frame, time_base: Fraction, *, path: Path) -> int:
    """Normalize a decoded frame PTS to the selected stream time base."""

    if frame.pts is None or frame.time_base is None:
        raise AssetInvalid(
            "decoded source video frame has no comparable timestamp",
            path=str(path),
            reason="source_video_timing_invalid",
        )
    normalized = Fraction(int(frame.pts)) * Fraction(frame.time_base) / time_base
    if normalized.denominator != 1:
        raise AssetInvalid(
            "decoded source video timestamp is not integral in its stream time base",
            path=str(path),
            reason="source_video_timing_invalid",
            actual=str(normalized),
        )
    return normalized.numerator


def _display_dimensions(frame) -> tuple[int, int]:
    width, height = int(frame.width), int(frame.height)
    rotation = float(frame.rotation or 0.0)
    quadrant = round(rotation / 90.0)
    if abs(rotation - quadrant * 90.0) > 1e-6:
        raise AssetInvalid(
            "source video has unsupported non-quadrant rotation",
            reason="source_video_dimensions_invalid",
            actual=rotation,
        )
    if quadrant % 2:
        width, height = height, width
    return width, height


def inspect_source_video_timeline(source: str | Path) -> SourceVideoTimeline:
    """Decode all video frames while retaining only constant-size timeline state."""

    path, initial_key = resolve_source_video_path(source)
    av = av_module()
    try:
        with av.open(str(path), mode="r") as container:
            if len(container.streams.video) != 1:
                raise AssetInvalid(
                    "source must contain exactly one video stream",
                    path=str(path),
                    reason="unsupported_video_streams",
                    actual=len(container.streams.video),
                    expected=1,
                )
            stream = container.streams.video[0]
            if stream.time_base is None:
                raise AssetInvalid(
                    "source video stream has no time base",
                    path=str(path),
                    reason="source_video_timing_invalid",
                )
            time_base = Fraction(stream.time_base)
            if time_base <= 0:
                raise AssetInvalid(
                    "source video stream time base is invalid",
                    path=str(path),
                    reason="source_video_timing_invalid",
                    actual=str(time_base),
                )

            count = 0
            width = height = 0
            first_pts: int | None = None
            previous_pts: int | None = None
            pts_step: int | None = None
            for frame in container.decode(stream):
                current_pts = frame_pts_in_time_base(frame, time_base, path=path)
                current_width, current_height = _display_dimensions(frame)
                if (
                    current_width < 1
                    or current_height < 1
                    or current_width > MAX_IMAGE_DIMENSION
                    or current_height > MAX_IMAGE_DIMENSION
                    or current_width * current_height > MAX_IMAGE_PIXELS
                ):
                    raise AssetInvalid(
                        "source video dimensions are invalid or exceed the media limit",
                        path=str(path),
                        reason="source_video_dimensions_invalid",
                        actual=(current_width, current_height),
                    )
                if count and (current_width, current_height) != (width, height):
                    raise AssetInvalid(
                        "decoded source video dimensions change between frames",
                        path=str(path),
                        reason="source_video_dimensions_changed",
                        actual=(current_width, current_height),
                        expected=(width, height),
                    )
                if previous_pts is not None:
                    current_step = current_pts - previous_pts
                    if current_step <= 0:
                        raise AssetInvalid(
                            "source video timestamps must increase strictly",
                            path=str(path),
                            reason="source_video_timing_invalid",
                            actual=current_pts,
                            expected=f"> {previous_pts}",
                        )
                    if pts_step is None:
                        pts_step = current_step
                    elif current_step != pts_step:
                        raise AssetInvalid(
                            "source video is not constant frame rate",
                            path=str(path),
                            reason="source_video_not_cfr",
                            actual={"frame": count, "pts": current_pts, "step": current_step},
                            expected=pts_step,
                        )
                else:
                    first_pts = current_pts
                count += 1
                width, height = current_width, current_height
                previous_pts = current_pts

            if count == 0 or first_pts is None:
                raise AssetInvalid(
                    "source video contains no decodable frames",
                    path=str(path),
                    reason="source_video_no_frames",
                )
            if pts_step is None:
                if stream.average_rate is None:
                    raise AssetInvalid(
                        "a single-frame source requires a declared frame rate",
                        path=str(path),
                        reason="source_video_timing_invalid",
                    )
                declared_fps = Fraction(stream.average_rate)
                if declared_fps <= 0:
                    raise AssetInvalid(
                        "source video frame rate is invalid",
                        path=str(path),
                        reason="source_video_timing_invalid",
                        actual=str(declared_fps),
                    )
                step = Fraction(declared_fps.denominator, declared_fps.numerator) / time_base
                if step.denominator != 1 or step <= 0:
                    raise AssetInvalid(
                        "source video time base cannot represent its declared frame rate exactly",
                        path=str(path),
                        reason="source_video_timing_invalid",
                        actual={"fps": str(declared_fps), "time_base": str(time_base)},
                    )
                pts_step = step.numerator

            fps = Fraction(1, 1) / (pts_step * time_base)
    except (AssetInvalid, EncoderUnavailable):
        raise
    except Exception as exc:
        if isinstance(exc, (OSError, getattr(av.error, "FFmpegError", ()))):
            raise AssetInvalid(
                "source video cannot be fully decoded",
                path=str(path),
                reason="source_video_decode_failed",
                actual=str(exc),
            ) from exc
        raise

    _, final_key = resolve_source_video_path(path)
    if final_key != initial_key:
        raise AssetInvalid(
            "source video changed while its timeline was being inspected",
            path=str(path),
            reason="source_video_changed",
            actual=final_key,
            expected=initial_key,
        )
    return SourceVideoTimeline(path, initial_key, count, width, height, fps, time_base, first_pts, pts_step)


__all__ = [
    "SourceVideoTimeline",
    "frame_pts_in_time_base",
    "inspect_source_video_timeline",
    "resolve_source_video_path",
]
