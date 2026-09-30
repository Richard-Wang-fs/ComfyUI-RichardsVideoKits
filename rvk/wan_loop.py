"""In-memory Wan Animate 2 segment planning and continuation state.

This module deliberately has no ComfyUI, persistence, or controller imports.
"""

from __future__ import annotations

import math
import threading
import time
import uuid
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable

import torch

from rvk.errors import RVKError
from rvk.segment_video import SegmentVideoReceipt, expected_segment_video_path


DEFAULT_SEGMENT_LENGTH = 81
MIN_SEGMENT_LENGTH = 5
MAX_SEGMENT_LENGTH = 16_381
DEFAULT_RUN_TTL_SECONDS = 30 * 60


class WanLoopError(RVKError):
    """A Wan loop request cannot safely enter or advance its in-memory run."""

    code = "RVK_WAN_LOOP_ERROR"


@dataclass(frozen=True, slots=True)
class WanSegmentPlan:
    run_token: str
    workflow_id: str
    entry_node_id: str
    source_key: str
    segment_index: int
    segment_length: int
    total_frames: int
    fps_num: int
    fps_den: int
    global_start: int
    local_offset: int
    valid_raw_frames: int
    trim_front: int
    delivery_frames: int
    next_offset: int
    has_more: bool

    @property
    def fps(self) -> Fraction:
        return Fraction(self.fps_num, self.fps_den)


@dataclass(frozen=True, slots=True)
class WanContinuationCandidate:
    run_token: str
    segment_index: int
    image: torch.Tensor | None


@dataclass(frozen=True, slots=True)
class WanLoopStatus:
    run_token: str
    workflow_id: str
    entry_node_id: str
    completed_segment: int
    next_segment: int
    completed_frames: int
    total_frames: int
    next_offset: int
    has_more: bool
    stopped: bool
    path: str
    total_segments: int = 0


@dataclass(frozen=True, slots=True)
class WanEntry:
    plan: WanSegmentPlan
    continuation: torch.Tensor | None
    total_segments: int = 0
    display_info: dict | None = None


@dataclass(frozen=True, slots=True)
class WanRunInspection:
    run_token: str
    workflow_id: str
    entry_node_id: str
    segment_index: int
    next_offset: int
    phase: str
    stop_requested: bool
    continuation_frames: int
    continuation_numel: int
    continuation_device: str | None
    continuation_contiguous: bool


@dataclass(slots=True)
class _WanRun:
    token: str
    workflow_id: str
    entry_node_id: str
    source_key: str
    output_directory: str
    segment_length: int
    total_frames: int
    fps: Fraction
    segment_index: int
    next_offset: int
    phase: str
    stop_requested: bool
    continuation: torch.Tensor | None
    ready_expires_at: float | None
    total_segments: int = 0
    resource: object | None = None
    on_saved: Callable[[WanLoopStatus], None] | None = None
    display_info: dict | None = None
    is_prompt_active: Callable[[], bool] | None = None
    prompt_id: str | None = None


def validate_segment_length(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < MIN_SEGMENT_LENGTH
        or value > MAX_SEGMENT_LENGTH
        or (value - 1) % 4 != 0
    ):
        raise WanLoopError(
            "segment_length must be a progressing Wan 4k+1 frame count",
            reason="invalid_segment_length",
            actual=value,
            expected={
                "minimum": MIN_SEGMENT_LENGTH,
                "maximum": MAX_SEGMENT_LENGTH,
                "step": 4,
                "offset": 1,
            },
        )
    return value


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WanLoopError(
            f"{name} must be a positive integer",
            reason=f"invalid_{name}",
            actual=value,
        )
    return value


def _positive_fps(value: Fraction | int) -> Fraction:
    try:
        fps = Fraction(value)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise WanLoopError("fps must be a positive rational", reason="invalid_fps", actual=value) from exc
    if fps <= 0:
        raise WanLoopError("fps must be a positive rational", reason="invalid_fps", actual=str(fps))
    return fps


