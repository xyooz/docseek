# Rust Core Phase 1

The rust/core-phase1 branch is the Rust rewrite foundation. The Python
indexer remains the production path and regression oracle during this phase.

## Workspace layout

- crates/docseek-core: platform-neutral cancellation, scanner, candidate
  priority scheduler, and single-active-job lifecycle controller.
- crates/docseek-python: thin PyO3 module named docseek_rust. It exposes
  the Phase 1 controller without changing Python startup or indexing behavior.

The core does not parse Office files or open SQLite yet. Those are deliberate
later boundaries: parser/extractor integration belongs after the lifecycle
contract is exercised against the existing Python behavior.

## Local checks

    cargo test -p docseek-core
    cargo fmt --all -- --check

To build the optional Python extension from the crate directory:

    maturin develop --interpreter python --manifest-path crates/docseek-python/Cargo.toml
    python -c "import docseek_rust; print(docseek_rust.JobController())"

The Python extension is intentionally opt-in in M1. M5 will add the adapter
that lets the existing Python application select the Rust controller while
keeping the current implementation available as a fallback.

The repository pins the development profile in rust-toolchain.toml:
stable Rust plus rustfmt and clippy. GitHub Actions runs the same workspace
checks on macOS and Windows in .github/workflows/rust.yml.
