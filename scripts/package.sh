#!/usr/bin/env bash
# Build the Lambda deployment zip.
#
# The package contains only first-party code: boto3 and botocore ship with the
# Lambda runtime, so there is nothing to vendor and the archive stays tiny.
# Timestamps are normalised so an unchanged tree produces a byte-identical zip
# and Terraform does not redeploy five functions on every apply.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="${ROOT}/build"
STAGE="${BUILD}/package"
ARCHIVE="${BUILD}/lambda.zip"

rm -rf "${STAGE}" "${ARCHIVE}"
mkdir -p "${STAGE}"

cp -R "${ROOT}/src" "${STAGE}/src"
find "${STAGE}" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "${STAGE}" -type f -name '*.pyc' -delete

# Fixed mtime => deterministic archive => stable source_code_hash.
find "${STAGE}" -exec touch -t 200001010000 {} +

(cd "${STAGE}" && zip -qrX "${ARCHIVE}" .)

printf 'built %s (%s)\n' "${ARCHIVE}" "$(du -h "${ARCHIVE}" | cut -f1)"
