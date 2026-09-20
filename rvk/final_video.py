"""Finalize verified silent segment MP4s with audio from the source video."""

from __future__ import annotations

import errno
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path, PurePosixPath, PureWindowsPath

from rvk.errors import AssetInvalid, EncoderUnavailable, NoProgress, StorageFull
from rvk.segment_video import MAX_FPS, MAX_SEGMENT_INDEX, fps_fraction
from rvk.video_codec import VIDEO_CODEC, VIDEO_PIXEL_FORMAT, VideoInspection, av_module, inspect_video_file


_SEGMENT_RE = re.compile(r"segment_(\d{4,6})\.mp4")
_PARTIAL_SEGMENT_RE = re.compile(r"segment_(\d{4,6})\.partial\.mp4")
_MAX_FINAL_FILENAME_LENGTH = 200
_AUDIO_START_TOLERANCE_SAMPLES = 2
_AUDIO_END_TOLERANCE = Fraction(1, 10)
_FINAL_FFMPEG_TIMEOUT_SECONDS = 30 * 60.0
_FFMPEG_POLL_SECONDS = 0.1
_FFMPEG_STOP_GRACE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class FinalVideoResult:
    """Small node result; it never owns images, audio samples, or model state."""

    status: str
    path: str
    message: str
    segment_count: int
    frame_count: int
    width: int
    height: int
    fps_num: int
    fps_den: int
    audio_codec: str
    audio_sample_rate: int
    audio_channels: int

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    @property
    def fps(self) -> Fraction:
        if self.fps_den == 0:
            return Fraction(0, 1)
        return Fraction(self.fps_num, self.fps_den)

    @classmethod
    def waiting(cls, message: str) -> FinalVideoResult:
        return cls("waiting", "", message, 0, 0, 0, 0, 0, 1, "", 0, 0)


@dataclass(frozen=True, slots=True)
class _AudioInspection:
    codec: str
    sample_rate: int
    channels: int
    start: Fraction
    end: Fraction


def _raise_io(exc: OSError, path: Path, stage: str) -> None:
    winerror = getattr(exc, "winerror", None)
    if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)} or winerror == 112:
        raise StorageFull(
            "final video storage is full",
            path=str(path),
            reason="final_storage_full",
            actual=stage,
        ) from exc
    if isinstance(exc, FileExistsError) or exc.errno == errno.EEXIST or winerror in {80, 183}:
        raise AssetInvalid(
            "final video path already exists",
            path=str(path),
            reason="final_exists",
            actual=stage,
        ) from exc
    if winerror in {32, 33}:
        reason = "storage_sharing_violation"
    elif exc.errno in {errno.EACCES, errno.EPERM}:
        reason = "storage_access_denied"
    else:
        reason = "final_storage_io_failed"
    raise AssetInvalid(
        "final video storage operation failed",
        path=str(path),
        reason=reason,
        actual=stage,
    ) from exc


def _relative_directory(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AssetInvalid(
            "output_directory must name a relative run directory",
            reason="invalid_output_directory",
        )
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value.replace("\\", "/"))
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


def resolve_run_directory(output_root: Path, output_directory: str) -> Path:
    root = Path(output_root)
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        _raise_io(exc, root, "resolve_output_root")
    if not resolved_root.is_dir():
        raise AssetInvalid(
            "output root is not a directory",
            path=str(resolved_root),
            reason="invalid_output_root",
        )
    candidate = resolved_root.joinpath(*_relative_directory(output_directory))
    try:
        directory = candidate.resolve(strict=True)
    except OSError as exc:
        raise AssetInvalid(
            "segment output directory does not exist",
            path=str(candidate),
            reason="output_directory_missing",
        ) from exc
    if resolved_root not in directory.parents or not directory.is_dir():
        raise AssetInvalid(
            "output_directory is outside the configured output root or is not a directory",
            path=str(directory),
            reason="invalid_output_directory",
        )
    return directory


