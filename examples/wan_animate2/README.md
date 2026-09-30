# Wan Animate 2 workflows

Import `wan_animate2_rvk_loop.json` for generation, or `finalize_existing_segments.json` to assemble an already complete set of segments. The second workflow has no model nodes.

The generation workflow retains one editable official **Motion Transfer (Wan Animate 2)** subgraph. Select your own reference image and full driving video, then choose the required models inside the subgraph. Model names in the workflow are references, not bundled files or automatic downloads.

Set segment length and `work_directory` on **RVK Wan Animate 2 Loop Entry**. The directory is relative to ComfyUI's output directory; keep its connections to Save and Finalize's `segment_directory` so it is entered only once. Select `new` for a missing or empty directory, or `resume` for an interrupted run's directory. Set Finalize's independent `output_directory` and `final_filename` for the finished video; an empty destination uses the segment directory.

Click **检查/刷新状态** (check/refresh status) before queuing. Entry shows current and recorded video/image filenames, completed frames and segments, and planned segments. The check does not run a model or modify the directory. Names are for manual comparison: no token, prompt or input fingerprint decides whether this is the same task. Missing historical names remain unknown. Users no longer supply `run_token`.

The example uses the official `WanAnimate2Cache` with `device=cpu`, as tested on a 16 GiB GPU. This reduces GPU memory pressure but can be slower. The setting remains editable inside the subgraph.

Queue once. Keep Entry at the top level and the original workflow open and unchanged while RVK queues successive segments. A segment must be saved successfully before the next is queued. Switching or closing the workflow stops continuation. The Entry button **当前片段结束后停止** (stop after the current segment) prevents the next segment; the current segment may finish. Completed MP4 files remain on disk.

After a stop, failure or restart, choose `resume`, check the directory and queue again. RVK verifies complete contiguous segments starting at index zero and uses their actual frame counts to find the next window. It reads the last frame of the previous saved segment for continuation; that frame includes compression and postprocessing effects, so recovery does not exactly restore the uninterrupted model state. No manual continuation-image input is provided. A lightweight `rvk_progress.json` records filenames and progress, but videos determine progress when that summary is missing, outdated or damaged. Only one active writer may use a work directory; after a browser disconnect, wait for the existing execution to finish before resuming.

Each segment is silent. Finalize waits until all frames are complete, then adds the first audio track from the original input. If your input has no audio, segment generation can complete but Finalize reports `source_audio_missing`.

Finalize's fourth `video` (`VIDEO`) output references the complete file with audio and connects to VIDEO consumers or **Get Video Components**. Existing result/path/completed slots keep their positions. Intermediate rounds and Stop block the video branch, so downstream processing starts only after the final file has been published. Returning VIDEO does not decode the whole file; downstream frame extraction can allocate a full image batch. Refresh after updating and restarting ComfyUI; re-add an old Finalize node if the new port is missing.

If every segment is complete, the loop's `resume` mode runs final assembly without Wan inference. For standalone finalization, select the same untrimmed input video used for generation and point Finalize to the existing segment directory. Segments must have compatible video properties and cover exactly the input's frame count. Existing final files are never overwritten; choose a different destination or filename when retrying.

Missing indexes, damaged or incompatible media, and completed frames exceeding the current source length prevent resume. `.partial.mp4` files are not complete segments. Once resume has exclusive access, recognized leftover RVK segment partials are preserved under `rvk_failed/`. Final-video partials still require manual inspection after ensuring no active writer is using them. Standalone Finalize rejects unresolved partials. A `cleanup_required` error can mean that a published file and its partial link both remain.

The source audio must begin at video time zero, allowing at most two audio samples of timestamp rounding. Larger positive or negative offsets are rejected; RVK does not insert silence, trim or shift audio. The final audio/video end-time difference must be within 100 ms. Final assembly has a 30-minute timeout and observes ComfyUI cancellation.

Only minimal continuation is kept in RAM during a run; persistent tensors and permanent image sequences are not saved. Saved segments are never deleted by finalization.

Save a copy of older workflows before updating. After restart and browser refresh, an old Entry token control migrates to `mode=new` and old token-output links are removed; other output positions stay unchanged. Select `resume` explicitly to continue existing work. Finalize's old directory input now displays as `segment_directory` and still selects the source; the added destination defaults to the same directory when empty.

Source and Registry version 1.1.0 include these updates; see the main README for publication and scan status. The frontend includes fixes for event handling, stale module loading and browser timers. The user reported a 15-segment run completing on ComfyUI v0.38.0 / frontend 1.53.6 with `--cache-classic`; saved media was checked as 1173 frames / 39.1 seconds with audio. This is not a full GPU resource or interruption-recovery validation. Use the tested configuration and limits in the [main README](../../README.md), and see [maintainer tests](https://github.com/Richard-Wang-fs/ComfyUI-RichardsVideoKits/blob/main/tests/README.md). The workflows contain no reference image, driving video or model weights; the official template source remains v0.36.0.
