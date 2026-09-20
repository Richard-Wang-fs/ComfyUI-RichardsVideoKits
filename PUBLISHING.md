# Publishing this repository

This directory is self-contained. Keep private development history, environment records, test media, credentials and build logs outside it.

## Before the first public upload

1. Confirm the GitHub owner and repository name, select the project license, add its complete `LICENSE` text and preserve applicable upstream workflow notices.
2. Set `[project.urls].Repository` to the actual GitHub URL in `pyproject.toml`, and add `license = { file = "LICENSE" }` under `[project]`.
3. Commit this directory's reviewed contents and push `main` to that repository. Verify the remote file list. Do not upload the development workspace.

## Make the node discoverable in Manager

GitHub hosting and Comfy Registry publication are separate steps. Follow the official [publishing guide](https://docs.comfy.org/registry/publishing) and [metadata specification](https://docs.comfy.org/registry/specifications).

1. Create a publisher on [Comfy Registry](https://registry.comfy.org/) and put its exact identifier in `[tool.comfy].PublisherId`. The publisher identifier and node name become permanent identities; confirm them before the first publish.
2. Create a Registry publishing API key. Store it as the GitHub repository Actions secret `REGISTRY_ACCESS_TOKEN`; never commit it or paste it into issues or chat.
3. Once the source, license and metadata are complete, publish with the official Comfy CLI (`comfy node publish`) from a separately prepared publishing environment, or configure the official GitHub [publish-node-action](https://github.com/Comfy-Org/publish-node-action). No publishing tools are installed by this package.
4. Check the published version and Registry review status, then verify that Manager can find **Richard's Video Kits** / **richards-video-kits**, install it into a clean supported ComfyUI instance, and expose all five nodes and both frontend assets.
5. Update the README publication status only after those checks succeed. A successful GitHub push alone is not proof of Manager discovery or installation.

`.comfyignore` excludes maintainer-only files from the Registry archive; runtime and example files remain included. Registry packaging uses Git-tracked files. Published versions are immutable, so fixes require a new version.

Older Manager installations can use a separate metadata database. If legacy discovery is needed, follow the current registration instructions in the [Manager repository](https://github.com/Comfy-Org/ComfyUI-Manager); do not equate a pending listing request with availability.

## Current prerequisites

- GitHub owner/repository: not supplied.
- Project license: pending. The upstream workflow template MIT notice is included under `licenses/`.
- Registry publisher and publishing secret: not configured.
- GitHub upload, Registry publish and Manager installation: not performed.