def _final_paths(directory: Path, final_filename: str) -> tuple[Path, Path]:
    if (
        not isinstance(final_filename, str)
        or not final_filename
        or "\x00" in final_filename
        or len(final_filename) > _MAX_FINAL_FILENAME_LENGTH
    ):
        raise AssetInvalid("final_filename is invalid", reason="invalid_final_filename", actual=final_filename)
    windows = PureWindowsPath(final_filename)
    posix = PurePosixPath(final_filename.replace("\\", "/"))
    if (
        windows.drive
        or windows.root
        or posix.is_absolute()
        or len(posix.parts) != 1
        or posix.name in {".", ".."}
        or posix.suffix.lower() != ".mp4"
        or ".partial" in posix.stem.lower()
        or _SEGMENT_RE.fullmatch(posix.name) is not None
    ):
        raise AssetInvalid(
            "final_filename must be one non-segment .mp4 file name",
            reason="invalid_final_filename",
            actual=final_filename,
        )
    final_path = directory / posix.name
    partial_path = directory / f"{posix.stem}.partial.mp4"
    return partial_path, final_path


def _discover_segments(directory: Path) -> tuple[Path, ...]:
    indexed: dict[int, Path] = {}
    partials: list[str] = []
    try:
        entries = tuple(directory.iterdir())
    except OSError as exc:
        _raise_io(exc, directory, "list_segments")
    for entry in entries:
        partial_match = _PARTIAL_SEGMENT_RE.fullmatch(entry.name)
        if partial_match is not None:
            partials.append(entry.name)
            continue
        match = _SEGMENT_RE.fullmatch(entry.name)
        if match is None:
            continue
        index = int(match.group(1))
        if index > MAX_SEGMENT_INDEX or not entry.is_file():
            raise AssetInvalid(
                "segment entry is not a supported regular file",
                path=str(entry),
                reason="invalid_segment_entry",
            )
        if index in indexed:
            raise AssetInvalid(
                "two segment files resolve to the same numeric index",
                path=str(entry),
                reason="duplicate_segment_index",
                actual=index,
            )
        indexed[index] = entry
    if partials:
        raise AssetInvalid(
            "incomplete segment files are present",
            path=str(directory),
            reason="incomplete_segment_present",
            actual=sorted(partials),
        )
    if not indexed:
        raise NoProgress("no completed segment videos were found", path=str(directory), reason="no_segments")
    expected = list(range(max(indexed) + 1))
    actual = sorted(indexed)
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        raise AssetInvalid(
            "segment numbering must be contiguous from zero",
            path=str(directory),
            reason="segment_gap",
            actual=actual,
            expected={"missing": missing},
        )
    return tuple(indexed[index] for index in expected)


def _validate_one_segment(path: Path, actual: VideoInspection) -> None:
    step = Fraction(actual.fps.denominator, actual.fps.numerator) / actual.time_base
    expected_pts = () if step.denominator != 1 else tuple(index * step.numerator for index in range(actual.frame_count))
    if (
        "mp4" not in actual.container.split(",")
        or actual.codec != VIDEO_CODEC
        or actual.pixel_format != VIDEO_PIXEL_FORMAT
        or (actual.color_primaries, actual.color_transfer, actual.color_matrix, actual.color_range) != (1, 1, 1, 1)
        or actual.audio_streams != 0
        or step.denominator != 1
        or actual.pts != expected_pts
        or abs(actual.duration - actual.frame_count * step.numerator) > 1
    ):
        raise AssetInvalid(
            "segment video is not compatible with lossless stream concatenation",
            path=str(path),
            reason="incompatible_segment",
        )


