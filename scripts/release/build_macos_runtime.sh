#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: build_macos_runtime.sh VERSION SOURCE_DATE_EPOCH OUTPUT_DIR" >&2
  exit 2
fi

version=$1
source_date_epoch=$2
output_dir=$3
machine_arch=$(uname -m)

case "$machine_arch" in
  arm64)
    release_arch=arm64
    ;;
  x86_64)
    release_arch=x86_64
    ;;
  *)
    echo "unsupported macOS architecture: $machine_arch" >&2
    exit 1
    ;;
esac

tag_version=${version/+/-}
build_root=${RUNNER_TEMP:-"$PWD/.release-build"}/gloss-babeldoc-"$release_arch"
pyinstaller_dist=$build_root/dist
pyinstaller_work=$build_root/work
bundle_root=$build_root/gloss-babeldoc-runtime

mkdir -p "$output_dir"
mkdir -p "$build_root"

uv sync --frozen --no-default-groups --group release
uv run --no-sync pyinstaller \
  --clean \
  --noconfirm \
  --onedir \
  --console \
  --name gloss-babeldoc \
  --distpath "$pyinstaller_dist" \
  --workpath "$pyinstaller_work" \
  --specpath "$build_root" \
  --copy-metadata BabelDOC \
  --collect-all babeldoc \
  --collect-all bitstring \
  --collect-all pymupdf \
  --collect-submodules tiktoken_ext \
  --hidden-import babeldoc.tools.executor.babeldoc_adapter \
  --hidden-import babeldoc.tools.executor.layout_server \
  babeldoc/gloss_cli.py

mv "$pyinstaller_dist/gloss-babeldoc" "$bundle_root"
cp LICENSE NOTICE "$bundle_root/"
printf '%s\n' \
  "Gloss BabelDOC managed runtime" \
  "Version: $version" \
  "Exact source: https://github.com/SunChJ/BabelDOC/tree/v$tag_version" \
  "Release provenance: https://github.com/SunChJ/BabelDOC/releases/tag/v$tag_version" \
  "Run ./gloss-babeldoc runtime-info --json for the runtime and upstream identity." \
  "License and attribution are included in LICENSE and NOTICE." \
  > "$bundle_root/README.txt"
"$bundle_root/gloss-babeldoc" runtime-info --json \
  > "$bundle_root/RUNTIME_INFO.json"

archive_path=$output_dir/gloss-babeldoc-"$tag_version"-macos-"$release_arch".tar.gz
uv run --no-sync python \
  scripts/release/archive_runtime.py \
  --source "$bundle_root" \
  --output "$archive_path" \
  --source-date-epoch "$source_date_epoch"

echo "$archive_path"
