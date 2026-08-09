# Contributing to nstream

## Setup

```sh
./install.sh                          # uv tool install + optional config bootstrap
uv run python -m pytest               # tests (always python -m — see below)
uvx ruff check . && uvx ruff format --check .
uvx ty check
uv run nstream "the matrix"           # run from source
```

Tooling is **uv**-based. Runtime stays **stdlib-only**; `ruff` / `ty` / `pytest` are
dev-group tools.

### Why `python -m pytest`

If a system-wide `nstream` is installed, bare `pytest` can resolve to the system interpreter
and import the **installed** package instead of `src/`. `uv run python -m pytest` uses the
project venv with the editable checkout.

## Project rules

Read [CLAUDE.md](CLAUDE.md) for agent-oriented hard constraints. Summary for humans:

- **Import graph:** `cli` at the bottom — nothing imports `cli`. Leaves: `util`, `ui`,
  `languages`, `types`, `sources`, `net`, `log` (and related pure helpers). See
  [docs/architecture.md](docs/architecture.md).
- **Debrid provider-agnostic:** no RealDebrid special-cases; tokens/markers/redaction are
  generic.
- **Never log stream URLs or debrid tokens** — use `what=` labels; `log.RedactFormatter`.
- **Disk I/O best-effort** — never block playback on state writes.
- **Dead sources (ADR 0025):** only `gone` probes are persisted.
- **Cast audio language** decided in `cast_vet.vet_cast_audio`, not on the DMR.
- Comments and technical docs: **English**. TUI strings may stay Italian.
- Concise code; no over-engineering.

## Adding or moving a module

1. Place it where the **policy** lives (not the first caller).
2. Preserve import discipline.
3. Add `tests/test_<mod>.py` (or package tests under `tests/`).
4. Update **[docs/architecture.md](docs/architecture.md)** only (not README module tables).
5. Architectural trade-off → new ADR (below).

## Tests

| Kind | Location | Notes |
| ---- | -------- | ----- |
| Unit | `tests/test_*.py` | Prefer unit before integration |
| Fixtures | `tests/data/`, `tests/conftest.py` | Offline stream/meta samples |
| Config/doc drift | `tests/test_config_docs.py` | `Config` ↔ `config.example.json` |

Mock network at `net` / `api` boundaries; do not hit live debrid in unit tests. Cast and
TorrServer paths should use fakes or skip when binaries are absent.

Ruff: line-length 100, rules `E,F,I,UP,B,SIM`. Pytest: `testpaths=["tests"]`, quiet by default.

## Architecture Decision Records

Format: MADR-light — see [docs/adr/0000-template.md](docs/adr/0000-template.md).

1. Copy the template → next number `NNNN-short-title.md`.
2. Status starts as **Proposed**; mark **Accepted** when landed in code.
3. **Append-only:** do not rewrite an Accepted ADR’s decision. Supersede with a new ADR and
   set the old one to `Superseded by NNNN`.
4. Add a row to [docs/adr/README.md](docs/adr/README.md).
5. Cite **symbols** (`stream_select.prepare_stream`), not fragile line numbers.
6. No personal vault wikilinks (`[[…]]`) — they do not resolve in the public repo.
7. Empirical data may live in `docs/adr/NNNN-phase*/` (see ADR 0020).

When to write an ADR: a constraint future code must respect, a rejected alternative that
would otherwise be re-proposed, or a cross-module policy (cast finish predicate, privacy
gate, denylist rules, …).

## Packaging

Tracked under `packaging/`: `PKGBUILD`, `build-local.sh`, `nstream.install`, `.SRCINFO` only.
Do not commit build trees, wheels, or `*.pkg.tar.zst`. Keep `pkgver` in sync with
`src/nstream/__init__.py`. See [docs/roadmap.md](docs/roadmap.md) release process.

## Maintainer scripts

| Script | Role |
| ------ | ---- |
| `scripts/refresh-stream-addons.py` | Rebuild RD-backed stream addon URLs in the local config from `torrentio_base` (never prints the token). Not part of the runtime package. |

## Documentation map

[docs/README.md](docs/README.md) — audience routing and single sources of truth. Do not
duplicate module maps or full ranking tables in the root README.
