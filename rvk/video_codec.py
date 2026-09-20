"""Small PyAV/libx264 codec layer with no RVK persistence dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import torch

from rvk.errors import AssetInvalid, EncoderUnavailable, NoProgress


VIDEO_ENCODER = "libx264"
VIDEO_CODEC = "h264"
VIDEO_CONTAINER = "mp4"
VIDEO_PIXEL_FORMAT = "yuv420p"
MAX_VIDEO_FRAMES_PER_SEGMENT = 1_000_000
MAX_IMAGE_DIMENSION = 16_384
MAX_IMAGE_PIXELS = 64 * 1024 * 1024
SUPPORTED_FRAME_DTYPES = frozenset((torch.float16, torch.bfloat16, torch.float32))
ALLOWED_PRESETS = (
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
)


@dataclass(frozen=True, slots=True)
class VideoInspection:
    frame_count: int
    width: int
    height: int
    fps: Fraction
    time_base: Fraction
    pts: tuple[int, ...]
    duration: int
    container: str
    codec: str
    pixel_format: str
    color_primaries: int
    color_transfer: int
    color_matrix: int
    color_range: int
    audio_streams: int


def av_module():
    try:
        import av
    except (ImportError, OSError) as exc:
        raise EncoderUnavailable(
            "PyAV cannot be imported from the selected media environment",
            reason="backend_import_failed",
        ) from exc
    return av


def validate_frame_batch(frames: torch.Tensor) -> tuple[int, int, int]:
    """Validate one dense finite ComfyUI IMAGE batch."""

    if not isinstance(frames, torch.Tensor):
        raise AssetInvalid("images must be a torch.Tensor", reason="invalid_frame_batch")
    if frames.layout is not torch.strided or frames.dtype not in SUPPORTED_FRAME_DTYPES:
        raise AssetInvalid(
            "images must use a supported dense floating dtype",
            reason="unsupported_frame_dtype",
            actual={"layout": str(frames.layout), "dtype": str(frames.dtype)},
        )
    if (
        frames.ndim != 4
        or frames.shape[0] <= 0
        or frames.shape[1] <= 0
        or frames.shape[2] <= 0
        or frames.shape[3] != 3
    ):
        raise AssetInvalid(
            "images must have positive [T,H,W,3] shape",
            reason="invalid_frame_shape",
            actual=tuple(frames.shape),
        )
    count, height, width = (int(frames.shape[0]), int(frames.shape[1]), int(frames.shape[2]))
    if count > MAX_VIDEO_FRAMES_PER_SEGMENT:
        raise AssetInvalid(
            "image batch exceeds the frame limit",
            reason="resource_limit",
            actual=count,
            expected=MAX_VIDEO_FRAMES_PER_SEGMENT,
        )
    if (
        height > MAX_IMAGE_DIMENSION
        or width > MAX_IMAGE_DIMENSION
        or height * width > MAX_IMAGE_PIXELS
    ):
        raise AssetInvalid(
            "frame dimensions exceed the media limit",
            reason="resource_limit",
            actual=(height, width),
        )
    if height % 2 or width % 2:
        raise AssetInvalid(
            "yuv420p video requires even frame dimensions",
            reason="invalid_frame_dimensions",
            actual={"width": width, "height": height},
        )
    if not bool(torch.isfinite(frames).all().item()):
        raise AssetInvalid("images must contain only finite values", reason="non_finite_frames")
    return count, height, width


def rgb8_frame(frame: torch.Tensor) -> torch.Tensor:
    """Apply the existing RGB8 floor quantizer to one frame."""

    return (
        frame.detach()
        .to(device="cpu", dtype=torch.float32)
        .clamp(0.0, 1.0)
        .mul(255.0)
        .floor()
        .to(dtype=torch.uint8)
        .contiguous()
    )


def encode_video_file(
    destination: Path,
    frames: torch.Tensor,
    *,
    fps: Fraction,
    crf: int,
    preset: str,
) -> None:
    """Encode a validated IMAGE batch and fully flush/close the container."""

    av = av_module()
    try:
        from av.video.reformatter import ColorPrimaries, ColorRange, ColorTrc, Colorspace, VideoReformatter

        container = av.open(str(destination), mode="w", format=VIDEO_CONTAINER)
        try:
            stream = container.add_stream(
                VIDEO_ENCODER,
                rate=fps,
                options={"crf": str(crf), "preset": preset, "bf": "0"},
            )
            stream.width = int(frames.shape[2])
            stream.height = int(frames.shape[1])
            stream.pix_fmt = VIDEO_PIXEL_FORMAT
            target_time_base = Fraction(fps.denominator, fps.numerator)
            stream.time_base = target_time_base
            context = stream.codec_context
            context.time_base = target_time_base
            context.color_primaries = 1
            context.color_trc = 1
            context.colorspace = 1
            context.color_range = 1
            reformatter = VideoReformatter()
            packets = 0
            for index in range(int(frames.shape[0])):
                rgb = av.VideoFrame.from_ndarray(rgb8_frame(frames[index]).numpy(), format="rgb24")
                rgb.pts = index
                rgb.time_base = target_time_base
                rgb.color_primaries = 1
                rgb.color_trc = 13  # IEC 61966-2-1 / sRGB input transfer.
                rgb.colorspace = 1
                rgb.color_range = 2  # Full-range RGB input.
                yuv = reformatter.reformat(
                    rgb,
                    format=VIDEO_PIXEL_FORMAT,
                    src_colorspace=Colorspace.ITU709,
                    dst_colorspace=Colorspace.ITU709,
                    src_color_range=ColorRange.JPEG,
                    dst_color_range=ColorRange.MPEG,
                    dst_color_trc=ColorTrc.BT709,
                    dst_color_primaries=ColorPrimaries.BT709,
                )
                yuv.pts = index
                yuv.time_base = target_time_base
                for packet in stream.encode(yuv):
                    container.mux(packet)
                    packets += 1
            for packet in stream.encode():
                container.mux(packet)
                packets += 1
            if packets == 0:
                raise NoProgress("video encoder produced no packets", reason="encoder_no_packets")
        finally:
            container.close()
    except (AssetInvalid, EncoderUnavailable, NoProgress, OSError):
        raise
    except Exception as exc:
        raise AssetInvalid(
            "PyAV failed while encoding, flushing, or closing the video",
            path=str(destination),
            reason="video_encode_failed",
            actual=str(exc),
        ) from exc


def inspect_video_file(path: Path) -> VideoInspection:
    """Decode every frame and report actual timing and stream properties."""

    av = av_module()
    try:
        with av.open(str(path), mode="r") as container:
            if len(container.streams.video) != 1 or len(container.streams.audio) != 0 or len(container.streams) != 1:
                raise AssetInvalid(
                    "video must contain exactly one video stream and no audio",
                    path=str(path),
                    reason="media_content_mismatch",
                )
            stream = container.streams.video[0]
            context = stream.codec_context
            if stream.time_base is None or stream.average_rate is None or stream.duration is None:
                raise AssetInvalid(
                    "video timing metadata is incomplete",
                    path=str(path),
                    reason="media_content_mismatch",
                )
            if (
                context.width <= 0
                or context.height <= 0
                or context.width > MAX_IMAGE_DIMENSION
                or context.height > MAX_IMAGE_DIMENSION
                or context.width * context.height > MAX_IMAGE_PIXELS
            ):
                raise AssetInvalid(
                    "video dimensions exceed the media limit",
                    path=str(path),
                    reason="resource_limit",
                )
            pts: list[int] = []
            width = height = 0
            for frame in container.decode(stream):
                if frame.pts is None or frame.time_base is None:
                    raise AssetInvalid(
                        "decoded video frame has no PTS",
                        path=str(path),
                        reason="media_content_mismatch",
                    )
                pts.append(int(frame.pts))
                if width and (frame.width != width or frame.height != height):
                    raise AssetInvalid(
                        "decoded video dimensions change between frames",
                        path=str(path),
                        reason="media_content_mismatch",
                    )
                width, height = frame.width, frame.height
                if len(pts) > MAX_VIDEO_FRAMES_PER_SEGMENT:
                    raise AssetInvalid(
                        "decoded video exceeds the frame limit",
                        path=str(path),
                        reason="resource_limit",
                    )
            if not pts:
                raise NoProgress(
                    "video contains no decodable frames",
                    path=str(path),
                    reason="decoder_no_frames",
                )
            return VideoInspection(
                len(pts),
                width,
                height,
                Fraction(stream.average_rate),
                Fraction(stream.time_base),
                tuple(pts),
                int(stream.duration),
                str(container.format.name),
                str(context.name),
                str(context.format.name) if context.format is not None else "",
                int(context.color_primaries),
                int(context.color_trc),
                int(context.colorspace),
                int(context.color_range),
                len(container.streams.audio),
            )
    except (AssetInvalid, EncoderUnavailable, NoProgress):
        raise
    except Exception as exc:
        if isinstance(exc, (OSError, getattr(av.error, "FFmpegError", ()))):
            raise AssetInvalid(
                "video cannot be demuxed and fully decoded",
                path=str(path),
                reason="invalid_video",
                actual=str(exc),
            ) from exc
        raise


def validate_encoder_options(path: Path, *, crf: int, preset: str) -> None:
    """Verify that the requested libx264 options were recorded in the bitstream."""

    preset_signatures = {
        "ultrafast": (b" ref=1 ", b" me=dia ", b" subme=0 "),
        "superfast": (b" ref=1 ", b" me=dia ", b" subme=1 "),
        "veryfast": (b" ref=1 ", b" me=hex ", b" subme=2 "),
        "faster": (b" ref=2 ", b" me=hex ", b" subme=4 "),
        "fast": (b" ref=2 ", b" me=hex ", b" subme=6 "),
        "medium": (b" ref=3 ", b" me=hex ", b" subme=7 "),
        "slow": (b" ref=5 ", b" me=hex ", b" subme=8 "),
        "slower": (b" ref=8 ", b" me=umh ", b" subme=9 "),
        "veryslow": (b" ref=16 ", b" me=umh ", b" subme=10 "),
    }
    markers = {b"x264 - core", b" bframes=0 ", *preset_signatures[preset]}
    if crf == 0:
        markers.update({b" rc=cqp ", b" qp=0"})
    else:
        markers.add(f" crf={crf}.0 ".encode("ascii"))
    found: set[bytes] = set()
    tail = b""
    try:
        with path.open("rb") as handle:
            while len(found) != len(markers):
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                window = tail + chunk
                found.update(marker for marker in markers if marker in window)
                tail = window[-128:]
    except OSError as exc:
        raise AssetInvalid(
            "video encoder options cannot be read",
            path=str(path),
            reason="invalid_video",
        ) from exc
    if found != markers:
        raise AssetInvalid(
            "H.264 encoder options differ from the requested libx264 profile",
            path=str(path),
            reason="media_content_mismatch",
            actual={"missing_markers": sorted(marker.decode("ascii") for marker in markers - found)},
        )
