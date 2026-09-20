"""Generic no-overwrite writer for one delivery-video segment."""

from __future__ import annotations

import errno
import math
import os
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path, PurePosixPath, PureWindowsPath

import torch

from rvk.errors import AssetInvalid, EncoderUnavailable, NoProgress, StorageFull
from rvk.video_codec import (
    ALLOWED_PRESETS,
    VIDEO_CODEC,
    VIDEO_PIXEL_FORMAT,
    encode_video_file,
    inspect_video_file,
    validate_encoder_options,
    validate_frame_batch,
)


MAX_SEGMENT_INDEX = 999_999
MAX_FPS = Fraction(1000, 1)


@dataclass(frozen=True, slots=True)
class SegmentVideoReceipt:
    """Lightweight evidence that one final video path was published."""

    path: str
    segment_index: int
    frame_count: int
    width: int
    height: int
    fps_num: int
    fps_den: int
    crf: int
    preset: str

    @property
    def fps(self) -> Fraction:
        return Fraction(self.fps_num, self.fps_den)


def fps_fraction(value: Fraction | float | int) -> Fraction:
    """Resolve a finite positive UI fps value to a bounded rational."""

    if isinstance(value, bool):
        raise AssetInvalid("fps must be a positive number", reason="invalid_fps", actual=value)
    if isinstance(value, Fraction):
        result = value
    elif isinstance(value, int):
        result = Fraction(value, 1)
    elif isinstance(value, float) and math.isfinite(value):
        result = Fraction(str(value)).limit_denominator(1_000_000)
    else:
        raise AssetInvalid("fps must be a finite positive number", reason="invalid_fps", actual=value)
    if result <= 0 or result > MAX_FPS:
        raise AssetInvalid(
            "fps is outside the supported range",
            reason="invalid_fps",
            actual=str(result),
            expected={"minimum_exclusive": 0, "maximum": str(MAX_FPS)},
        )
    return result


