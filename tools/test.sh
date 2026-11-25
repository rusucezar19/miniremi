#!/bin/sh
# Run the test suite in a throwaway Python container (no local Python needed).
# Usage: sh tools/test.sh [pytest args]
set -e
cd "$(dirname "$0")/.."
docker run --rm -v "$(pwd):/src" -v certtracker-pipcache:/root/.cache/pip -w /src -e PYTHONDONTWRITEBYTECODE=1 \
  python:3.12-slim sh -c "pip install -q --root-user-action=ignore --disable-pip-version-check -r requirements-dev.txt && python -m pytest -p no:cacheprovider $*"
