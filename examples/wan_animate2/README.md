# Wan Animate 2 workflows

Import `wan_animate2_rvk_loop.json` for generation, or `finalize_existing_segments.json` to assemble an already complete set of segments. The second workflow has no model nodes.

The generation workflow retains one editable official **Motion Transfer (Wan Animate 2)** subgraph. Select your own reference image and full driving video, then choose the required models inside the subgraph. Model names in the workflow are references, not bundled files or automatic downloads.

Set segment length and a fresh relative output directory on **RVK Wan Animate 2 Loop Entry**. Keep its output connections to the subgraph, Save and Finalize. An output directory containing any existing file is rejected before model execution.

The example uses the official `WanAnimate2Cache` with `device=cpu`, as tested on a 16 GiB GPU. This reduces GPU memory pressure but can be slower. The setting remains editable inside the subgraph.

Queue once. Keep the original workflow open and unchanged while RVK queues successive segments. Switching or closing the workflow stops continuation. The Entry button **Stop RVK after current segment** prevents the next segment; the current segment may finish. Completed MP4 files remain on disk.

Each segment is silent. Finalize waits until all frames are complete, then adds the first audio track from the original input. If your input has no audio, segment generation can complete but Finalize reports `source_audio_missing`.

For standalone finalization, select the same untrimmed input video used for generation and point Finalize to the existing segment directory. Segments must start at index zero with no gaps, have compatible video properties, and cover exactly the input's frame count. Existing final files and stale partial files cause an error. No model state is resumed.

The source audio must begin at video time zero, allowing at most two audio samples of timestamp rounding. Larger positive or negative offsets are rejected; RVK does not insert silence, trim or shift audio. The final audio/video end-time difference must be within 100 ms. Final assembly has a 30-minute timeout and observes ComfyUI cancellation.

Only minimal continuation is kept in RAM. A crash or restart can lose it; use a new empty directory when starting generation again. Saved segments are never deleted by finalization.

Use the tested configuration and limits in the [main README](../../README.md). The workflows contain no reference image, driving video or model weights.