def _inspect_segments(
    paths: tuple[Path, ...],
    *,
    source_frame_count: int,
    source_fps: Fraction,
    expected_segment_count: int | None,
) -> tuple[VideoInspection, int]:
    if expected_segment_count is not None and len(paths) != expected_segment_count:
        raise AssetInvalid(
            "segment count does not match the completed loop status",
            reason="segment_count_mismatch",
            actual=len(paths),
            expected=expected_segment_count,
        )
    first: VideoInspection | None = None
    total_frames = 0
    for path in paths:
        actual = inspect_video_file(path)
        _validate_one_segment(path, actual)
        if first is None:
            first = actual
        elif (
            actual.width,
            actual.height,
            actual.fps,
            actual.time_base,
            actual.codec,
            actual.pixel_format,
            actual.color_primaries,
            actual.color_transfer,
            actual.color_matrix,
            actual.color_range,
        ) != (
            first.width,
            first.height,
            first.fps,
            first.time_base,
            first.codec,
            first.pixel_format,
            first.color_primaries,
            first.color_transfer,
            first.color_matrix,
            first.color_range,
        ):
            raise AssetInvalid(
                "segment media settings differ",
                path=str(path),
                reason="incompatible_segment",
            )
        total_frames += actual.frame_count
        if total_frames > source_frame_count:
            raise AssetInvalid(
                "segments contain more frames than the source video",
                path=str(path),
                reason="segment_frame_count_mismatch",
                actual=total_frames,
                expected=source_frame_count,
            )
    if first is None:
        raise NoProgress("no segment video could be inspected", reason="no_segments")
    if first.fps != source_fps:
        raise AssetInvalid(
            "segment frame rate differs from the source video",
            reason="source_fps_mismatch",
            actual=str(first.fps),
            expected=str(source_fps),
        )
    if total_frames != source_frame_count:
        raise AssetInvalid(
            "completed segment frames do not cover the source video",
            reason="segment_frame_count_mismatch",
            actual=total_frames,
            expected=source_frame_count,
        )
    return first, total_frames


def _inspect_audio(path: Path, *, expected_duration: Fraction, output: bool) -> _AudioInspection:
    av = av_module()
    label = "final" if output else "source"
    try:
        with av.open(str(path), mode="r") as container:
            if output:
                if len(container.streams.video) != 1 or len(container.streams.audio) != 1 or len(container.streams) != 2:
                    raise AssetInvalid(
                        "final video must contain exactly one video and one audio stream",
                        path=str(path),
                        reason="final_stream_mismatch",
                    )
            elif len(container.streams.audio) < 1:
                raise AssetInvalid(
                    "source video has no audio stream",
                    path=str(path),
                    reason="source_audio_missing",
                )
            stream = container.streams.audio[0]
            codec = str(stream.codec_context.name)
            start: Fraction | None = None
            end: Fraction | None = None
            sample_rate = 0
            channels = 0
            for frame in container.decode(stream):
                if frame.pts is None or frame.time_base is None or frame.sample_rate is None or frame.samples <= 0:
                    raise AssetInvalid(
                        f"{label} audio timing is incomplete",
                        path=str(path),
                        reason=f"{label}_audio_invalid",
                    )
                frame_start = Fraction(int(frame.pts)) * Fraction(frame.time_base)
                frame_end = frame_start + Fraction(int(frame.samples), int(frame.sample_rate))
                current_channels = len(frame.layout.channels)
                if sample_rate and (sample_rate != int(frame.sample_rate) or channels != current_channels):
                    raise AssetInvalid(
                        f"{label} audio settings change during the stream",
                        path=str(path),
                        reason=f"{label}_audio_invalid",
                    )
                sample_rate = int(frame.sample_rate)
                channels = current_channels
                start = frame_start if start is None else min(start, frame_start)
                end = frame_end if end is None else max(end, frame_end)
            if start is None or end is None or sample_rate <= 0 or channels <= 0:
                raise AssetInvalid(
                    f"{label} audio has no decodable samples",
                    path=str(path),
                    reason=f"{label}_audio_invalid",
                )
            start_tolerance = Fraction(_AUDIO_START_TOLERANCE_SAMPLES, sample_rate)
            if abs(start) > start_tolerance:
                raise AssetInvalid(
                    f"{label} audio does not begin at the video timeline origin",
                    path=str(path),
                    reason=f"{label}_audio_start_mismatch",
                    actual=str(start),
                    expected={"target": "0", "absolute_tolerance": str(start_tolerance)},
                )
            # AAC/container frame boundaries commonly leave a final fraction of
            # a second uncovered.  Accept at most 100 ms, but never shorten the
            # video or silently tolerate a materially incomplete source track.
            tolerance = max(Fraction(2, sample_rate), _AUDIO_END_TOLERANCE)
            if end < expected_duration - tolerance:
                raise AssetInvalid(
                    f"{label} audio is shorter than the final video",
                    path=str(path),
                    reason=f"{label}_audio_too_short",
                    actual=str(end),
                    expected=str(expected_duration),
                )
            if output and end > expected_duration + tolerance:
                raise AssetInvalid(
                    "final audio extends beyond the final video",
                    path=str(path),
                    reason="final_audio_duration_mismatch",
                    actual=str(end),
                    expected=str(expected_duration),
                )
            return _AudioInspection(codec, sample_rate, channels, start, end)
    except AssetInvalid:
        raise
    except Exception as exc:
        if isinstance(exc, (OSError, getattr(av.error, "FFmpegError", ()))):
            raise AssetInvalid(
                f"{label} audio cannot be decoded",
                path=str(path),
                reason=f"{label}_audio_invalid",
                actual=str(exc),
            ) from exc
        raise


