# Publishing this repository

This directory is self-contained. Keep private development history, environment records, test media, credentials and build logs outside it.

## Source repository

The source repository is [Richard-Wang-fs/ComfyUI-RichardsVideoKits](https://github.com/Richard-Wang-fs/ComfyUI-RichardsVideoKits). RVK uses PolyForm Noncommercial 1.0.0; preserve `LICENSE`, `NOTICE.md` and the upstream template MIT notice when distributing it. This is source-available software, not an OSI-approved open source project.

Commit only this directory's reviewed contents and push `main`. Verify the remote commit and file list before announcing availability.

## Make the node discoverable in Manager

GitHub hosting and Comfy Registry publication are separate steps. Follow the official [publishing guide](https://docs.comfy.org/registry/publishing) and [metadata specification](https://docs.comfy.org/registry/specifications).

1. Create a publisher on [Comfy Registry](https://registry.comfy.org/) and put its exact identifier in `[tool.comfy].PublisherId`. The publisher identifier and node name become permanent identities; confirm them before the first publish.
2. Create a Registry publishing API key. Store it as the GitHub repository Actions secret `REGISTRY_ACCESS_TOKEN` in [repository Actions secrets](https://github.com/Richard-Wang-fs/ComfyUI-RichardsVideoKits/settings/secrets/actions); never commit it or paste it into issues or chat.
3. Commit the real PublisherId to `pyproject.toml` and push it to `main`. Open [Publish to Comfy Registry](https://github.com/Richard-Wang-fs/ComfyUI-RichardsVideoKits/actions/workflows/publish-registry.yml), select **Run workflow** on `main`, and enter the version exactly as declared in `pyproject.toml` (initially `1.0.0`). The included workflow validates the metadata and secret, then uses the official [publish-node-action](https://github.com/Comfy-Org/publish-node-action). It only runs manually; ordinary pushes do not publish. Publishing tools run on GitHub's runner, not in your ComfyUI environment.
4. Check the published version and Registry review status, then verify that Manager can find **Richard's Video Kits** / **richards-video-kits**, install it into a clean supported ComfyUI instance, and expose all five nodes and both frontend assets.
5. Update the README publication status only after those checks succeed. A successful GitHub push alone is not proof of Manager discovery or installation.

`.comfyignore` excludes maintainer-only files from the Registry archive; runtime and example files remain included. Registry packaging uses Git-tracked files. Published versions are immutable, so fixes require a new version.

Older Manager installations can use a separate metadata database. If legacy discovery is needed, follow the current registration instructions in the [Manager repository](https://github.com/Comfy-Org/ComfyUI-Manager); do not equate a pending listing request with availability.

## Current prerequisites

- GitHub owner/repository: `Richard-Wang-fs/ComfyUI-RichardsVideoKits`.
- Project license: PolyForm Noncommercial 1.0.0. Upstream workflow template MIT notice included under `licenses/`.
- Registry publisher and publishing secret: not configured.
- Registry publication and Manager installation verification: not performed.
