use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use crate::scanner::{DiscoverySession, ScanBatch, ScanProgress, Scanner, ScannerConfig};
use crate::{CancellationToken, CandidateScheduler, CoreError};

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
    Cancelling,
    Succeeded,
    Cancelled,
    Failed,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct JobSnapshot {
    pub id: JobId,
    pub kind: JobKind,
    pub state: JobState,
    pub directories_seen: usize,
    pub files_seen: usize,
    pub candidates_discovered: usize,
    pub candidates_emitted: usize,
    pub errors: usize,
    pub current_path: Option<PathBuf>,
}

impl JobSnapshot {
    fn apply_progress(&mut self, progress: &ScanProgress) {
        self.directories_seen = progress.directories_seen;
        self.files_seen = progress.files_seen;
        self.candidates_discovered = progress.candidates_discovered;
        self.candidates_emitted = progress.candidates_emitted;
        self.errors = progress.errors;
        self.current_path = progress.current_path.clone();
    }
}

#[derive(Debug)]
struct ActiveJob {
    id: JobId,
    token: CancellationToken,
    progress: Arc<Mutex<ScanProgress>>,
}

#[derive(Debug)]
struct ControllerState {
    next_id: u64,
    active: Option<ActiveJob>,
    jobs: BTreeMap<JobId, JobSnapshot>,
}

/// Owns one active core job and its cancellation/lifecycle state.
#[derive(Clone, Debug)]
pub struct JobController {
    state: Arc<Mutex<ControllerState>>,
}

/// Pull-based, bounded scan interface owned by one controller job.
pub struct ScanSession {
    inner: DiscoverySession,
    controller: JobController,
    job_id: JobId,
    completed: bool,
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