def segment_count(total_frames: int, segment_length: int) -> int:
    total = _positive_integer(total_frames, "total_frames")
    length = validate_segment_length(segment_length)
    return max(1, math.ceil((total - 1) / (length - 1)))


def make_segment_plan(
    *,
    run_token: str,
    workflow_id: str,
    entry_node_id: str,
    source_key: str,
    segment_index: int,
    next_offset: int,
    segment_length: int,
    total_frames: int,
    fps: Fraction,
) -> WanSegmentPlan:
    length = validate_segment_length(segment_length)
    total = _positive_integer(total_frames, "total_frames")
    frame_rate = _positive_fps(fps)
    if isinstance(segment_index, bool) or not isinstance(segment_index, int) or segment_index < 0:
        raise WanLoopError("segment_index is invalid", reason="invalid_segment_index", actual=segment_index)
    if isinstance(next_offset, bool) or not isinstance(next_offset, int) or next_offset < 0:
        raise WanLoopError("next_offset is invalid", reason="invalid_next_offset", actual=next_offset)
    start = 0 if segment_index == 0 else next_offset - 1
    if start < 0 or start >= total:
        raise WanLoopError(
            "segment plan cannot make forward progress",
            reason="no_progress",
            actual={"segment_index": segment_index, "next_offset": next_offset, "total_frames": total},
        )
    trim_front = 0 if segment_index == 0 else 1
    valid_raw = min(length, total - start)
    delivery = valid_raw - trim_front
    if delivery <= 0:
        raise WanLoopError(
            "segment would deliver no new frames",
            reason="no_progress",
            actual={"global_start": start, "valid_raw_frames": valid_raw, "trim_front": trim_front},
        )
    planned_next = start + length
    return WanSegmentPlan(
        run_token=run_token,
        workflow_id=workflow_id,
        entry_node_id=entry_node_id,
        source_key=source_key,
        segment_index=segment_index,
        segment_length=length,
        total_frames=total,
        fps_num=frame_rate.numerator,
        fps_den=frame_rate.denominator,
        global_start=start,
        local_offset=0 if segment_index == 0 else 1,
        valid_raw_frames=valid_raw,
        trim_front=trim_front,
        delivery_frames=delivery,
        next_offset=planned_next,
        has_more=total > planned_next,
    )


def collect_wan_segment(
    raw_images: torch.Tensor,
    plan: WanSegmentPlan,
) -> tuple[torch.Tensor, WanContinuationCandidate]:
    """Detach delivery frames and clone only one raw continuation frame to CPU."""

    if (
        not isinstance(raw_images, torch.Tensor)
        or raw_images.layout is not torch.strided
        or raw_images.ndim != 4
        or raw_images.shape[-1] != 3
        or raw_images.dtype not in {torch.float16, torch.bfloat16, torch.float32}
    ):
        raise WanLoopError(
            "raw_images must be a dense floating [T,H,W,3] tensor",
            reason="invalid_raw_images",
        )
    if int(raw_images.shape[0]) != plan.segment_length:
        raise WanLoopError(
            "raw decode frame count differs from segment_length",
            reason="raw_frame_count_mismatch",
            actual=int(raw_images.shape[0]),
            expected=plan.segment_length,
        )
    if not bool(torch.isfinite(raw_images).all().item()):
        raise WanLoopError("raw_images contains non-finite values", reason="invalid_raw_images")
    delivery = raw_images[plan.trim_front : plan.valid_raw_frames].detach().clone()
    if int(delivery.shape[0]) != plan.delivery_frames:
        raise WanLoopError("delivery crop is inconsistent with the plan", reason="delivery_frame_count_mismatch")
    continuation = None
    if plan.has_more:
        continuation = (
            raw_images[plan.valid_raw_frames - 1 : plan.valid_raw_frames]
            .detach()
            .to(device="cpu")
            .contiguous()
            .clone()
        )
    return delivery, WanContinuationCandidate(plan.run_token, plan.segment_index, continuation)


