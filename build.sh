#!/usr/bin/env bash
set -euo pipefail

push=true
if [[ ${1:-} == --no-push ]]; then
    push=false
    shift
fi
if [[ $# -ne 2 || -z $1 || -z $2 ]]; then
    echo "Usage: $0 [--no-push] SUBSTRATE_REPOSITORY RUNNER_IMAGE:TAG" >&2
    exit 1
fi

repository=$1
runner_image=$2
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
revision=${SUBSTRATE_REF:-$(cat "$script_dir/substrate.ref")}
tag=${runner_image##*:}
if [[ ! $revision =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]]; then
    echo "Substrate ref must be a release tag or commit SHA." >&2
    exit 1
fi
if [[ $runner_image != *:* || $runner_image == *@* || ! $tag =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$ ]]; then
    echo "Runner image must include a valid image tag (not a digest)." >&2
    exit 1
fi

work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
curl --fail --location --retry 3 \
    "https://api.github.com/repos/agent-substrate/substrate/tarball/$revision" \
    --output "$work/source.tar.gz"
tar -xzf "$work/source.tar.gz" -C "$work" --strip-components=1

cd "$work"
export KO_DOCKER_REPO="$repository"
export KO_DEFAULTPLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
ko_images=(ateapi atecontroller atelet atenet podcertcontroller ateom-gvisor)

# Ship upstream's license and third-party notices; ko serves kodata at /var/run/ko.
licenses=(LICENSE)
if [[ -d _LICENSES ]]; then
    licenses+=(_LICENSES)
fi
for image in "${ko_images[@]}" ate-setup; do
    mkdir -p "cmd/$image/kodata"
    cp -R "${licenses[@]}" "cmd/$image/kodata/"
done

# Keep upstream image definitions; only publish when requested.
make build-images "KO=ko" "KO_DOCKER_REPO=$repository" "VERSION=$revision" \
    "KO_TAGS=--tags=$tag" "KO_FLAGS=--push=$push" \
    "IMAGES=${ko_images[*]/#/./cmd/}"

# ate-setup's create commands need a repository marker, not source/build tools.
cp go.mod cmd/ate-setup/kodata/go.mod
ko build --base-import-paths "--tags=$tag" "--push=$push" \
    "--ldflags=-X=github.com/agent-substrate/substrate/internal/version.Version=$revision" ./cmd/ate-setup

output=(--output=type=cacheonly)
if [[ $push == true ]]; then
    output=(--push)
fi
# Upstream's make target always pushes; invoke its Dockerfile directly for PRs.
# Its build context excludes the repository root, so pass the license separately.
mkdir "$work/.substrate-license"
cp LICENSE "$work/.substrate-license/"
printf '\nCOPY --from=substrate-license LICENSE /usr/share/doc/substrate/LICENSE\n' \
    >> cmd/dataplane/envoy/Dockerfile
docker buildx build --platform "$KO_DEFAULTPLATFORMS" "${output[@]}" \
    --build-context "substrate-license=$work/.substrate-license" \
    --tag "$repository/envoy-dataplane:$tag" cmd/dataplane/envoy
docker buildx build --platform "$KO_DEFAULTPLATFORMS" "${output[@]}" \
    --tag "$runner_image" "$script_dir/claude-runner"
