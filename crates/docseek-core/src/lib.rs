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
pub use controller::{JobController, JobId, JobKind, JobSnapshot, JobState, ScanOutcome};
pub use error::CoreError;
pub use scanner::{ScanReport, Scanner, ScannerConfig};
pub use scheduler::CandidateScheduler;
