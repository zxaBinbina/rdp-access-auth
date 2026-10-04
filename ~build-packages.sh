#!/bin/sh
# Build the current working tree; no commit or tag is required.
set -eu
RDP_BUILD_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$RDP_BUILD_ROOT/tools/build-package" "$@"
