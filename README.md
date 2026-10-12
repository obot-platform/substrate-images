# Substrate images for Obot

Build and publish the Substrate infrastructure images and the Claude self-hosted
runner used by Obot. This directory is self-contained; it does not need an Obot
checkout and does not build the Obot server image.

## Build everything

Install Go 1.27+, [ko](https://ko.build/), Make, curl, tar, and Docker with
Buildx. Use a Buildx builder capable of building `linux/amd64` and `linux/arm64`
(including emulation or native nodes for the runner's package installation).
Authenticate to your container registry first.

Run the Bash script from this directory (examples use Nushell):

```nu
docker login ghcr.io
./build.sh ghcr.io/YOUR_ORG/substrate ghcr.io/YOUR_ORG/obot-claude-runner:main
```

This **builds and pushes** all nine images for both architectures. The first
argument is a repository prefix; the second is the full runner image. All images
use the runner's tag (`main` above):

- `substrate/ateapi`
- `substrate/atecontroller`
- `substrate/atelet`
- `substrate/atenet`
- `substrate/podcertcontroller`
- `substrate/ateom-gvisor`
- `substrate/envoy-dataplane`
- `substrate/ate-setup`
- `obot-claude-runner`

To build without publishing:

```nu
./build.sh --no-push ghcr.io/YOUR_ORG/substrate ghcr.io/YOUR_ORG/obot-claude-runner:local
```

Docker builds remain in the Buildx cache; this does not load multi-platform images
into the local Docker daemon. Set `PLATFORMS` to build only one architecture:

```nu
with-env {PLATFORMS: "linux/amd64"} {
    ./build.sh --no-push ghcr.io/YOUR_ORG/substrate ghcr.io/YOUR_ORG/obot-claude-runner:local
}
```

## Bump Substrate

Edit **`substrate.ref`** to contain an upstream release tag or full commit SHA,
then open a PR. CI builds the same source for every Substrate image. Prefer a
full SHA for reproducible builds. The source ref is independent of the published
image tag; changing it does not require editing the build script or workflows.
To try a different ref without changing the file:

```nu
with-env {SUBSTRATE_REF: "v0.3.0"} {
    ./build.sh --no-push ghcr.io/YOUR_ORG/substrate ghcr.io/YOUR_ORG/obot-claude-runner:local
}
```

The ref must have the upstream build targets and Dockerfile used by `build.sh`;
older releases may not support them. Source downloads use a temporary directory
that is removed after the build. The gVisor runtime needs no demo or microVM
images. The runner's Claude Code version is pinned separately in
`claude-runner/Dockerfile`.

To rebuild just the runner:

```nu
docker buildx build --platform linux/amd64,linux/arm64 --tag ghcr.io/YOUR_ORG/obot-claude-runner:main --push ./claude-runner
```

## GitHub Actions

| Event | Result |
| --- | --- |
| Pull request | Check scripts/workflows, run tests, build all nine images for amd64 and arm64; no push or registry login |
| Push/merge to `main` | Same checks and builds, publish all images with tag `main` |
| Tag push | Same checks and builds, publish all images with the exact tag name |

Tag names must be valid container tags, such as `v1.2.3` (no `/` or spaces).
Publication uses `GITHUB_TOKEN` with `packages: write`. PRs have read-only
repository permissions. Third-party actions are pinned to commits; Dependabot
checks action and Docker base-image updates weekly.

Default destinations are `ghcr.io/<owner>/substrate/<component>` and
`ghcr.io/<owner>/obot-claude-runner`. Optional repository Actions variables
`SUBSTRATE_IMAGE_REPOSITORY` and `RUNNER_IMAGE_REPOSITORY` override those prefixes
(without tags). They must be GHCR destinations writable by this repository.
For existing packages, grant this repository Actions write access in each
package's settings. Set package visibility to public if clusters pull anonymously.

Require the PR image build check before merging and protect release tags against
replacement. Registry publishing is per image: if a build fails halfway through,
earlier images may already have been published. Only deploy after the whole
workflow succeeds. Signing/attestations and vulnerability scanning are useful
next steps; they are not configured here.

## Configure Obot

Add these arguments to your existing Obot Helm command:

```nu
--set substrate.image.repository=ghcr.io/YOUR_ORG/substrate
--set-string config.OBOT_SERVER_SUBSTRATE_RUNNER_IMAGE=ghcr.io/YOUR_ORG/obot-claude-runner@sha256:FULL_DIGEST
```

Use the multi-platform digest printed by the build, or retrieve it with:

```nu
docker buildx imagetools inspect ghcr.io/YOUR_ORG/obot-claude-runner:main
```

Substrate requires runner images pinned by digest; a tag alone is not accepted
in Obot's environment settings. Building and publishing with a tag is still fine.

The chart derives the default worker image as
`<substrate.image.repository>/ateom-gvisor:<chart appVersion>`. Environments can
override the worker and runner images in their advanced configuration. Changing
a default does not overwrite an environment's saved image configuration.

Obot's bundled chart currently uses its `appVersion` as the Substrate image tag.
Publish that tag here or update the chart's `appVersion` to your published tag;
publishing `main` alone does not update the chart. Keep `substrate.ref` aligned
with `substrate.obot.ai/upstream-commit` in Obot's
`chart/charts/substrate/Chart.yaml`. Upgrade chart manifests and image source together. The runner's Claude Code
version is pinned in `claude-runner/Dockerfile`; coordinate updates with the
Claude CLI in Obot's Dockerfile. The embedded orchestrator and its provisioning
hook remain part of the Obot image. The runner communicates with Obot's
`/api/claude-runners` endpoints, so deploy compatible versions of both.

## Checks without building images

Requires Python 3.11+:

```nu
bash -n build.sh
python3 -B -m unittest discover -s claude-runner
python3 -B -m unittest discover -s tests
```

The build-script tests stub the image tools and network download; they verify
publishing flags, source overrides, and tag validation without building images.

## License

The build scripts and Obot runner integration in this directory are provided
under the [Apache License 2.0](LICENSE). This repository does not contain
Substrate source; the build downloads it and copies upstream's `LICENSE` and
`_LICENSES` third-party notices into each Substrate image (`/var/run/ko` for
ko-built images, `/usr/share/doc/substrate` for `envoy-dataplane`). The Claude
binary is distributed by Anthropic under its own terms; this license does not
relicense it or other software installed in the images.

## Obot egress credentials

The pinned upstream revision includes HTTP credential injection, requires a
placeholder header, and fails closed when the provider is unavailable. The build
uses the upstream source unchanged apart from adding license files; no patch
is needed.

The runner now installs MCP URLs with a non-secret placeholder header. API keys
are supplied by Obot directly to the mTLS-authenticated egress gateway, never to
the runner. Rebuild Obot, these images, and upgrade the chart together. Set the
new runner digest on environments and start a fresh Claude execution.

To test the gateway without building images, download the pinned upstream source
and run `go test ./cmd/atenet/internal/router/egress` from that checkout.

When upgrading from the previous pin, rebuild Obot as well as Substrate, and
update the saved worker image in each Claude environment to the new tag. For an
external database, set `substrate.postgres.role` to the database role used by its
DSN. Back up the database and suspend active sessions before upgrading; this
revision changes the actor protocol and database startup configuration.

### Claude managed settings

Obot can provide an environment's managed settings as a JSON object through
`OBOT_CLAUDE_MANAGED_SETTINGS`. The supervisor removes the variable and writes
`/etc/claude-code/managed-settings.json` before starting the runner. Blank input
preserves the image default; `{}` explicitly replaces it with an empty object.
Settings apply to new executions, including resumed sessions.

The file has mode 0444, but sessions currently run as root, so this is configuration
delivery rather than tamper-proof enforcement. Anthropic server-managed policy can
take precedence for ordinary sessions.