def _integer(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise AssetInvalid(
            f"{name} is outside the supported range",
            reason=f"invalid_{name}",
            actual=value,
            expected={"minimum": minimum, "maximum": maximum},
        )
    return value


def _relative_directory(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AssetInvalid(
            "output_directory must name a relative run directory",
            reason="invalid_output_directory",
        )
    windows = PureWindowsPath(value)
    normalized = value.replace("\\", "/")
    posix = PurePosixPath(normalized)
    parts = posix.parts
    if (
        windows.drive
        or windows.root
        or posix.is_absolute()
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise AssetInvalid(
            "output_directory must be relative and cannot traverse parents",
            reason="invalid_output_directory",
            actual=value,
        )
    return tuple(parts)


def _segment_paths(
    output_root: Path,
    output_directory: str,
    segment_index: int,
) -> tuple[Path, Path]:
    resolved_directory = resolve_segment_output_directory(output_root, output_directory)
    stem = f"segment_{segment_index:04d}"
    return resolved_directory / f"{stem}.partial.mp4", resolved_directory / f"{stem}.mp4"


def expected_segment_video_path(
    output_root: Path,
    output_directory: str,
    segment_index: int,
) -> Path:
    """Resolve the one formal segment path allowed for a run/index pair."""

    index = _integer(segment_index, name="segment_index", minimum=0, maximum=MAX_SEGMENT_INDEX)
    return _segment_paths(output_root, output_directory, index)[1]


def resolve_segment_output_directory(output_root: Path, output_directory: str) -> Path:
    """Create and resolve one run directory below the configured output root."""

    root = Path(output_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        _raise_io(exc, root, "create_output_root")
    if not resolved_root.is_dir():
        raise AssetInvalid(
            "output root is not a directory",
            path=str(resolved_root),
            reason="invalid_output_root",
        )
    directory = resolved_root.joinpath(*_relative_directory(output_directory))
    try:
        candidate = directory.resolve(strict=False)
    except OSError as exc:
        _raise_io(exc, directory, "resolve_output_directory")
    if candidate == resolved_root or resolved_root not in candidate.parents:
        raise AssetInvalid(
            "output_directory escapes the configured output root",
            path=str(candidate),
            reason="invalid_output_directory",
        )
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        resolved_directory = candidate.resolve(strict=True)
    except OSError as exc:
        _raise_io(exc, candidate, "create_output_directory")
    if resolved_root not in resolved_directory.parents or not resolved_directory.is_dir():
        raise AssetInvalid(
            "output directory is outside the configured output root",
            path=str(resolved_directory),
            reason="invalid_output_directory",
        )
    return resolved_directory


def preflight_new_segment_run(output_root: Path, output_directory: str) -> Path:
    """Require an empty run directory before expensive generation starts."""

    directory = resolve_segment_output_directory(output_root, output_directory)
    try:
        existing = next(directory.iterdir(), None)
    except OSError as exc:
        _raise_io(exc, directory, "inspect_output_directory")
    if existing is not None:
        raise AssetInvalid(
            "new segment run requires an empty output directory; choose a new directory",
            path=str(directory),
            reason="output_directory_not_empty",
            actual=existing.name,
            expected="empty_directory",
        )
    return directory


def _raise_io(exc: OSError, path: Path, stage: str) -> None:
    winerror = getattr(exc, "winerror", None)
    if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)} or winerror == 112:
        raise StorageFull(
            "segment video storage is full",
            path=str(path),
            reason="segment_storage_full",
            actual=stage,
        ) from exc
    if isinstance(exc, FileExistsError) or exc.errno == errno.EEXIST or winerror in {80, 183}:
        raise AssetInvalid(
            "segment video path already exists",
            path=str(path),
            reason="segment_exists",
            actual=stage,
        ) from exc
    if winerror in {32, 33}:
        reason = "storage_sharing_violation"
    elif exc.errno in {errno.EACCES, errno.EPERM}:
        reason = "storage_access_denied"
    else:
        reason = "segment_storage_io_failed"
    raise AssetInvalid(
        "segment video storage operation failed",
        path=str(path),
        reason=reason,
        actual=stage,
    ) from exc


def _validate_encoded_video(
    path: Path,
    *,
    frame_count: int,
    width: int,
    height: int,
    fps: Fraction,
    crf: int,
    preset: str,
) -> None:
    actual = inspect_video_file(path)
    step = Fraction(fps.denominator, fps.numerator) / actual.time_base
    expected_pts = () if step.denominator != 1 else tuple(index * step.numerator for index in range(frame_count))
    fields_match = (
        actual.frame_count == frame_count
        and actual.width == width
        and actual.height == height
        and actual.fps == fps
        and "mp4" in actual.container.split(",")
        and actual.codec == VIDEO_CODEC
        and actual.pixel_format == VIDEO_PIXEL_FORMAT
        and (actual.color_primaries, actual.color_transfer, actual.color_matrix, actual.color_range) == (1, 1, 1, 1)
        and actual.audio_streams == 0
        and step.denominator == 1
        and actual.pts == expected_pts
        and abs(actual.duration - frame_count * step.numerator) <= 1
    )
    if not fields_match:
        raise AssetInvalid(
            "encoded segment video differs from the requested settings",
            path=str(path),
            reason="media_content_mismatch",
            actual={
                "frame_count": actual.frame_count,
                "dimensions": (actual.width, actual.height),
                "fps": str(actual.fps),
                "time_base": str(actual.time_base),
                "pts": actual.pts,
                "duration": actual.duration,
                "container": actual.container,
                "codec": actual.codec,
                "pixel_format": actual.pixel_format,
                "color": (
                    actual.color_primaries,
                    actual.color_transfer,
                    actual.color_matrix,
                    actual.color_range,
                ),
                "audio_streams": actual.audio_streams,
            },
        )
    validate_encoder_options(path, crf=crf, preset=preset)


def _publish_no_replace(partial_path: Path, final_path: Path) -> None:
    """Atomically add a final hard link; unlike replace, this cannot overwrite."""

    try:
        os.link(partial_path, final_path)
    except OSError as exc:
        _raise_io(exc, final_path, "publish")
    try:
        os.unlink(partial_path)
    except OSError as exc:
        try:
            same_file = os.path.samefile(partial_path, final_path)
        except OSError as identity_exc:
            raise AssetInvalid(
                "segment video was published but cleanup failed and file identity is uncertain",
                path=str(partial_path),
                reason="segment_publish_uncertain",
                actual={"published_path": str(final_path), "cleanup_error": str(exc)},
                expected="partial and formal paths must identify the same published file",
            ) from identity_exc
        if not same_file:
            raise AssetInvalid(
                "segment video was published but cleanup failed and the paths no longer identify the same file",
                path=str(partial_path),
                reason="segment_publish_uncertain",
                actual={"published_path": str(final_path), "cleanup_error": str(exc)},
                expected="partial and formal paths must identify the same published file",
            ) from exc
        raise AssetInvalid(
            "segment video was published, but its partial hard link could not be removed; remove the partial before continuing",
            path=str(partial_path),
            reason="segment_cleanup_required",
            actual={"published_path": str(final_path), "same_file": True, "cleanup_error": str(exc)},
            expected="remove the partial path while preserving the published video",
        ) from exc


def save_segment_video(
    images: torch.Tensor,
    *,
    output_root: Path,
    output_directory: str,
    segment_index: int,
    fps: Fraction | float | int,
    crf: int = 23,
    preset: str = "medium",
) -> SegmentVideoReceipt:
    """Encode, verify, and publish one independent silent MP4 without overwrite."""

    index = _integer(segment_index, name="segment_index", minimum=0, maximum=MAX_SEGMENT_INDEX)
    quality = _integer(crf, name="crf", minimum=0, maximum=51)
    if preset not in ALLOWED_PRESETS:
        raise AssetInvalid(
            "preset is not supported",
            reason="invalid_preset",
            actual=preset,
            expected=ALLOWED_PRESETS,
        )
    frame_rate = fps_fraction(fps)
    frame_count, height, width = validate_frame_batch(images)
    partial_path, final_path = _segment_paths(output_root, output_directory, index)
    if final_path.exists():
        raise AssetInvalid(
            "completed segment video already exists",
            path=str(final_path),
            reason="segment_exists",
        )
    try:
        with partial_path.open("xb"):
            pass
    except OSError as exc:
        _raise_io(exc, partial_path, "reserve_partial")
    if final_path.exists():
        raise AssetInvalid(
            "completed segment video appeared while reserving the partial path",
            path=str(final_path),
            reason="segment_exists",
        )
    try:
        encode_video_file(partial_path, images, fps=frame_rate, crf=quality, preset=preset)
        _validate_encoded_video(
            partial_path,
            frame_count=frame_count,
            width=width,
            height=height,
            fps=frame_rate,
            crf=quality,
            preset=preset,
        )
        _publish_no_replace(partial_path, final_path)
    except (AssetInvalid, EncoderUnavailable, NoProgress, StorageFull):
        raise
    except OSError as exc:
        _raise_io(exc, partial_path, "encode_or_validate")
    return SegmentVideoReceipt(
        str(final_path),
        index,
        frame_count,
        width,
        height,
        frame_rate.numerator,
        frame_rate.denominator,
        quality,
        preset,
    )


__all__ = [
    "ALLOWED_PRESETS",
    "MAX_FPS",
    "MAX_SEGMENT_INDEX",
    "SegmentVideoReceipt",
    "expected_segment_video_path",
    "fps_fraction",
    "preflight_new_segment_run",
    "resolve_segment_output_directory",
    "save_segment_video",
]
