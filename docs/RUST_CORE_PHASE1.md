# Rust Core Phase 1

The rust/core-phase1 branch is the Rust rewrite foundation. The existing
Python indexing lifecycle remains the regression oracle, while the default
scan backend now selects Rust when available and keeps Python as the fallback.

## Workspace layout

- crates/docseek-core: platform-neutral cancellation, streaming scanner,
  candidate priority scheduler, and single-active-job lifecycle controller.
- crates/docseek-python: thin PyO3 module named docseek_rust. It exposes
  the Phase 1 controller and pull-based scan session without changing Python
  startup or indexing behavior.

The core does not parse Office files or open SQLite yet. Those are deliberate
later boundaries: parser/extractor integration belongs after the lifecycle
contract is exercised against the existing Python behavior.

## Local checks

    cargo check --workspace
    cargo test --workspace
    cargo clippy --workspace --all-targets --all-features -- -D warnings
    cargo fmt --all -- --check
    python -m unittest tests.test_scan_backend -v
    python -m unittest tests.test_scan_backend_selection -v
    python -m unittest tests.test_indexer_scan_backend -v

To build the optional Python extension from the crate directory:

    maturin develop --interpreter python --manifest-path crates/docseek-python/Cargo.toml
    python -c "import docseek_rust; print(docseek_rust.JobController())"

The Python extension remains optional. `DirectoryIndexer` accepts an injected
`ScanBackend`, so either backend can be exercised against the existing indexing
lifecycle without changing parsers, chunk writing, or SQLite ownership. The
production selector is `DOCSEEK_SCAN_BACKEND=auto|rust|python` and defaults to
`auto`: Rust is selected when it can create a scan session, otherwise Python is
selected with a warning. `rust` is strict and reports startup unavailability;
`python` always selects the Python backend. Fallback is only possible while
creating the session, before Rust has emitted any candidate. Once a session
exists, traversal errors propagate and do not restart discovery through Python.
`DirectoryIndexer.scan_backend_name` exposes the active diagnostic name.
`JobController.start_scan()` accepts the scanner configuration fields and
exposes a bounded `ScanSession.next_batch()` API. Each batch is capped at 128
candidates, or 2,000 filesystem work items when a sparse tree needs a progress
pulse. A batch carries both a progress snapshot and drained `ScanIssue` values
so permission and filesystem failures retain the existing Python index issue
and `stats.skipped` semantics.
The terminal batch is empty and marked `finished`, so callers can process every
candidate batch before stopping.
Cancellation is idempotent and reports the `cancelling` lifecycle state before
the session finishes as cancelled. The adapter and `DirectoryIndexer`
integration live in `src/docseek/scan_backend.py` and
`src/docseek/indexer.py`; the existing indexing lifecycle remains shared by
both backends. The adapter deliberately does not forward `max_file_size` to
discovery: oversized candidates must still reach `DirectoryIndexer`, where the
existing lifecycle removes stale rows and records the user-visible
`file_too_large` issue.

The repository pins the development profile in rust-toolchain.toml:
stable Rust plus rustfmt and clippy. GitHub Actions runs the same workspace
checks on macOS and Windows in .github/workflows/rust.yml.
