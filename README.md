# Richard's Video Kits (RVK)

[简体中文](README.zh-CN.md)

ComfyUI nodes for saving video segments, running an editable Wan Animate 2 workflow across a driving video, and assembling the finished segments with the original audio.

Configure one Motion Transfer graph and queue once. RVK saves each completed segment before queuing the next. After a stop, failure or restart, choose `resume` to continue from complete segment videos in the working directory.

Version **1.1.1** fixes an RVK frontend bug in `1.1.0` that could prevent unrelated workflows from queuing with a `DataCloneError`, even when they contained no RVK nodes. Upgrade from `1.1.0`. Requests without Loop Entry now pass through unchanged, RVK requests accept JSON-compatible frontend Proxy objects without cloning the entire graph, and loading an unrelated graph no longer modifies its metadata.

Video-based resume, status controls on Loop Entry, separate final-output directories and Finalize's standard `VIDEO` output remain available. Check the [Comfy Registry listing](https://registry.comfy.org/richard34512/richards-video-kits) for available versions and their review status; publication does not mean scan approval. Refresh Manager metadata if an available version is not listed.

**Registry publication:** `1.1.1` was published on 2026-10-01 (Australia/Sydney). Public search, version-list and installation endpoints return the patch. All 27 downloaded files match the release tag, package links pass, and isolated CPU import registers all five nodes. The scan status was **Pending** at verification; scan approval, a Manager UI installation and real-browser/GPU validation of this patch have not been established.

## Nodes

| Node | Purpose |
| --- | --- |
| RVK Save Segment Video | Save an IMAGE batch as a verified, silent H.264 MP4. Usable without Wan. |
| RVK Finalize Segments | Join completed segments, add the driving video's audio, and output the completed VIDEO for further processing. Usable without model inference. |
| RVK Wan Animate 2 Loop Entry | Start or resume a run, inspect its progress and plan its windows. |
| RVK Wan Animate 2 Collect | Prepare the delivered frames and minimal in-memory continuation. |
| RVK Wan Animate 2 Advance | Confirm the saved segment and advance the loop. |

## Installation

Version 1.1.1 targets **Windows / NTFS, Python 3.12, ComfyUI v0.38.0, frontend 1.53.6**, with `--cache-classic`. Before this patch, the user reported a completed 15-segment run on this baseline; saved media was checked as 1173 frames / 39.1 seconds with audio. This is functional evidence for the loop, not a browser/GPU validation of the patch or a complete resource or failure-recovery validation. Historical Wan inference tests used v0.36.0 / frontend 1.52.7 on an RTX 4080.

1. In ComfyUI Manager, search for **Richard's Video Kits** / `richards-video-kits` and select **1.1.1** when available. Registry review status and Manager metadata refreshes can affect availability. The GitHub installation below uses the current `main` source.
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
3. Set `segment_length` on **Loop Entry**: `81` is the default; accepted values are `4k+1` from 5 through 16381. Longer segments require more memory. Keep the existing length connection to Motion Transfer.
4. Set `work_directory` once on **Loop Entry**, relative to ComfyUI's output directory. Keep its connections to Save and Finalize's `segment_directory`. Choose `new` for a missing or empty directory, or `resume` for a previous run's directory. Finalize's separate `output_directory` selects the finished video's destination; leave it empty to use the segment directory.
5. Click **检查/刷新状态** (check/refresh status) on Entry. Check current and recorded video/image filenames, completed frames and segments, and the planned segment count. Filenames are only a manual aid: RVK does not compare tokens, prompts or input identities. You choose whether this is the intended run directory.
6. Queue once and keep the original workflow open. Do not edit inputs or the graph during a run. Use **当前片段结束后停止** (stop after the current segment) on Entry to stop further segments. The current segment may finish.

See [workflow usage and input constraints](examples/wan_animate2/README.md).

## Outputs and recovery

Each segment is saved as `segment_0000.mp4`, `segment_0001.mp4`, and so on. These videos are silent. When all frames are complete, Finalize joins the video streams without re-encoding and adds the first original audio track as AAC to `final.mp4`. Segment files are retained; existing outputs are never overwritten.

