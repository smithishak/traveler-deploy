#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# != 1 || $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
    printf 'Usage on Linux amd64: %s PATH_TO_CANDIDATE\n' "$0" >&2
    exit 2
fi

candidate=$(realpath -ms -- "$1")
if [[ -e $candidate || -L $candidate || ! -d $(dirname -- "$candidate") ]]; then
    printf 'The output must be a new file in an existing directory.\n' >&2
    exit 2
fi

# Match the upstream v1.14.4 pure-Go Linux build and add its V2Ray API.
version=1.14.4
revision=c005f35300ce76427dc285481ec5695f5b179cd2
toolchain=go1.26.10.linux-amd64.tar.gz
toolchain_sha256=ccfe7ae822d2c2f41f4fe7c956199967a4dac22d0ee0d2b2bfdb440aab3b20c5
scratch=$(mktemp -d)
candidate_stage=
trap 'rm -rf -- "$scratch"; [[ -z $candidate_stage ]] || rm -f -- "$candidate_stage"' EXIT

curl -fLsS --retry 2 --output "$scratch/$toolchain" "https://go.dev/dl/$toolchain"
(cd "$scratch"; printf '%s  %s\n' "$toolchain_sha256" "$toolchain" | sha256sum -c -)
tar -xzf "$scratch/$toolchain" -C "$scratch"

repo="$scratch/source"
git init --quiet "$repo"
git -C "$repo" fetch --quiet --depth=1 https://github.com/SagerNet/sing-box.git "$revision"
git -C "$repo" checkout --quiet --detach FETCH_HEAD
[[ $(git -C "$repo" rev-parse HEAD) == "$revision" ]]

(
    cd "$repo"
    export CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOAMD64=v1
    export GOTOOLCHAIN=local GOENV=off GOWORK=off GOFLAGS=
    export GOMODCACHE="$scratch/modules" GOCACHE="$scratch/cache"
    tags="$(cat release/DEFAULT_BUILD_TAGS),with_purego,with_v2ray_api"
    flags="$(cat release/LDFLAGS) -X github.com/sagernet/sing-box/constant.Version=$version -s -w -buildid="
    "$scratch/go/bin/go" build -p 2 -mod=readonly -modcacherw -trimpath -buildvcs=true \
        -tags "$tags" -ldflags "$flags" -o "$scratch/sing-box" ./cmd/sing-box
)

version_info=$("$scratch/sing-box" version)
[[ $version_info == *"sing-box version $version"* && $version_info == *with_v2ray_api* ]]
candidate_stage=$(mktemp --tmpdir="$(dirname -- "$candidate")" .traveler-sing-box.XXXXXXXX)
install -m 0755 -- "$scratch/sing-box" "$candidate_stage"
ln -T -- "$candidate_stage" "$candidate"
printf '%s\n' "$version_info"
sha256sum -- "$candidate"
