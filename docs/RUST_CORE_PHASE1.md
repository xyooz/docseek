# Rust Core Phase 1

The rust/core-phase1 branch is the Rust rewrite foundation. The Python
indexer remains the production path and regression oracle during this phase.

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

To build the optional Python extension from the crate directory:

    maturin develop --interpreter python --manifest-path crates/docseek-python/Cargo.toml
    python -c "import docseek_rust; print(docseek_rust.JobController())"

The Python extension is intentionally opt-in through M4.5. The core exposes
`JobController.start_scan()` and a bounded `ScanSession.next_batch()` API;
each batch is capped at 128 candidates and carries a progress snapshot.
The terminal batch is empty and marked `finished`, so callers can process every
candidate batch before stopping.
Cancellation is idempotent and reports the `cancelling` lifecycle state before
the session finishes as cancelled. M5 will add the adapter that lets the
existing Python application select the Rust controller while keeping the
current implementation available as a fallback.

The repository pins the development profile in rust-toolchain.toml:
stable Rust plus rustfmt and clippy. GitHub Actions runs the same workspace
checks on macOS and Windows in .github/workflows/rust.yml.
