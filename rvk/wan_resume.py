"""Inspect completed Wan delivery videos and hold one lightweight directory lease.

Formal videos are authoritative. The JSON record is advisory and never supplies
frame offsets, source identity, or tensors without rechecking the actual videos.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import torch

from rvk.errors import AssetInvalid, NoProgress
from rvk.segment_video import MAX_SEGMENT_INDEX, _relative_directory, fps_fraction
from rvk.video_codec import (
    MAX_IMAGE_DIMENSION,
    MAX_IMAGE_PIXELS,
    MAX_VIDEO_FRAMES_PER_SEGMENT,
    VIDEO_CODEC,
    VIDEO_PIXEL_FORMAT,
    av_module,
    inspect_video_file,
)


PROGRESS_FILENAME = "rvk_progress.json"
MAX_PROGRESS_BYTES = 64 * 1024
_FORMAL = re.compile(r"segment_([0-9]{4,6})\.mp4\Z")
_PARTIAL = re.compile(r"segment_([0-9]{4,6})\.partial\.mp4\Z")


@dataclass(frozen=True, slots=True)
class RunInspection:
    directory: Path
    completed_segments: int
    completed_frames: int
    total_frames: int
    total_segments: int
    last_path: Path | None
    recorded_video_name: str | None
    recorded_image_name: str | None
    partial_paths: tuple[Path, ...]
    warnings: tuple[str, ...]
    width: int | None = None
    height: int | None = None

    @property
    def complete(self) -> bool:
        return self.completed_frames == self.total_frames


def _directory(output_root: Path, output_directory: str) -> Path:
    root = Path(output_root).resolve(strict=False)
    directory = root.joinpath(*_relative_directory(output_directory)).resolve(strict=False)
    if directory == root or root not in directory.parents:
        raise AssetInvalid("run directory escapes the output root", path=str(directory), reason="invalid_output_directory")
    if root.exists() and not root.is_dir():
        raise AssetInvalid("output root is not a directory", path=str(root), reason="invalid_output_root")
    if directory.exists() and not directory.is_dir():
        raise AssetInvalid("run directory is not a directory", path=str(directory), reason="invalid_output_directory")
    return directory


def _validate_counts(total_frames: int, segment_length: int) -> None:
    if isinstance(total_frames, bool) or not isinstance(total_frames, int) or total_frames < 1:
        raise AssetInvalid("total_frames must be a positive integer", reason="invalid_total_frames")
    if (
        isinstance(segment_length, bool) or not isinstance(segment_length, int)
        or not 5 <= segment_length <= 16_381 or (segment_length - 1) % 4
    ):
        raise AssetInvalid("segment_length must be a supported Wan 4k+1 length", reason="invalid_segment_length")


def _safe_file(path: Path, directory: Path) -> None:
    if path.is_symlink() or not path.is_file() or path.resolve(strict=True).parent != directory:
        raise AssetInvalid("run media must be a regular file inside its directory", path=str(path), reason="resume_unsafe_file")


def _filename(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, (str, os.PathLike)):
        raise AssetInvalid("reference filename must be text", reason="invalid_reference_filename")
    name = str(value).replace("\\", "/").rsplit("/", 1)[-1]
    if not name or name in {".", ".."} or len(name) > 255 or "\x00" in name:
        raise AssetInvalid("reference filename is invalid", reason="invalid_reference_filename")
    return name


def _read_progress(directory: Path) -> tuple[dict | None, str | None]:
    path = directory / PROGRESS_FILENAME
    if not path.exists():
        return None, "Progress record is missing; completed videos determine progress and original reference names are unknown."
    try:
        _safe_file(path, directory)
        with path.open("rb") as handle:
            data = handle.read(MAX_PROGRESS_BYTES + 1)
        if len(data) > MAX_PROGRESS_BYTES:
            raise ValueError("record exceeds the byte limit")
        value = json.loads(data)
        if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
            raise ValueError("unsupported progress record")
        for key in ("completed_segments", "completed_frames", "total_frames", "segment_length"):
            number = value.get(key)
            if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                raise ValueError(f"invalid {key}")
        _validate_counts(value["total_frames"], value["segment_length"])
        if value["completed_segments"] > MAX_SEGMENT_INDEX + 1 or value["completed_frames"] > value["total_frames"]:
            raise ValueError("invalid progress counts")
        for key in ("original_reference_video", "original_reference_image", "reference_video", "reference_image"):
            if key not in value or _filename(value[key]) != value[key]:
                raise ValueError(f"invalid {key}")
        return value, None
    except (OSError, ValueError, UnicodeError, RecursionError, AssetInvalid) as exc:
        return None, f"Progress record is unreadable or invalid ({type(exc).__name__}); completed videos determine progress."


def inspect_run(
    output_root: Path,
    output_directory: str,
    total_frames: int,
    fps: Fraction | int | float,
    segment_length: int,
    mode: str = "resume",
) -> RunInspection:
    """Read and fully decode formal segments without creating any files or directories."""
    _validate_counts(total_frames, segment_length)
    rate = fps_fraction(fps)
    if mode not in {"new", "resume"}:
        raise AssetInvalid("mode must be new or resume", reason="invalid_run_mode")
    directory = _directory(output_root, output_directory)
    if not directory.exists() and mode == "resume":
        raise AssetInvalid("resume directory does not exist", path=str(directory), reason="resume_directory_missing")
    entries = sorted(directory.iterdir()) if directory.exists() else []
    if mode == "new" and entries:
        raise AssetInvalid("new run requires an empty directory", path=str(directory), reason="output_directory_not_empty")
    formal: dict[int, Path] = {}
    partials: list[Path] = []
    warnings: list[str] = []
    for path in entries:
        match = _FORMAL.fullmatch(path.name)
        partial = _PARTIAL.fullmatch(path.name)
        if match or partial:
            index = int((match or partial).group(1))
            suffix = ".mp4" if match else ".partial.mp4"
            if index > MAX_SEGMENT_INDEX or path.name != f"segment_{index:04d}{suffix}":
                raise AssetInvalid("segment filename is not canonical", path=str(path), reason="resume_segment_filename")
            _safe_file(path, directory)
            if match:
                formal[index] = path
            else:
                partials.append(path)
        elif path.name.startswith("segment_") and path.name.endswith(".mp4"):
            raise AssetInvalid("segment filename is not recognized", path=str(path), reason="resume_segment_filename")
        elif path.name != PROGRESS_FILENAME and not path.name.startswith("rvk_progress.") and path.name != "rvk_failed":
            warnings.append(f"Unrelated item is preserved: {path.name}")
    if sorted(formal) != list(range(len(formal))):
        raise AssetInvalid("formal segments must be contiguous starting at segment_0000.mp4", reason="resume_segment_gap")
    completed = 0
    profile = None
    width = height = None
    for index in range(len(formal)):
        path = formal[index]
        media = inspect_video_file(path)
        step = Fraction(rate.denominator, rate.numerator) / media.time_base
        if (
            media.fps != rate or media.codec != VIDEO_CODEC or media.pixel_format != VIDEO_PIXEL_FORMAT
            or (media.color_primaries, media.color_transfer, media.color_matrix, media.color_range) != (1, 1, 1, 1)
            or "mp4" not in media.container.split(",") or step.denominator != 1
            or media.pts != tuple(i * step.numerator for i in range(media.frame_count))
            or abs(media.duration - media.frame_count * step.numerator) > 1
        ):
            raise AssetInvalid("formal segment timing or codec is incompatible", path=str(path), reason="resume_media_mismatch")
        current_profile = (
            media.width, media.height, media.codec, media.pixel_format,
            media.color_primaries, media.color_transfer, media.color_matrix, media.color_range,
        )
        if profile is not None and current_profile != profile:
            raise AssetInvalid("formal segment dimensions or media profile differ", path=str(path), reason="resume_media_mismatch")
        profile = current_profile
        width, height = media.width, media.height
        completed += media.frame_count
    if completed > total_frames:
        raise AssetInvalid(
            "completed videos contain more frames than the current reference video", reason="resume_exceeds_source",
            actual=completed, expected=total_frames,
        )
    record, record_warning = _read_progress(directory) if entries else (None, None)
    if record_warning:
        warnings.append(record_warning)
    if record and (record["completed_segments"], record["completed_frames"]) != (len(formal), completed):
        warnings.append("Progress record counts are stale; completed videos determine progress.")
    if partials:
        warnings.append("Incomplete RVK partial files are excluded and will be moved aside when resume starts.")
    remaining = total_frames - completed
    total_segments = (
        len(formal) + (remaining + segment_length - 2) // (segment_length - 1)
        if formal else max(1, (total_frames - 1 + segment_length - 2) // (segment_length - 1))
    )
    return RunInspection(
        directory, len(formal), completed, total_frames, total_segments,
        formal[len(formal) - 1] if formal else None,
        record["original_reference_video"] if record else None,
        record["original_reference_image"] if record else None,
        tuple(partials), tuple(warnings), width, height,
    )


def write_progress(
    directory: Path, *, completed_segments: int, completed_frames: int, total_frames: int,
    segment_length: int, reference_video: str | None, reference_image: str | None,
) -> Path:
    """Atomically update advisory JSON while the caller holds the directory lease."""
    directory = Path(directory).resolve(strict=True)
    _validate_counts(total_frames, segment_length)
    if (
        isinstance(completed_segments, bool) or not isinstance(completed_segments, int)
        or not 0 <= completed_segments <= MAX_SEGMENT_INDEX + 1
        or isinstance(completed_frames, bool) or not isinstance(completed_frames, int)
        or not 0 <= completed_frames <= total_frames
    ):
        raise AssetInvalid("progress counts are invalid", reason="invalid_progress_counts")
    video_name, image_name = _filename(reference_video), _filename(reference_image)
    previous, _ = _read_progress(directory)
    # A legacy directory's original inputs are unknowable, even when resume gets new inputs.
    unknown_history = any(
        _FORMAL.fullmatch(path.name) or _PARTIAL.fullmatch(path.name)
        or path.name == "rvk_failed" or path.name.startswith("rvk_progress.")
        for path in directory.iterdir()
    )
    original_video = previous["original_reference_video"] if previous else (None if unknown_history else video_name)
    original_image = previous["original_reference_image"] if previous else (None if unknown_history else image_name)
    record = {
        "version": 1,
        "completed_segments": completed_segments,
        "completed_frames": completed_frames,
        "total_frames": total_frames,
        "segment_length": segment_length,
        "original_reference_video": original_video,
        "original_reference_image": original_image,
        "reference_video": video_name,
        "reference_image": image_name,
    }
    encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(encoded) > MAX_PROGRESS_BYTES:
        raise AssetInvalid("progress record exceeds the byte limit", reason="invalid_progress_record")
    destination = directory / PROGRESS_FILENAME
    if destination.exists():
        _safe_file(destination, directory)
        if previous is None:
            backup = directory / f"rvk_progress.corrupt.{uuid.uuid4().hex}.json"
            with destination.open("rb") as source, backup.open("xb") as target:
                shutil.copyfileobj(source, target, length=64 * 1024)
    temporary = directory / f"rvk_progress.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def quarantine_partials(inspection: RunInspection) -> tuple[Path, ...]:
    """Move only recognized partial names aside; caller must hold the directory lease."""
    if not inspection.partial_paths:
        return ()
    directory = inspection.directory.resolve(strict=True)
    for path in inspection.partial_paths:
        if path.parent != directory or not _PARTIAL.fullmatch(path.name):
            raise AssetInvalid("partial is outside the inspected run", path=str(path), reason="resume_unsafe_file")
        _safe_file(path, directory)
    failure_root = directory / "rvk_failed"
    if failure_root.resolve(strict=False).parent != directory or failure_root.is_symlink():
        raise AssetInvalid("failure directory is outside the run", path=str(failure_root), reason="resume_unsafe_file")
    failure_root.mkdir(exist_ok=True)
    destination = failure_root / uuid.uuid4().hex
    destination.mkdir()
    moved = []
    for path in inspection.partial_paths:
        target = destination / path.name
        path.rename(target)
        moved.append(target)
    return tuple(moved)


def read_last_frame(path: Path) -> torch.Tensor:
    """Stream decode while retaining only the last frame, then return owned CPU IMAGE."""
    av = av_module()
    try:
        with av.open(str(path), mode="r") as container:
            if len(container.streams.video) != 1 or len(container.streams) != 1:
                raise AssetInvalid("segment must contain one silent video stream", reason="resume_media_mismatch")
            last = None
            for count, frame in enumerate(container.decode(container.streams.video[0]), 1):
                if count > MAX_VIDEO_FRAMES_PER_SEGMENT or (
                    frame.width > MAX_IMAGE_DIMENSION or frame.height > MAX_IMAGE_DIMENSION
                    or frame.width * frame.height > MAX_IMAGE_PIXELS
                ):
                    raise AssetInvalid("continuation video exceeds media limits", reason="resource_limit")
                last = frame
            if last is None:
                raise NoProgress("segment contains no frames", reason="decoder_no_frames")
            return torch.from_numpy(last.to_ndarray(format="rgb24")).to(dtype=torch.float32).div_(255).unsqueeze(0).clone()
    except (AssetInvalid, NoProgress):
        raise
    except Exception as exc:
        raise AssetInvalid("last segment frame cannot be decoded", path=str(path), reason="resume_invalid_video") from exc


class RunDirectoryLease:
    """Nonblocking OS lock outside the run; its handle releases on process exit.

    Lock files stay in place after release to prevent unlink/recreate inode races.
    They carry no ownership journal or generation state.
    """

    def __init__(self, output_root: Path, output_directory: str):
        self.output_root = Path(output_root).resolve(strict=False)
        self.directory = _directory(self.output_root, output_directory)
        key = os.path.normcase(str(self.directory)).encode("utf-8")
        self.path = self.output_root / ".rvk_locks" / (hashlib.sha256(key).hexdigest() + ".lock")
        self._handle = None
        self._mutex = threading.Lock()

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> RunDirectoryLease:
        with self._mutex:
            if self._handle is not None:
                raise AssetInvalid("directory lease is already held", reason="run_directory_locked")
            self.output_root.mkdir(parents=True, exist_ok=True)
            lock_root = self.path.parent
            if lock_root.resolve(strict=False).parent != self.output_root or lock_root.is_symlink():
                raise AssetInvalid("lock directory is outside output root", reason="resume_unsafe_file")
            lock_root.mkdir(exist_ok=True)
            if self.path.is_symlink():
                raise AssetInvalid("lock file cannot be a symlink", reason="resume_unsafe_file")
            handle = self.path.open("a+b", buffering=0)
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.close()
                raise AssetInvalid(
                    "this run directory is already being written by another execution", path=str(self.directory),
                    reason="run_directory_locked",
                ) from exc
            self._handle = handle
            return self

    def release(self) -> None:
        with self._mutex:
            handle, self._handle = self._handle, None
            if handle is not None:
                try:
                    if os.name == "nt":
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()

    def __enter__(self) -> RunDirectoryLease:
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


__all__ = [
    "RunInspection", "RunDirectoryLease", "inspect_run", "write_progress", "quarantine_partials", "read_last_frame",
]
