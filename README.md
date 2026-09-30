# Richard's Video Kits (RVK)

[简体中文](README.zh-CN.md)

ComfyUI nodes for saving video segments, running an editable Wan Animate 2 workflow across a driving video, and assembling the finished segments with the original audio.

Configure one Motion Transfer graph and queue once. RVK saves each completed segment before queuing the next, keeping continuation in memory. Completed videos survive a stopped run; generation does not resume from disk checkpoints.

Source version: **1.1.0**. Finalize now exposes the completed video through a standard `VIDEO` output. This update is available from this GitHub repository; the previously published [Comfy Registry](https://registry.comfy.org/richard34512/richards-video-kits) version is **1.0.0**, which does not include the new output.

## Nodes

| Node | Purpose |
| --- | --- |
| RVK Save Segment Video | Save an IMAGE batch as a verified, silent H.264 MP4. Usable without Wan. |
| RVK Finalize Segments | Join completed segments, add the driving video's audio, and output the completed VIDEO for further processing. Usable without model inference. |
| RVK Wan Animate 2 Loop Entry | Start a run, plan its windows and select an empty output directory. |
| RVK Wan Animate 2 Collect | Prepare the delivered frames and minimal in-memory continuation. |
| RVK Wan Animate 2 Advance | Confirm the saved segment and advance the loop. |

## Installation

Version 1.1.0 targets **Windows / NTFS, Python 3.12, ComfyUI v0.38.0, frontend 1.53.6**, with `--cache-classic`. Its final-video output has focused CPU validation, including the official Get Video Components consumer and downstream blocking while waiting or stopped. Historical Wan inference tests used v0.36.0 / frontend 1.52.7 on an RTX 4080; the complete Wan/GPU validation has not been repeated on v0.38.0.

1. For **1.1.0 and the new VIDEO output**, use the GitHub installation below. ComfyUI Manager's Registry version `1.0.0` remains available as **Richard's Video Kits** / `richards-video-kits`; it does not contain this update.
2. Keep only one RVK installation. Its `__init__.py`, `rvk/`, and `web/` must be directly inside one directory under `ComfyUI/custom_nodes/`.
3. RVK uses PyAV, NumPy and PyTorch from ComfyUI's existing environment and adds no pip dependencies. For final assembly, ensure that **FFmpeg with AAC encoding** is available on the ComfyUI process's `PATH`; FFmpeg 7.0.2 was tested.
4. Start ComfyUI with `--cache-classic`, refresh the browser, search for the five nodes above, and import [the loop workflow](examples/wan_animate2/wan_animate2_rvk_loop.json).

For a fresh installation, run this from your ComfyUI directory while ComfyUI is stopped:

```sh
git clone https://github.com/Richard-Wang-fs/ComfyUI-RichardsVideoKits.git custom_nodes/ComfyUI-RichardsVideoKits
```

If you already have RVK installed under another directory name, stop ComfyUI and move that installation outside `custom_nodes` before cloning. Keep it as a backup; loading two copies can cause conflicts. Your videos remain in ComfyUI's output directory.

## Quick start

1. Select your reference image and full, untrimmed **constant-frame-rate (CFR)** driving video. The video must include audio if you want a final video with sound.
2. Select the official Wan Animate 2 models and edit the prompt in the Motion Transfer subgraph. Models and media are not included.
3. Set `length` on **Loop Entry**: `81` is the default; accepted values are `4k+1` from 5 through 16381. Longer segments require more memory. Keep the existing length connection to Motion Transfer.
4. Set a new, empty directory relative to ComfyUI's output directory on **Loop Entry**. It is already connected to Save and Finalize.
5. Queue once and keep the workflow open. Do not edit its inputs or graph during a run. Use **Stop RVK after current segment** on Loop Entry to stop further segments.

See [workflow usage and input constraints](examples/wan_animate2/README.md).

## Outputs and recovery

Each segment is saved as `segment_0000.mp4`, `segment_0001.mp4`, and so on. These videos are silent. When all frames are complete, Finalize joins the video streams without re-encoding and adds the first original audio track as AAC to `final.mp4`. Segment files are retained; existing outputs are never overwritten.

Finalize's fourth output, `video` (`VIDEO`), references that completed file with its audio. Connect it to a VIDEO consumer or **Get Video Components** for images, audio and fps. The existing `result`, `path` and `completed` ports keep their positions. During intermediate rounds or after Stop, the video branch skips downstream execution. Returning VIDEO does not decode the whole file; a downstream frame extractor can allocate a full image batch. After updating and restarting ComfyUI, refresh the browser and re-add Finalize if an old workflow still shows only three outputs.

If every segment already exists, use [the standalone finalization workflow](examples/wan_animate2/finalize_existing_segments.json) with the same original video and segment directory. It performs no Wan inference. An incomplete run must start again in a new directory; RVK does not reconstruct model state from saved videos.

Files ending in `.partial.mp4` are incomplete outputs. Investigate the reported error before manually removing a partial file, and ensure no active writer is using it. A `cleanup_required` error can mean that a completed file and its partial link both exist.

## Tested limits

- Verified: real three-segment Wan runs with a short tail, public Stop, and a separate 12-segment / 903-frame finalization with AAC audio.
- The short Wan test used a silent input, so its finalization correctly rejected missing audio; it was not an end-to-end audio test.
- Not validated: a full three-minute resource profile, interruption during active inference, segments taking more than 30 minutes, other filesystems, or equivalent memory behavior with the default RAM-pressure cache.
- Input must be file-backed CFR video with an exactly representable frame timeline. Missing, short or offset audio is rejected by Finalize. Saved segments remain usable when finalization fails.
- No persistent tensors, image sequences, automatic generation resume, model downloads or bundled weights.

Current final-output test baseline: [`v0.38.0`, `6b747c0428c343e1417219641db93a4fb7cb69ae`](https://github.com/Comfy-Org/ComfyUI/releases/tag/v0.38.0). The example template and historical Wan evidence remain based on v0.36.0. See [NOTICE](NOTICE.md) for workflow attribution and [PUBLISHING](PUBLISHING.md) for release maintenance.

## License

RVK's original code is available under the [PolyForm Noncommercial License 1.0.0](LICENSE). This is **source-available software for noncommercial purposes**, not an OSI-approved open source license. Commercial use is not granted by this license; contact [Richard-Wang-fs](https://github.com/Richard-Wang-fs) for separate permission.

The upstream workflow template material retains its [MIT notice](licenses/ComfyUI-workflow-templates-MIT.txt). The software license does not grant rights in model weights, input media or generated media; their applicable terms remain separate.
