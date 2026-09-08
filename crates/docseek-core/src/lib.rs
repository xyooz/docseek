//! Platform-neutral indexing orchestration primitives.
//!
//! This crate deliberately does not parse document formats or open the
//! SQLite database yet. Phase 1 establishes the lifecycle and ordering
//! contract that the existing Python indexer can adopt incrementally.

mod cancellation;
mod candidate;
mod controller;
mod error;
mod scanner;
mod scheduler;

pub use cancellation::CancellationToken;
pub use candidate::{priority_for_extension, Candidate, CandidatePriority};
pub use controller::{JobController, JobId, JobKind, JobSnapshot, JobState, ScanSession};
pub use error::CoreError;
pub use scanner::{
    ScanBatch, ScanIssue, ScanProgress, ScanReport, Scanner, ScannerConfig, MAX_SCAN_BATCH_SIZE,
    MAX_SCAN_WORK_ITEMS,
};
pub use scheduler::CandidateScheduler;
