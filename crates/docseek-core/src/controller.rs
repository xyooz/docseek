use std::collections::BTreeMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use crate::{CancellationToken, CandidateScheduler, CoreError, ScanReport, Scanner, ScannerConfig};

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct JobId(u64);

impl JobId {
    pub fn get(self) -> u64 {
        self.0
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum JobKind {
    FullScan,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum JobState {
    Queued,
    Running,
    Succeeded,
    Cancelled,
    Failed,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct JobSnapshot {
    pub id: JobId,
    pub kind: JobKind,
    pub state: JobState,
    pub files_seen: usize,
    pub candidates: usize,
    pub errors: usize,
}

#[derive(Debug)]
struct ControllerState {
    next_id: u64,
    active: Option<(JobId, CancellationToken)>,
    jobs: BTreeMap<JobId, JobSnapshot>,
}

/// Owns one active core job and its cancellation/lifecycle state.
#[derive(Clone, Debug)]
pub struct JobController {
    state: Arc<Mutex<ControllerState>>,
}

#[derive(Debug)]
pub struct ScanOutcome {
    pub job_id: JobId,
    pub report: ScanReport,
    pub scheduler: CandidateScheduler,
}

impl Default for JobController {
    fn default() -> Self {
        Self::new()
    }
}

impl JobController {
    pub fn new() -> Self {
        Self {
            state: Arc::new(Mutex::new(ControllerState {
                next_id: 1,
                active: None,
                jobs: BTreeMap::new(),
            })),
        }
    }

    pub fn cancel(&self) -> bool {
        let state = self.state.lock().expect("job controller mutex poisoned");
        if let Some((_, token)) = &state.active {
            token.cancel();
            true
        } else {
            false
        }
    }

    pub fn is_cancelled(&self) -> bool {
        let state = self.state.lock().expect("job controller mutex poisoned");
        state
            .active
            .as_ref()
            .is_some_and(|(_, token)| token.is_cancelled())
    }

    pub fn snapshot(&self, job_id: JobId) -> Option<JobSnapshot> {
        let state = self.state.lock().expect("job controller mutex poisoned");
        state.jobs.get(&job_id).cloned()
    }

    pub fn last_snapshot(&self) -> Option<JobSnapshot> {
        let state = self.state.lock().expect("job controller mutex poisoned");
        state.jobs.values().next_back().cloned()
    }

    pub fn run_scan(
        &self,
        root: impl AsRef<Path>,
        config: ScannerConfig,
    ) -> Result<ScanOutcome, CoreError> {
        let (job_id, token) = self.begin(JobKind::FullScan)?;
        let result = Scanner::new(config).scan(root, &token);
        match result {
            Ok(report) => {
                let scheduler = CandidateScheduler::from_candidates(report.candidates.clone());
                self.finish(job_id, JobState::Succeeded, &report);
                Ok(ScanOutcome {
                    job_id,
                    report,
                    scheduler,
                })
            }
            Err(error) => {
                let state = if matches!(error, CoreError::Cancelled) {
                    JobState::Cancelled
                } else {
                    JobState::Failed
                };
                self.finish_empty(job_id, state);
                Err(error)
            }
        }
    }

    fn begin(&self, kind: JobKind) -> Result<(JobId, CancellationToken), CoreError> {
        let mut state = self.state.lock().expect("job controller mutex poisoned");
        if state.active.is_some() {
            return Err(CoreError::Busy);
        }
        let id = JobId(state.next_id);
        state.next_id = state.next_id.saturating_add(1);
        let token = CancellationToken::new();
        state.jobs.insert(
            id,
            JobSnapshot {
                id,
                kind,
                state: JobState::Queued,
                files_seen: 0,
                candidates: 0,
                errors: 0,
            },
        );
        state.active = Some((id, token.clone()));
        state
            .jobs
            .get_mut(&id)
            .expect("job was just inserted")
            .state = JobState::Running;
        Ok((id, token))
    }

    fn finish(&self, job_id: JobId, job_state: JobState, report: &ScanReport) {
        let mut state = self.state.lock().expect("job controller mutex poisoned");
        if let Some(snapshot) = state.jobs.get_mut(&job_id) {
            snapshot.state = job_state;
            snapshot.files_seen = report.files_seen;
            snapshot.candidates = report.candidates.len();
            snapshot.errors = report.errors;
        }
        state.active = None;
    }

    fn finish_empty(&self, job_id: JobId, job_state: JobState) {
        let mut state = self.state.lock().expect("job controller mutex poisoned");
        if let Some(snapshot) = state.jobs.get_mut(&job_id) {
            snapshot.state = job_state;
        }
        state.active = None;
    }
}
