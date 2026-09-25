#!/usr/bin/env bash
# One reproducible gate for contributors and CI; never installs on the host.
set -euo pipefail
cd "$(dirname "$0")/.."
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen ty check
uv run --frozen python -m pytest
uv run --frozen python scripts/check-package.py