def _inspect_final_video(
    path: Path,
    *,
    expected: VideoInspection,
    frame_count: int,
    duration: Fraction,
) -> _AudioInspection:
    av = av_module()
    try:
        with av.open(str(path), mode="r") as container:
            if len(container.streams.video) != 1 or len(container.streams.audio) != 1 or len(container.streams) != 2:
                raise AssetInvalid(
                    "final video must contain exactly one video and one audio stream",
                    path=str(path),
                    reason="final_stream_mismatch",
                )
            stream = container.streams.video[0]
            context = stream.codec_context
            if stream.average_rate is None or stream.time_base is None or stream.duration is None:
                raise AssetInvalid(
                    "final video timing metadata is incomplete",
                    path=str(path),
                    reason="final_media_mismatch",
                )
            pts = tuple(int(frame.pts) for frame in container.decode(stream) if frame.pts is not None)
            time_base = Fraction(stream.time_base)
            fps = Fraction(stream.average_rate)
            step = Fraction(fps.denominator, fps.numerator) / time_base
            expected_pts = () if step.denominator != 1 else tuple(index * step.numerator for index in range(frame_count))
            if (
                len(pts) != frame_count
                or context.width != expected.width
                or context.height != expected.height
                or fps != expected.fps
                or str(context.name) != VIDEO_CODEC
                or str(context.format.name) != VIDEO_PIXEL_FORMAT
                or (int(context.color_primaries), int(context.color_trc), int(context.colorspace), int(context.color_range))
                != (1, 1, 1, 1)
                or step.denominator != 1
                or pts != expected_pts
                or abs(int(stream.duration) - frame_count * step.numerator) > 1
            ):
                raise AssetInvalid(
                    "final video differs from the ordered segment stream",
                    path=str(path),
                    reason="final_media_mismatch",
                )
    except AssetInvalid:
        raise
    except Exception as exc:
        if isinstance(exc, (OSError, getattr(av.error, "FFmpegError", ()))):
            raise AssetInvalid(
                "final video cannot be demuxed and fully decoded",
                path=str(path),
                reason="final_media_invalid",
                actual=str(exc),
            ) from exc
        raise
    audio = _inspect_audio(path, expected_duration=duration, output=True)
    if audio.codec != "aac":
        raise AssetInvalid(
            "final audio codec is not AAC",
            path=str(path),
            reason="final_audio_codec_mismatch",
            actual=audio.codec,
            expected="aac",
        )
    return audio


def _publish_no_replace(partial_path: Path, final_path: Path) -> None:
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
                "final video was published but cleanup failed and file identity is uncertain",
                path=str(partial_path),
                reason="final_publish_uncertain",
                actual={"published_path": str(final_path), "cleanup_error": str(exc)},
                expected="partial and formal paths must identify the same published file",
            ) from identity_exc
        if not same_file:
            raise AssetInvalid(
                "final video was published but cleanup failed and the paths no longer identify the same file",
                path=str(partial_path),
                reason="final_publish_uncertain",
                actual={"published_path": str(final_path), "cleanup_error": str(exc)},
                expected="partial and formal paths must identify the same published file",
            ) from exc
        raise AssetInvalid(
            "final video was published, but its partial hard link could not be removed; remove the partial before continuing",
            path=str(partial_path),
            reason="final_cleanup_required",
            actual={"published_path": str(final_path), "same_file": True, "cleanup_error": str(exc)},
            expected="remove the partial path while preserving the published video",
        ) from exc