class WanRunRegistry:
    """One-process state machine; it is intentionally empty after restart."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_RUN_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be finite and positive")
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._token_factory = token_factory
        self._lock = threading.RLock()
        self._runs: dict[str, _WanRun] = {}
        self._entry_tokens: dict[tuple[str, str], str] = {}
        self._janitor_stop = threading.Event()
        self._janitor_thread: threading.Thread | None = None

    def _drop_locked(self, token: str) -> _WanRun | None:
        state = self._runs.pop(token, None)
        entry_key = None if state is None else (state.workflow_id, state.entry_node_id)
        if entry_key is not None and self._entry_tokens.get(entry_key) == token:
            del self._entry_tokens[entry_key]
        if state is not None and state.resource is not None:
            state.resource.release()
        return state

    def _cleanup_locked(self, now: float) -> tuple[str, ...]:
        # Wall-clock age cannot distinguish an orphan from a legitimately slow
        # prompt. Only idle/ready runs have a time-based cleanup deadline.
        expired = tuple(
            token
            for token, state in self._runs.items()
            if (state.phase == "ready" and state.ready_expires_at is not None and state.ready_expires_at <= now)
            or (state.phase == "in_flight" and self._prompt_finished(state))
        )
        for token in expired:
            self._drop_locked(token)
        return expired

    @staticmethod
    def _prompt_finished(state: _WanRun) -> bool:
        if state.is_prompt_active is None:
            return False
        try:
            return state.is_prompt_active() is False
        except Exception:
            # An unavailable queue is not evidence that its writer has stopped.
            return False

    def reclaim_finished_directory(self, directory: Path) -> tuple[str, ...]:
        """Allow an explicit new/resume admission to supersede a finished prompt.

        A ready run is normally kept for the automatic next prompt. If its browser
        disappeared, a fresh resume can use disk once neither that prompt nor a
        queued continuation is active. No timeout is used for in-flight work.
        """
        with self._lock:
            tokens = tuple(token for token, state in self._runs.items()
                           if getattr(state.resource, "directory", None) == directory
                           and self._prompt_finished(state))
            for token in tokens:
                self._drop_locked(token)
            return tokens

    def finish_prompt(self, prompt_id: str) -> tuple[str, ...]:
        """Release failed/incomplete executions even if their browser disconnected."""
        with self._lock:
            tokens = tuple(token for token, state in self._runs.items()
                           if state.prompt_id == prompt_id
                           and (state.phase == "in_flight" or state.stop_requested))
            for token in tokens:
                self._drop_locked(token)
            return tokens

    def cleanup_expired(self) -> tuple[str, ...]:
        with self._lock:
            return self._cleanup_locked(self._clock())

    def start_janitor(self, *, interval_seconds: float = 60.0) -> None:
        """Start one daemon that bounds stale-run lifetime without disk state."""

        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("interval_seconds must be finite and positive")
        with self._lock:
            if self._janitor_thread is not None and self._janitor_thread.is_alive():
                return
            self._janitor_stop.clear()

            def run() -> None:
                while not self._janitor_stop.wait(interval_seconds):
                    self.cleanup_expired()

            self._janitor_thread = threading.Thread(target=run, name="rvk-wan-run-janitor", daemon=True)
            self._janitor_thread.start()

    def stop_janitor(self, *, timeout: float = 1.0) -> None:
        with self._lock:
            thread = self._janitor_thread
            self._janitor_thread = None
            self._janitor_stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def enter(
        self,
        *,
        run_token: str,
        workflow_id: str,
        entry_node_id: str,
        source_key: str,
        output_directory: str,
        segment_length: int,
        total_frames: int,
        fps: Fraction,
        start_segment: int = 0,
        start_offset: int = 0,
        continuation: torch.Tensor | None = None,
        resource: object | None = None,
        on_saved: Callable[[WanLoopStatus], None] | None = None,
        display_info: dict | None = None,
        is_prompt_active: Callable[[], bool] | None = None,
        prompt_id: str | None = None,
    ) -> WanEntry:
        length = validate_segment_length(segment_length)
        total = _positive_integer(total_frames, "total_frames")
        frame_rate = _positive_fps(fps)
        if not isinstance(workflow_id, str) or not workflow_id or len(workflow_id) > 256:
            raise WanLoopError("workflow identity is missing or invalid", reason="invalid_workflow_id")
        if not isinstance(entry_node_id, str) or not entry_node_id:
            raise WanLoopError("entry node identity is missing", reason="invalid_entry_node")
        if not isinstance(source_key, str) or not source_key or len(source_key) > 4096:
            raise WanLoopError("video source identity is invalid", reason="invalid_source_key")
        if not isinstance(output_directory, str) or not output_directory or len(output_directory) > 4096:
            raise WanLoopError("output directory identity is invalid", reason="invalid_output_directory")
        token_input = run_token.strip() if isinstance(run_token, str) else ""
        now = self._clock()
        with self._lock:
            self._cleanup_locked(now)
            entry_key = (workflow_id, entry_node_id)
            if not token_input:
                active = self._entry_tokens.get(entry_key)
                if active is not None:
                    raise WanLoopError(
                        "this loop entry already owns an active run",
                        reason="duplicate_start",
                        actual=active,
                    )
                if (
                    isinstance(start_segment, bool) or not isinstance(start_segment, int) or start_segment < 0
                    or isinstance(start_offset, bool) or not isinstance(start_offset, int)
                    or start_offset < 0 or start_offset >= total
                    or (start_segment == 0) != (start_offset == 0)
                ):
                    raise WanLoopError("resume position is invalid", reason="invalid_resume_position")
                if start_segment and (
                    not isinstance(continuation, torch.Tensor)
                    or continuation.device.type != "cpu"
                    or continuation.layout is not torch.strided
                    or continuation.dtype not in {torch.float16, torch.bfloat16, torch.float32}
                    or continuation.ndim != 4
                    or continuation.shape[0] != 1 or continuation.shape[-1] != 3
                    or continuation.shape[1] < 1 or continuation.shape[2] < 1
                    or not continuation.is_contiguous()
                    or continuation.untyped_storage().nbytes() != continuation.numel() * continuation.element_size()
                    or not bool(torch.isfinite(continuation).all().item())
                ):
                    raise WanLoopError("resume requires one owned CPU frame", reason="invalid_continuation")
                token = self._token_factory()
                if not isinstance(token, str) or not token or token in self._runs:
                    raise WanLoopError("internal loop session creation failed", reason="invalid_run_token")
                state = _WanRun(
                    token=token,
                    workflow_id=workflow_id,
                    entry_node_id=entry_node_id,
                    source_key=source_key,
                    output_directory=output_directory,
                    segment_length=length,
                    total_frames=total,
                    fps=frame_rate,
                    segment_index=start_segment,
                    next_offset=start_offset,
                    phase="in_flight",
                    stop_requested=False,
                    continuation=continuation,
                    ready_expires_at=None,
                    total_segments=(start_segment + math.ceil((total - start_offset) / (length - 1))
                                    if start_segment else segment_count(total, length)),
                    resource=resource,
                    on_saved=on_saved,
                    display_info=display_info,
                    is_prompt_active=is_prompt_active,
                    prompt_id=prompt_id,
                )
                self._runs[token] = state
                self._entry_tokens[entry_key] = token
            else:
                token = token_input
                state = self._runs.get(token)
                if state is None:
                    raise WanLoopError("loop session ended; select resume to continue from saved videos", reason="run_not_found")
                expected = (
                    state.workflow_id,
                    state.entry_node_id,
                    state.source_key,
                    state.output_directory,
                    state.segment_length,
                    state.total_frames,
                    state.fps,
                )
                actual = (workflow_id, entry_node_id, source_key, output_directory, length, total, frame_rate)
                if actual != expected:
                    self._drop_locked(token)
                    raise WanLoopError(
                        "running inputs changed; select resume to continue from saved videos with the new settings",
                        reason="run_configuration_changed",
                        actual=actual,
                        expected=expected,
                    )
                if state.stop_requested:
                    self._drop_locked(token)
                    raise WanLoopError("run was stopped", reason="run_stopped", actual=token)
                if state.phase != "ready":
                    raise WanLoopError(
                        "a segment is already in flight for this run",
                        reason="segment_in_flight",
                        actual=state.phase,
                    )
                state.phase = "in_flight"
                state.ready_expires_at = None
                state.is_prompt_active = is_prompt_active
                state.prompt_id = prompt_id
            plan = make_segment_plan(
                run_token=state.token,
                workflow_id=state.workflow_id,
                entry_node_id=state.entry_node_id,
                source_key=state.source_key,
                segment_index=state.segment_index,
                next_offset=state.next_offset,
                segment_length=state.segment_length,
                total_frames=state.total_frames,
                fps=state.fps,
            )
            return WanEntry(plan, state.continuation, state.total_segments, state.display_info)

    def advance(
        self,
        *,
        plan: WanSegmentPlan,
        candidate: WanContinuationCandidate,
        receipt: SegmentVideoReceipt,
        output_root: Path,
    ) -> WanLoopStatus:
        now = self._clock()
        with self._lock:
            self._cleanup_locked(now)
            state = self._runs.get(plan.run_token)
            if state is None:
                raise WanLoopError("loop session ended; select resume to continue from saved videos", reason="run_not_found")
            if state.phase != "in_flight":
                raise WanLoopError("segment is not in flight", reason="invalid_run_phase", actual=state.phase)
            expected_plan = make_segment_plan(
                run_token=state.token,
                workflow_id=state.workflow_id,
                entry_node_id=state.entry_node_id,
                source_key=state.source_key,
                segment_index=state.segment_index,
                next_offset=state.next_offset,
                segment_length=state.segment_length,
                total_frames=state.total_frames,
                fps=state.fps,
            )
            if plan != expected_plan:
                self._drop_locked(state.token)
                raise WanLoopError("segment plan does not match active state", reason="plan_mismatch")
            if candidate.run_token != state.token or candidate.segment_index != state.segment_index:
                self._drop_locked(state.token)
                raise WanLoopError("continuation candidate does not match active state", reason="candidate_mismatch")

            expected_path: Path | None = None
            receipt_path: Path | None = None
            try:
                expected_path = expected_segment_video_path(
                    output_root,
                    state.output_directory,
                    state.segment_index,
                ).resolve(strict=True)
                raw_receipt_path = Path(receipt.path)
                if raw_receipt_path.is_absolute():
                    receipt_path = raw_receipt_path.resolve(strict=True)
            except (OSError, RuntimeError, TypeError, RVKError):
                # A receipt is only a completion gate when its published file is
                # the current run/index path under the caller's trusted root.
                receipt_path = None
            if (
                receipt.segment_index != state.segment_index
                or receipt.frame_count != plan.delivery_frames
                or receipt.fps != state.fps
                or receipt_path is None
                or expected_path is None
                or receipt_path != expected_path
                or not receipt_path.is_file()
            ):
                self._drop_locked(state.token)
                raise WanLoopError(
                    "saved video receipt does not match the current segment",
                    reason="receipt_mismatch",
                    actual={
                        "segment_index": receipt.segment_index,
                        "frame_count": receipt.frame_count,
                        "fps": str(receipt.fps),
                        "path": receipt.path,
                    },
                    expected={
                        "segment_index": state.segment_index,
                        "frame_count": plan.delivery_frames,
                        "fps": str(state.fps),
                        "path": None if expected_path is None else str(expected_path),
                    },
                )
            if plan.has_more:
                image = candidate.image
                if (
                    not isinstance(image, torch.Tensor)
                    or image.device.type != "cpu"
                    or image.layout is not torch.strided
                    or not image.is_contiguous()
                    or image.ndim != 4
                    or tuple(image.shape[:1]) != (1,)
                    or int(image.shape[-1]) != 3
                ):
                    self._drop_locked(state.token)
                    raise WanLoopError(
                        "continuation must be one independent contiguous CPU image",
                        reason="invalid_continuation",
                    )
            elif candidate.image is not None:
                self._drop_locked(state.token)
                raise WanLoopError("final segment must not retain continuation", reason="unexpected_continuation")

            stopped = state.stop_requested
            has_more = plan.has_more and not stopped
            status = WanLoopStatus(
                run_token=state.token,
                workflow_id=state.workflow_id,
                entry_node_id=state.entry_node_id,
                completed_segment=state.segment_index,
                next_segment=state.segment_index + 1,
                completed_frames=min(plan.next_offset, state.total_frames),
                total_frames=state.total_frames,
                next_offset=plan.next_offset,
                has_more=has_more,
                stopped=stopped,
                path=receipt.path,
                total_segments=state.total_segments,
            )
            if state.on_saved is not None:
                try:
                    state.on_saved(status)
                except Exception:
                    # The formal video remains authoritative if its advisory
                    # summary could not be written; a new resume can scan it.
                    self._drop_locked(state.token)
                    raise
            if not has_more:
                self._drop_locked(state.token)
            else:
                state.continuation = candidate.image
                state.segment_index += 1
                state.next_offset = plan.next_offset
                state.phase = "ready"
                state.ready_expires_at = now + self._ttl_seconds
            return status

    def request_stop(self, token: str) -> bool:
        now = self._clock()
        with self._lock:
            self._cleanup_locked(now)
            state = self._runs.get(token)
            if state is None:
                return False
            state.stop_requested = True
            if state.phase == "ready":
                self._drop_locked(token)
            else:
                state.ready_expires_at = None
            return True

    def abort(self, token: str) -> bool:
        with self._lock:
            return self._drop_locked(token) is not None

    def abort_after_prompt(self, token: str) -> bool:
        """A browser error event may precede the worker leaving the live queue."""
        with self._lock:
            state = self._runs.get(token)
            if state is None:
                return False
            if state.is_prompt_active is not None and not self._prompt_finished(state):
                state.stop_requested = True
                return True
            return self._drop_locked(token) is not None

    def inspect(self, token: str) -> WanRunInspection | None:
        now = self._clock()
        with self._lock:
            self._cleanup_locked(now)
            state = self._runs.get(token)
            if state is None:
                return None
            image = state.continuation
            return WanRunInspection(
                run_token=state.token,
                workflow_id=state.workflow_id,
                entry_node_id=state.entry_node_id,
                segment_index=state.segment_index,
                next_offset=state.next_offset,
                phase=state.phase,
                stop_requested=state.stop_requested,
                continuation_frames=0 if image is None else int(image.shape[0]),
                continuation_numel=0 if image is None else int(image.numel()),
                continuation_device=None if image is None else image.device.type,
                continuation_contiguous=False if image is None else image.is_contiguous(),
            )

    def clear(self) -> None:
        with self._lock:
            for token in tuple(self._runs):
                self._drop_locked(token)


__all__ = [
    "DEFAULT_RUN_TTL_SECONDS",
    "DEFAULT_SEGMENT_LENGTH",
    "MAX_SEGMENT_LENGTH",
    "MIN_SEGMENT_LENGTH",
    "WanContinuationCandidate",
    "WanEntry",
    "WanLoopError",
    "WanLoopStatus",
    "WanRunInspection",
    "WanRunRegistry",
    "WanSegmentPlan",
    "collect_wan_segment",
    "make_segment_plan",
    "segment_count",
    "validate_segment_length",
]
