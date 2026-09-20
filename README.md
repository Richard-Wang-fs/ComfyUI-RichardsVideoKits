# Richard's Video Kits (RVK)

[简体中文](README.zh-CN.md)

ComfyUI nodes for saving video segments, running an editable Wan Animate 2 workflow across a driving video, and assembling the finished segments with the original audio.

Configure one Motion Transfer graph and queue once. RVK saves each completed segment before queuing the next, keeping continuation in memory. Completed videos survive a stopped run; generation does not resume from disk checkpoints.

Version: **1.0.0**. This is a release candidate repository for the stable runtime. GitHub publication and Comfy Registry listing are pending; Manager search installation is not available yet.

## Nodes

| Node | Purpose |
| --- | --- |
| RVK Save Segment Video | Save an IMAGE batch as a verified, silent H.264 MP4. Usable without Wan. |
| RVK Finalize Segments | Join completed segments and add the driving video's audio. Usable without model inference. |
| RVK Wan Animate 2 Loop Entry | Start a run, plan its windows and select an empty output directory. |
| RVK Wan Animate 2 Collect | Prepare the delivered frames and minimal in-memory continuation. |
| RVK Wan Animate 2 Advance | Confirm the saved segment and advance the loop. |

## Installation

The tested configuration is **Windows / NTFS, Python 3.12, ComfyUI v0.36.0, frontend 1.52.7**, with `--cache-classic`. Wan inference was tested on an RTX 4080. Other configurations have not been validated.

1. Stop ComfyUI. Put this repository in one directory under `ComfyUI/custom_nodes/`. Its `__init__.py`, `rvk/`, and `web/` must be directly inside that directory. Keep only one RVK installation.
2. Use ComfyUI's existing Python environment with PyAV, NumPy and PyTorch. RVK adds no pip installation step. For final assembly, ensure that **FFmpeg with AAC encoding** is available on the ComfyUI process's `PATH`; FFmpeg 7.0.2 was tested.
3. Start ComfyUI with `--cache-classic`, refresh the browser, and search for the five nodes above.
4. Import [the loop workflow](examples/wan_animate2/wan_animate2_rvk_loop.json).

Once this project is published to Comfy Registry, installation through Manager will become a separate supported route. Registry publication status must be checked before using that route.

## Quick start

1. Select your reference image and full, untrimmed **constant-frame-rate (CFR)** driving video. The video must include audio if you want a final video with sound.
2. Select the official Wan Animate 2 models and edit the prompt in the Motion Transfer subgraph. Models and media are not included.
3. Set `length` on **Loop Entry**: `81` is the default; accepted values are `4k+1` from 5 through 16381. Longer segments require more memory. Keep the existing length connection to Motion Transfer.
4. Set a new, empty directory relative to ComfyUI's output directory on **Loop Entry**. It is already connected to Save and Finalize.
5. Queue once and keep the workflow open. Do not edit its inputs or graph during a run. Use **Stop RVK after current segment** on Loop Entry to stop further segments.

See [workflow usage and input constraints](examples/wan_animate2/README.md).

## Outputs and recovery

Each segment is saved as `segment_0000.mp4`, `segment_0001.mp4`, and so on. These videos are silent. When all frames are complete, Finalize joins the video streams without re-encoding and adds the first original audio track as AAC to `final.mp4`. Segment files are retained; existing outputs are never overwritten.

If every segment already exists, use [the standalone finalization workflow](examples/wan_animate2/finalize_existing_segments.json) with the same original video and segment directory. It performs no Wan inference. An incomplete run must start again in a new directory; RVK does not reconstruct model state from saved videos.

Files ending in `.partial.mp4` are incomplete outputs. Investigate the reported error before manually removing a partial file, and ensure no active writer is using it. A `cleanup_required` error can mean that a completed file and its partial link both exist.

## Tested limits

- Verified: real three-segment Wan runs with a short tail, public Stop, and a separate 12-segment / 903-frame finalization with AAC audio.
- The short Wan test used a silent input, so its finalization correctly rejected missing audio; it was not an end-to-end audio test.
- Not validated: a full three-minute resource profile, interruption during active inference, segments taking more than 30 minutes, other filesystems, or equivalent memory behavior with the default RAM-pressure cache.
- Input must be file-backed CFR video with an exactly representable frame timeline. Missing, short or offset audio is rejected by Finalize. Saved segments remain usable when finalization fails.
- No persistent tensors, image sequences, automatic generation resume, model downloads or bundled weights.

ComfyUI baseline: [`v0.36.0`, `ee71d5c4993f29086b27fde1629a945ae48425bf`](https://github.com/Comfy-Org/ComfyUI/releases/tag/v0.36.0). See [NOTICE](NOTICE.md) for workflow attribution and [PUBLISHING](PUBLISHING.md) for remaining publication steps.