def _write_concat_list(directory: Path, segments: tuple[Path, ...]) -> Path:
    try:
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=".rvk-finalize-",
            suffix=".txt",
            dir=directory,
            delete=False,
        )
        with handle:
            for segment in segments:
                # Segment names are generated by RVK and cannot contain concat syntax.
                handle.write(f"file '{segment.name}'\n")
        return Path(handle.name)
    except OSError as exc:
        _raise_io(exc, directory, "write_concat_list")


def _run_ffmpeg(
    *,
    directory: Path,
    concat_path: Path,
    source_video_path: Path,
    partial_path: Path,
    duration: Fraction,
    timeout_seconds: float,
    cancel_check: Callable[[], None] | None,
) -> None:
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise EncoderUnavailable("FFmpeg is not available on PATH", reason="ffmpeg_missing")
    duration_text = f"{float(duration):.12f}".rstrip("0").rstrip(".")
    command = [
        executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "1",
        "-i",
        concat_path.name,
        "-i",
        str(source_video_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-map_metadata",
        "-1",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-af",
        f"atrim=start=0:end={duration_text},asetpts=PTS-STARTPTS",
        "-t",
        duration_text,
        "-movflags",
        "+faststart",
        "-n",
        partial_path.name,
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
    except OSError as exc:
        _raise_io(exc, partial_path, "run_ffmpeg")

    def stop_process() -> None:
        try:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=_FFMPEG_STOP_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        except OSError as exc:
            if process.poll() is None:
                raise AssetInvalid(
                    "FFmpeg could not be stopped safely",
                    path=str(partial_path),
                    reason="final_process_cleanup_failed",
                    actual=str(exc),
                ) from exc
            return
        try:
            process.kill()
            process.communicate(timeout=_FFMPEG_STOP_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AssetInvalid(
                "FFmpeg remained alive after terminate and kill",
                path=str(partial_path),
                reason="final_process_cleanup_failed",
                actual=str(exc),
            ) from exc

    deadline = time.monotonic() + timeout_seconds
    stdout = stderr = ""
    while True:
        try:
            if cancel_check is not None:
                cancel_check()
        except BaseException:
            stop_process()
            raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stop_process()
            raise AssetInvalid(
                "FFmpeg exceeded the final-video encoding timeout",
                path=str(partial_path),
                reason="final_encode_timeout",
                actual=timeout_seconds,
            )
        try:
            stdout, stderr = process.communicate(timeout=min(_FFMPEG_POLL_SECONDS, remaining))
            break
        except subprocess.TimeoutExpired:
            continue
        except OSError as exc:
            stop_process()
            _raise_io(exc, partial_path, "run_ffmpeg")

    if process.returncode != 0:
        details = (stderr or "").strip()[-2000:]
        if "No space left on device" in details:
            raise StorageFull(
                "FFmpeg ran out of storage while creating the final video",
                path=str(partial_path),
                reason="final_storage_full",
            )
        if "Unknown encoder" in details and "aac" in details:
            raise EncoderUnavailable(
                "FFmpeg does not provide the required AAC encoder",
                reason="aac_encoder_missing",
                actual=details,
            )
        raise AssetInvalid(
            "FFmpeg failed while creating the final video",
            path=str(partial_path),
            reason="final_encode_failed",
            actual=details or f"exit code {process.returncode}",
        )
    if not partial_path.is_file() or partial_path.stat().st_size <= 0:
        raise NoProgress(
            "FFmpeg produced no final video",
            path=str(partial_path),
            reason="final_encoder_no_output",
        )


def finalize_segment_videos(
    *,
    output_root: Path,
    output_directory: str,
    source_video_path: Path,
    source_frame_count: int,
    source_fps: Fraction | float | int,
    final_filename: str = "final.mp4",
    expected_segment_count: int | None = None,
    cancel_check: Callable[[], None] | None = None,
    ffmpeg_timeout_seconds: float = _FINAL_FFMPEG_TIMEOUT_SECONDS,
) -> FinalVideoResult:
    """Validate, concatenate, add source audio, verify, and publish one final MP4."""

    if isinstance(source_frame_count, bool) or not isinstance(source_frame_count, int) or source_frame_count < 1:
        raise AssetInvalid(
            "source_frame_count must be a positive integer",
            reason="invalid_source_frame_count",
            actual=source_frame_count,
        )
    if (
        expected_segment_count is not None
        and (isinstance(expected_segment_count, bool) or not isinstance(expected_segment_count, int) or expected_segment_count < 1)
    ):
        raise AssetInvalid(
            "expected_segment_count must be a positive integer",
            reason="invalid_segment_count",
            actual=expected_segment_count,
        )
    if (
        isinstance(ffmpeg_timeout_seconds, bool)
        or not isinstance(ffmpeg_timeout_seconds, (int, float))
        or not math.isfinite(float(ffmpeg_timeout_seconds))
        or ffmpeg_timeout_seconds <= 0
    ):
        raise AssetInvalid(
            "ffmpeg_timeout_seconds must be finite and positive",
            reason="invalid_final_timeout",
            actual=ffmpeg_timeout_seconds,
        )
    if cancel_check is not None and not callable(cancel_check):
        raise AssetInvalid("cancel_check must be callable", reason="invalid_cancel_check")
    timeout_seconds = float(ffmpeg_timeout_seconds)
    fps = fps_fraction(source_fps)
    if fps > MAX_FPS:
        raise AssetInvalid("source fps is unsupported", reason="invalid_fps", actual=str(fps))
    try:
        source = Path(source_video_path).resolve(strict=True)
    except OSError as exc:
        raise AssetInvalid(
            "source video cannot be resolved",
            path=str(source_video_path),
            reason="source_video_unavailable",
        ) from exc
    if not source.is_file():
        raise AssetInvalid(
            "source video is not a file",
            path=str(source),
            reason="source_video_unavailable",
        )
    directory = resolve_run_directory(output_root, output_directory)
    partial_path, final_path = _final_paths(directory, final_filename)
    if final_path.exists():
        raise AssetInvalid(
            "completed final video already exists",
            path=str(final_path),
            reason="final_exists",
        )
    if partial_path.exists():
        raise AssetInvalid(
            "an incomplete final video already exists",
            path=str(partial_path),
            reason="final_partial_exists",
        )
    segments = _discover_segments(directory)
    first, total_frames = _inspect_segments(
        segments,
        source_frame_count=source_frame_count,
        source_fps=fps,
        expected_segment_count=expected_segment_count,
    )
    duration = Fraction(total_frames, 1) / fps
    _inspect_audio(source, expected_duration=duration, output=False)
    concat_path = _write_concat_list(directory, segments)
    try:
        _run_ffmpeg(
            directory=directory,
            concat_path=concat_path,
            source_video_path=source,
            partial_path=partial_path,
            duration=duration,
            timeout_seconds=timeout_seconds,
            cancel_check=cancel_check,
        )
    finally:
        try:
            concat_path.unlink(missing_ok=True)
        except OSError as exc:
            logging.warning("Could not remove RVK finalization list %s: %s", concat_path, exc)
    audio = _inspect_final_video(partial_path, expected=first, frame_count=total_frames, duration=duration)
    _publish_no_replace(partial_path, final_path)
    return FinalVideoResult(
        "completed",
        str(final_path),
        "Final video published with source audio.",
        len(segments),
        total_frames,
        first.width,
        first.height,
        fps.numerator,
        fps.denominator,
        audio.codec,
        audio.sample_rate,
        audio.channels,
    )


__all__ = ["FinalVideoResult", "finalize_segment_videos", "resolve_run_directory"]