    pub fn start_scan(
        &self,
        root: impl AsRef<Path>,
        config: ScannerConfig,
    ) -> Result<ScanSession, CoreError> {
        let (job_id, token, progress) = self.begin(JobKind::FullScan)?;
        match Scanner::new(config).start_session_with_progress(root, token, progress) {
            Ok(inner) => {
                self.update_progress(job_id, &inner.snapshot());
                Ok(ScanSession {
                    inner,
                    controller: self.clone(),
                    job_id,
                    completed: false,
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

    pub fn cancel(&self) -> bool {
        let mut state = self.state.lock().expect("job controller mutex poisoned");
        let Some(active) = state.active.as_ref() else {
            return false;
        };
        let job_id = active.id;
        active.token.cancel();
        if let Some(snapshot) = state.jobs.get_mut(&job_id) {
            if matches!(snapshot.state, JobState::Queued | JobState::Running) {
                snapshot.state = JobState::Cancelling;
            }
        }
        true
    }

    pub fn is_cancelled(&self) -> bool {
        let state = self.state.lock().expect("job controller mutex poisoned");
        state
            .active
            .as_ref()
            .is_some_and(|active| active.token.is_cancelled())
    }

    pub fn snapshot(&self, job_id: JobId) -> Option<JobSnapshot> {
        let state = self.state.lock().expect("job controller mutex poisoned");
        Self::snapshot_from_state(&state, job_id)
    }

    pub fn last_snapshot(&self) -> Option<JobSnapshot> {
        let state = self.state.lock().expect("job controller mutex poisoned");
        let job_id = state.jobs.keys().next_back().copied()?;
        Self::snapshot_from_state(&state, job_id)
    }

    fn begin(
        &self,
        kind: JobKind,
    ) -> Result<(JobId, CancellationToken, Arc<Mutex<ScanProgress>>), CoreError> {
        let mut state = self.state.lock().expect("job controller mutex poisoned");
        if state.active.is_some() {
            return Err(CoreError::Busy);
        }
        let id = JobId(state.next_id);
        state.next_id = state.next_id.saturating_add(1);
        let token = CancellationToken::new();
        let progress = Arc::new(Mutex::new(ScanProgress::default()));
        state.jobs.insert(
            id,
            JobSnapshot {
                id,
                kind,
                state: JobState::Queued,
                directories_seen: 0,
                files_seen: 0,
                candidates_discovered: 0,
                candidates_emitted: 0,
                errors: 0,
                current_path: None,
            },
        );
        state.active = Some(ActiveJob {
            id,
            token: token.clone(),
            progress: progress.clone(),
        });
        state
            .jobs
            .get_mut(&id)
            .expect("job was just inserted")
            .state = JobState::Running;
        Ok((id, token, progress))
    }

    fn snapshot_from_state(state: &ControllerState, job_id: JobId) -> Option<JobSnapshot> {
        let mut snapshot = state.jobs.get(&job_id)?.clone();
        if let Some(active) = state.active.as_ref().filter(|active| active.id == job_id) {
            let progress = active
                .progress
                .lock()
                .expect("scan progress mutex poisoned")
                .clone();
            snapshot.apply_progress(&progress);
        }
        Some(snapshot)
    }

    fn is_cancelling(&self, job_id: JobId) -> bool {
        let state = self.state.lock().expect("job controller mutex poisoned");
        state
            .jobs
            .get(&job_id)
            .is_some_and(|snapshot| snapshot.state == JobState::Cancelling)
    }

    fn update_progress(&self, job_id: JobId, progress: &ScanProgress) {
        let mut state = self.state.lock().expect("job controller mutex poisoned");
        if let Some(snapshot) = state.jobs.get_mut(&job_id) {
            snapshot.apply_progress(progress);
        }
    }

    fn finish(&self, job_id: JobId, job_state: JobState, progress: &ScanProgress) {
        let mut state = self.state.lock().expect("job controller mutex poisoned");
        if let Some(snapshot) = state.jobs.get_mut(&job_id) {
            snapshot.state = job_state;
            snapshot.apply_progress(progress);
        }
        if state
            .active
            .as_ref()
            .is_some_and(|active| active.id == job_id)
        {
            state.active = None;
        }
    }

    fn finish_empty(&self, job_id: JobId, job_state: JobState) {
        let mut state = self.state.lock().expect("job controller mutex poisoned");
        if let Some(snapshot) = state.jobs.get_mut(&job_id) {
            snapshot.state = job_state;
        }
        if state
            .active
            .as_ref()
            .is_some_and(|active| active.id == job_id)
        {
            state.active = None;
        }
    }
}

impl ScanSession {
    pub fn next_batch(&mut self, requested_size: usize) -> Result<ScanBatch, CoreError> {
        if self.completed {
            return Ok(ScanBatch {
                candidates: Vec::new(),
                finished: true,
                progress: self.inner.snapshot(),
            });
        }

        match self.inner.next_candidates(requested_size) {
            Ok(discovery_batch) => {
                let mut scheduler = CandidateScheduler::new();
                scheduler.extend(discovery_batch.candidates);
                let candidates = scheduler.into_sorted_vec();
                let progress = discovery_batch.progress;
                self.controller.update_progress(self.job_id, &progress);

                if discovery_batch.finished {
                    let state = if self.controller.is_cancelling(self.job_id) {
                        JobState::Cancelled
                    } else {
                        JobState::Succeeded
                    };
                    self.controller.finish(self.job_id, state, &progress);
                    self.completed = true;
                }

                Ok(ScanBatch {
                    candidates,
                    finished: discovery_batch.finished,
                    progress,
                })
            }
            Err(error) => {
                if matches!(error, CoreError::InvalidBatchSize) {
                    return Err(error);
                }
                let progress = self.inner.snapshot();
                let state = if matches!(error, CoreError::Cancelled) {
                    JobState::Cancelled
                } else {
                    JobState::Failed
                };
                self.controller.finish(self.job_id, state, &progress);
                self.completed = true;
                Err(error)
            }
        }
    }

    pub fn cancel(&self) -> bool {
        self.controller.cancel()
    }

    pub fn snapshot(&self) -> Option<JobSnapshot> {
        self.controller.snapshot(self.job_id)
    }

    pub fn is_finished(&self) -> bool {
        self.completed
    }
}

impl Drop for ScanSession {
    fn drop(&mut self) {
        if !self.completed {
            let progress = self.inner.snapshot();
            self.controller
                .finish(self.job_id, JobState::Cancelled, &progress);
            self.completed = true;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn active_snapshot_reads_shared_scan_progress() {
        let controller = JobController::new();
        let (job_id, _token, progress) = controller.begin(JobKind::FullScan).expect("job starts");
        {
            let mut progress = progress.lock().expect("progress mutex is healthy");
            progress.directories_seen = 4;
            progress.files_seen = 37;
            progress.candidates_discovered = 5;
            progress.candidates_emitted = 2;
            progress.errors = 1;
            progress.current_path = Some(PathBuf::from("/tmp/live-progress.txt"));
        }

        let snapshot = controller.snapshot(job_id).expect("snapshot exists");
        assert_eq!(snapshot.state, JobState::Running);
        assert_eq!(snapshot.directories_seen, 4);
        assert_eq!(snapshot.files_seen, 37);
        assert_eq!(snapshot.candidates_discovered, 5);
        assert_eq!(snapshot.candidates_emitted, 2);
        assert_eq!(snapshot.errors, 1);
        assert_eq!(
            snapshot.current_path,
            Some(PathBuf::from("/tmp/live-progress.txt"))
        );
        assert_eq!(controller.last_snapshot(), Some(snapshot));
    }
}