Finalize's fourth output, `video` (`VIDEO`), references that completed file with its audio. Connect it to a VIDEO consumer or **Get Video Components** for images, audio and fps. The existing `result`, `path` and `completed` ports keep their positions. During intermediate rounds or after Stop, the video branch skips downstream execution. Returning VIDEO does not decode the whole file; a downstream frame extractor can allocate a full image batch. After updating and restarting ComfyUI, refresh the browser and re-add Finalize if an old workflow still shows only three outputs.

After stopping or restarting, select `resume`, inspect the directory and queue again. RVK validates contiguous complete videos, derives progress from their actual frame counts and reads the previous segment's last frame for continuation. This includes compression and postprocessing effects; it is not an exact restoration of model state. A lightweight `rvk_progress.json` records progress and filenames, but the saved videos determine progress if that summary is missing or outdated. No user `run_token` is required.

If every segment already exists, `resume` runs final assembly without Wan inference. You can also use [the standalone finalization workflow](examples/wan_animate2/finalize_existing_segments.json) with the same original video and segment directory. Existing final files are never overwritten: choose another destination or filename when retrying assembly.

Files ending in `.partial.mp4` do not count as complete. Resume preserves recognized leftover segment partials under `rvk_failed/` once it has exclusive access to the directory. Final-video partials still require manual inspection after ensuring no active writer is using them. Standalone Finalize rejects unresolved partials. A `cleanup_required` error can mean that a completed file and its partial link both exist.

After updating, restart ComfyUI and refresh the browser. Save a copy of older workflows before migration: the old token control becomes `mode=new`, old token-output links are removed, and the remaining output positions stay unchanged. Select `resume` explicitly for existing work. Finalize's original directory input now displays as `segment_directory`; its added `output_directory` is the destination and defaults to the same directory when empty.

## Tested limits

- Patch evidence: 75 passing Node/VM frontend regression tests, including 18 new isolation and Proxy tests that fail against the previous code. These tests do not establish real-browser or GPU behavior; the user's original workflow has not yet been rerun against the patch.
- Earlier evidence: CPU/media checks and the user-reported 15-segment / 1173-frame completion described above. Historical evidence includes real three-segment Wan runs with a short tail, public Stop, and separate 12-segment / 903-frame finalization with AAC audio.
- The short Wan test used a silent input, so its finalization correctly rejected missing audio; it was not an end-to-end audio test.
- Not validated: a full three-minute resource profile, interruption during active inference, segments taking more than 30 minutes, other filesystems, or equivalent memory behavior with the default RAM-pressure cache.
- Input must be file-backed CFR video with an exactly representable frame timeline. Missing, short or offset audio is rejected by Finalize. Saved segments remain usable when finalization fails.
- Resume is explicitly selected by the user; there are no persistent tensors, permanent image sequences, model downloads or bundled weights. Recovery rejects gaps, incompatible or damaged segments, and completed frames exceeding the current driving video's length.

Current test baseline: [`v0.38.0`, `6b747c0428c343e1417219641db93a4fb7cb69ae`](https://github.com/Comfy-Org/ComfyUI/releases/tag/v0.38.0). The example template and historical Wan evidence remain based on v0.36.0. See [tests](https://github.com/Richard-Wang-fs/ComfyUI-RichardsVideoKits/blob/main/tests/README.md) for maintainer checks, [NOTICE](NOTICE.md) for workflow attribution and [PUBLISHING](https://github.com/Richard-Wang-fs/ComfyUI-RichardsVideoKits/blob/main/PUBLISHING.md) for release maintenance.

## License

RVK's original code is available under the [PolyForm Noncommercial License 1.0.0](LICENSE). This is **source-available software for noncommercial purposes**, not an OSI-approved open source license. Commercial use is not granted by this license; contact [Richard-Wang-fs](https://github.com/Richard-Wang-fs) for separate permission.

The upstream workflow template material retains its [MIT notice](licenses/ComfyUI-workflow-templates-MIT.txt). The software license does not grant rights in model weights, input media or generated media; their applicable terms remain separate.
