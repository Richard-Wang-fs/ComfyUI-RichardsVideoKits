"""ComfyUI entrypoint for Richard's Video Kits."""

from pathlib import Path
import sys


# ComfyUI loads a custom-node directory by file location but does not add that
# directory to sys.path.  Keep the internal ``rvk`` package importable from a
# normal copied installation without requiring pip or a development launcher.
_PLUGIN_ROOT = str(Path(__file__).resolve().parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from rvk.comfy.extension import RVKExtension

WEB_DIRECTORY = "./web"


async def comfy_entrypoint() -> RVKExtension:
    return RVKExtension()


__all__ = ["WEB_DIRECTORY", "comfy_entrypoint"]
