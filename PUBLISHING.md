# Publishing this repository

This directory is self-contained. Keep private development history, environment records, test media, credentials and build logs outside it.

## Source repository

The source repository is [Richard-Wang-fs/ComfyUI-RichardsVideoKits](https://github.com/Richard-Wang-fs/ComfyUI-RichardsVideoKits). RVK uses PolyForm Noncommercial 1.0.0; preserve `LICENSE`, `NOTICE.md` and the upstream template MIT notice when distributing it. This is source-available software, not an OSI-approved open source project.

Commit only this directory's reviewed contents and push `main`. Run the [maintainer checks](https://github.com/Richard-Wang-fs/ComfyUI-RichardsVideoKits/blob/main/tests/README.md), then verify the remote commit and file list before announcing availability. The included frontend suite has 75 Node/VM tests covering loop control, status integration, stale module loading, browser timer behavior, unrelated-workflow isolation and Proxy handling. These tests do not run a real browser or Wan inference.

## Make the node discoverable in Manager

GitHub hosting and Comfy Registry publication are separate steps. Follow the official [publishing guide](https://docs.comfy.org/registry/publishing) and [metadata specification](https://docs.comfy.org/registry/specifications).

1. This project uses Publisher ID `richard34512` and node name `richards-video-kits`. The publisher identifier and node name become permanent Registry identities. Keep `[tool.comfy].PublisherId` aligned with the publisher that owns the release key.
2. Create a Registry publishing API key. Store it as the GitHub repository Actions secret `REGISTRY_ACCESS_TOKEN` in [repository Actions secrets](https://github.com/Richard-Wang-fs/ComfyUI-RichardsVideoKits/settings/secrets/actions); never commit it or paste it into issues or chat.
3. Commit the release metadata to `main`, then push a version tag matching `pyproject.toml`, such as `v1.1.1`. This starts [Publish to Comfy Registry](https://github.com/Richard-Wang-fs/ComfyUI-RichardsVideoKits/actions/workflows/publish-registry.yml). The workflow requires the tagged commit to belong to `main` and checks the version, publisher, repository and secret before using the official [publish-node-action](https://github.com/Comfy-Org/publish-node-action). Ordinary branch pushes do not publish. You can also select **Run workflow** on `main` and enter the matching version manually. Publishing tools run on GitHub's runner, not in your ComfyUI environment. Do not move an existing release tag or republish an already published version.
4. Check the published version and Registry review status, then verify that Manager can find **Richard's Video Kits** / **richards-video-kits**. Check public search/version/install endpoints, compare the downloaded archive with the release tag, validate package links, and import its five nodes in isolation. A Pending scan is not scan approval, and endpoint checks are not a production Manager UI installation test.
5. Update the README publication status only after those checks succeed. A successful GitHub push alone is not proof of Manager discovery or installation.

`.comfyignore` excludes maintainer-only files from the Registry archive; runtime and example files remain included. Registry packaging uses Git-tracked files. Published versions are immutable, so fixes require a new version.

Older Manager installations can use a separate metadata database. If legacy discovery is needed, follow the current registration instructions in the [Manager repository](https://github.com/Comfy-Org/ComfyUI-Manager); do not equate a pending listing request with availability.

## Current prerequisites

- Patch version `1.1.1` fixes the global frontend queue hook in `1.1.0`: requests without Loop Entry pass through unchanged, RVK requests support JSON-compatible Proxy objects without whole-graph cloning, and unrelated graph loading leaves metadata untouched. Users on `1.1.0` should upgrade to `1.1.1`. Its publication checks below passed; scan approval remains pending.
- The compatibility boundary remains Windows/NTFS, Python `3.12`, ComfyUI `0.38.0`, frontend `1.53.6` and `--cache-classic`. The patch has 75 passing Node/VM regressions, including 18 new checks that fail against the previous source; real-browser, original-workflow and GPU validation of this patch remain unperformed. Existing VIDEO, resume, status and output-directory behavior is retained.
- Current functional evidence includes a user-reported completed 15-segment run, with saved media checked as 1173 frames / 39.1 seconds and audio. This does not establish a full GPU resource profile or comprehensive interruption-recovery validation. See the README for remaining limits.
- GitHub owner/repository: `Richard-Wang-fs/ComfyUI-RichardsVideoKits`.
- Project license: PolyForm Noncommercial 1.0.0. Upstream workflow template MIT notice included under `licenses/`.
- Registry publisher: `richard34512`; the maintainer has configured `REGISTRY_ACCESS_TOKEN` as a GitHub repository secret. Its validity is checked during publication.

## 1.1.1 publication evidence

- Published on 2026-10-01 (Australia/Sydney), [Actions run 36747054759](https://github.com/Richard-Wang-fs/ComfyUI-RichardsVideoKits/actions/runs/36747054759) succeeded. Tag `v1.1.1` points to commit `8c5cc0820a48b950414de26f505e6e9302fe5b1f`. Public search and the Active/Pending version query return the project and patch; both default and explicit-version installation endpoints return `1.1.1`. The version scan status was **Pending** at verification, not approved.
- The actual Registry archive is 86,561 bytes, SHA-256 `378c24a68c4a814188b5de9ef6aaf2e753fe5917aa0c2cb8f8795ef711f2cf7a`. All 27 files match the immutable tag byte for byte; all 17 runtime files, including both patched frontend modules, match the tested candidate. ZIP path checks and all 18 package-relative documentation links passed.
- Isolated CPU import of the downloaded package registered all five nodes. These checks do not establish a production Manager UI installation, real-browser execution of the user's workflow, or GPU behavior. Follow-up publication-status documentation on `main` does not change the immutable tag or archive.

## Historical 1.1.0 publication evidence

Version `1.1.0` contains the queue-isolation defect described above. Its publication checks did not detect that frontend behavior; preserve the following evidence as history, not as a recommendation to install it.

- Version `1.1.0` was published on 2026-10-01 (Australia/Sydney), [Actions run 36739174547](https://github.com/Richard-Wang-fs/ComfyUI-RichardsVideoKits/actions/runs/36739174547), tag `v1.1.0` / commit `fc1e490653101aa3d27bba8d3297b0e8f9cf6b7d`. The existing `v1.0.0` tag is also preserved. The node was active and the version scan status was Pending at verification; the older `1.0.0` version was Flagged. Check current Registry status rather than treating those observations as current approval.
- Public search and Active/Pending version queries returned `1.1.0`; both default and explicit-version install endpoints returned its CDN URL. The downloaded archive was 85,301 bytes, SHA-256 `cd52f4dfdd004b7a0c1d7805aeb5c50551563a69f9b1a435716d69eaee8a4dd1`; all 27 files matched the immutable tag, including all 17 runtime files and both examples. Isolated CPU import registered all five nodes. No production Manager UI installation or GPU run was performed.
- The immutable `1.1.0` archive's example README retains prepublication wording and a relative maintainer-tests link whose target is excluded by `.comfyignore`. Runtime/package content checks passed; that documentation link check failed. The online example README corrects the link and status. These follow-up documentation edits do not move the tag or replace the published package.
