# Publishing this repository

This directory is self-contained. Keep private development history, environment records, test media, credentials and build logs outside it.

## Source repository

The source repository is [Richard-Wang-fs/ComfyUI-RichardsVideoKits](https://github.com/Richard-Wang-fs/ComfyUI-RichardsVideoKits). RVK uses PolyForm Noncommercial 1.0.0; preserve `LICENSE`, `NOTICE.md` and the upstream template MIT notice when distributing it. This is source-available software, not an OSI-approved open source project.

Commit only this directory's reviewed contents and push `main`. Run the [maintainer checks](tests/README.md), then verify the remote commit and file list before announcing availability. The included frontend suite has 57 tests covering loop control, status integration, stale module loading and browser timer behavior; simulated queue tests do not run Wan inference.

## Make the node discoverable in Manager

GitHub hosting and Comfy Registry publication are separate steps. Follow the official [publishing guide](https://docs.comfy.org/registry/publishing) and [metadata specification](https://docs.comfy.org/registry/specifications).

1. This project uses Publisher ID `richard34512` and node name `richards-video-kits`. The publisher identifier and node name become permanent Registry identities. Keep `[tool.comfy].PublisherId` aligned with the publisher that owns the release key.
2. Create a Registry publishing API key. Store it as the GitHub repository Actions secret `REGISTRY_ACCESS_TOKEN` in [repository Actions secrets](https://github.com/Richard-Wang-fs/ComfyUI-RichardsVideoKits/settings/secrets/actions); never commit it or paste it into issues or chat.
3. Commit the release metadata to `main`, then push a version tag matching `pyproject.toml`, such as `v1.1.0`. This starts [Publish to Comfy Registry](https://github.com/Richard-Wang-fs/ComfyUI-RichardsVideoKits/actions/workflows/publish-registry.yml). The workflow requires the tagged commit to belong to `main` and checks the version, publisher, repository and secret before using the official [publish-node-action](https://github.com/Comfy-Org/publish-node-action). Ordinary branch pushes do not publish. You can also select **Run workflow** on `main` and enter the matching version manually. Publishing tools run on GitHub's runner, not in your ComfyUI environment. Do not move an existing release tag or republish an already published version.
4. Check the published version and Registry review status, then verify that Manager can find **Richard's Video Kits** / **richards-video-kits**. For version 1.1.0, public search/version/install endpoints, downloaded archive bytes and isolated five-node import were verified on 2026-10-01 (Australia/Sydney). The scan status was Pending; this is not scan approval or a production Manager UI installation test.
5. Update the README publication status only after those checks succeed. A successful GitHub push alone is not proof of Manager discovery or installation.

`.comfyignore` excludes maintainer-only files from the Registry archive; runtime and example files remain included. Registry packaging uses Git-tracked files. Published versions are immutable, so fixes require a new version.

Older Manager installations can use a separate metadata database. If legacy discovery is needed, follow the current registration instructions in the [Manager repository](https://github.com/Comfy-Org/ComfyUI-Manager); do not equate a pending listing request with availability.

## Current prerequisites

- GitHub source version remains `1.1.0`. Current source includes Finalize's standard VIDEO output, explicit new/resume modes, Entry status controls, separate final-output directories and frontend continuation fixes. Version `1.1.0` is published at tag `v1.1.0` / commit `fc1e490653101aa3d27bba8d3297b0e8f9cf6b7d`; the existing `v1.0.0` tag is preserved. The metadata pins ComfyUI `0.38.0`; tested frontend is `1.53.6` with `--cache-classic`.
- Current functional evidence includes a user-reported completed 15-segment run, with saved media checked as 1173 frames / 39.1 seconds and audio. This does not establish a full GPU resource profile or comprehensive interruption-recovery validation. See the README for remaining limits.
- GitHub owner/repository: `Richard-Wang-fs/ComfyUI-RichardsVideoKits`.
- Project license: PolyForm Noncommercial 1.0.0. Upstream workflow template MIT notice included under `licenses/`.
- Registry publisher: `richard34512`; the maintainer has configured `REGISTRY_ACCESS_TOKEN` as a GitHub repository secret. Its validity is checked during publication.
- Registry publication: version `1.1.0` published on 2026-10-01 (Australia/Sydney), [Actions run 36739174547](https://github.com/Richard-Wang-fs/ComfyUI-RichardsVideoKits/actions/runs/36739174547). Node active; version scan status Pending at verification. The older `1.0.0` version is Flagged. Check current status before recommending installation; do not bypass Manager security checks.
- Discovery/package verification: public search and Active/Pending version queries return `1.1.0`; both default and explicit-version install endpoints return its CDN URL. The downloaded archive is 85,301 bytes, SHA-256 `cd52f4dfdd004b7a0c1d7805aeb5c50551563a69f9b1a435716d69eaee8a4dd1`; all 27 files match the immutable tag, including all 17 runtime files and both examples. Isolated CPU import registered all five nodes. No production Manager UI installation or GPU run was performed.
- Documentation limit: the immutable `1.1.0` archive's example README retains prepublication wording and a relative maintainer-tests link whose target is excluded by `.comfyignore`. Runtime/package content checks passed; that documentation link check failed. The online example README corrects the link and status. These follow-up documentation edits do not move the tag or replace the published package.
