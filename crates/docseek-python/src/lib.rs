// PyO3 0.22's generated wrappers trigger this lint on PyResult return types
// with current Clippy. Keep the exception scoped to the binding crate; core
// remains checked with -D warnings without this allowance.
#![allow(clippy::useless_conversion)]

use std::collections::BTreeSet;
use std::path::PathBuf;

use docseek_core::{
    CoreError, JobController, JobSnapshot, JobState, ScanBatch, ScanSession as CoreScanSession,
    ScannerConfig,
};
use pyo3::exceptions::{PyInterruptedError, PyRuntimeError};
use pyo3::prelude::*;

fn to_python_error(error: CoreError) -> PyErr {
    match error {
        CoreError::Cancelled => PyInterruptedError::new_err("DocSeek core job cancelled"),
        other => PyRuntimeError::new_err(other.to_string()),
    }
}

fn path_to_string(path: PathBuf) -> String {
    path.to_string_lossy().into_owned()
}

fn optional_path_to_string(path: Option<PathBuf>) -> Option<String> {
    path.map(path_to_string)
}

fn scanner_config_from_options(
    enabled_extensions: Option<Vec<String>>,
    ignored_dir_names: Option<Vec<String>>,
    excluded_paths: Option<Vec<PathBuf>>,
    excluded_file_patterns: Option<Vec<String>>,
    max_file_size: Option<u64>,
) -> ScannerConfig {
    let mut config = ScannerConfig::default();
    if let Some(extensions) = enabled_extensions {
        config.enabled_extensions = extensions.into_iter().collect::<BTreeSet<_>>();
    }
    if let Some(names) = ignored_dir_names {
        config.ignored_dir_names = names.into_iter().collect::<BTreeSet<_>>();
    }
    if let Some(paths) = excluded_paths {
        config.excluded_paths = paths;
    }
    if let Some(patterns) = excluded_file_patterns {
        config.excluded_file_patterns = patterns;
    }
    if let Some(limit) = max_file_size {
        config.max_file_size = Some(limit);
    }
    config
}

#[pyclass(name = "CancellationToken")]
pub struct PyCancellationToken {
    inner: docseek_core::CancellationToken,
}

#[pymethods]
impl PyCancellationToken {
    #[new]
    fn new() -> Self {
        Self {
            inner: docseek_core::CancellationToken::new(),
        }
    }

    fn cancel(&self) {
        self.inner.cancel();
    }

    fn is_cancelled(&self) -> bool {
        self.inner.is_cancelled()
    }
}

#[pyclass(name = "JobSnapshot")]
pub struct PyJobSnapshot {
    #[pyo3(get)]
    pub job_id: u64,
    #[pyo3(get)]
    pub state: String,
    #[pyo3(get)]
    pub directories_seen: usize,
    #[pyo3(get)]
    pub files_seen: usize,
    #[pyo3(get)]
    pub candidates_discovered: usize,
    #[pyo3(get)]
    pub candidates_emitted: usize,
    #[pyo3(get)]
    pub errors: usize,
    #[pyo3(get)]
    pub current_path: Option<String>,
}

impl From<JobSnapshot> for PyJobSnapshot {
    fn from(snapshot: JobSnapshot) -> Self {
        Self {
            job_id: snapshot.id.get(),
            state: job_state_name(snapshot.state).to_owned(),
            directories_seen: snapshot.directories_seen,
            files_seen: snapshot.files_seen,
            candidates_discovered: snapshot.candidates_discovered,
            candidates_emitted: snapshot.candidates_emitted,
            errors: snapshot.errors,
            current_path: optional_path_to_string(snapshot.current_path),
        }
    }
}

fn job_state_name(state: JobState) -> &'static str {
    match state {
        JobState::Queued => "queued",
        JobState::Running => "running",
        JobState::Cancelling => "cancelling",
        JobState::Succeeded => "succeeded",
        JobState::Cancelled => "cancelled",
        JobState::Failed => "failed",
    }
}

#[pyclass(name = "ScanBatch")]
pub struct PyScanBatch {
    #[pyo3(get)]
    pub candidates: Vec<String>,
    #[pyo3(get)]
    pub finished: bool,
    #[pyo3(get)]
    pub directories_seen: usize,
    #[pyo3(get)]
    pub files_seen: usize,
    #[pyo3(get)]
    pub candidates_discovered: usize,
    #[pyo3(get)]
    pub candidates_emitted: usize,
    #[pyo3(get)]
    pub errors: usize,
    #[pyo3(get)]
    pub current_path: Option<String>,
}

impl From<ScanBatch> for PyScanBatch {
    fn from(batch: ScanBatch) -> Self {
        let progress = batch.progress;
        Self {
            candidates: batch
                .candidates
                .into_iter()
                .map(|candidate| path_to_string(candidate.path))
                .collect(),
            finished: batch.finished,
            directories_seen: progress.directories_seen,
            files_seen: progress.files_seen,
            candidates_discovered: progress.candidates_discovered,
            candidates_emitted: progress.candidates_emitted,
            errors: progress.errors,
            current_path: optional_path_to_string(progress.current_path),
        }
    }
}

#[pyclass(name = "ScanSession")]
pub struct PyScanSession {
    inner: CoreScanSession,
}

#[pymethods]
impl PyScanSession {
    fn next_batch(&mut self, py: Python<'_>, max_items: usize) -> PyResult<PyScanBatch> {
        py.allow_threads(|| self.inner.next_batch(max_items))
            .map(PyScanBatch::from)
            .map_err(to_python_error)
    }

    fn cancel(&self) -> bool {
        self.inner.cancel()
    }

    fn snapshot(&self) -> Option<PyJobSnapshot> {
        self.inner.snapshot().map(Into::into)
    }

    fn is_finished(&self) -> bool {
        self.inner.is_finished()
    }
}

#[pyclass(name = "JobController")]
pub struct PyJobController {
    inner: JobController,
}

#[pymethods]
impl PyJobController {
    #[new]
    fn new() -> Self {
        Self {
            inner: JobController::new(),
        }
    }

    #[pyo3(signature = (
        root,
        enabled_extensions = None,
        ignored_dir_names = None,
        excluded_paths = None,
        excluded_file_patterns = None,
        max_file_size = None,
    ))]
    fn start_scan(
        &self,
        root: PathBuf,
        enabled_extensions: Option<Vec<String>>,
        ignored_dir_names: Option<Vec<String>>,
        excluded_paths: Option<Vec<PathBuf>>,
        excluded_file_patterns: Option<Vec<String>>,
        max_file_size: Option<u64>,
    ) -> PyResult<PyScanSession> {
        let config = scanner_config_from_options(
            enabled_extensions,
            ignored_dir_names,
            excluded_paths,
            excluded_file_patterns,
            max_file_size,
        );
        self.inner
            .start_scan(root, config)
            .map(|inner| PyScanSession { inner })
            .map_err(to_python_error)
    }

    fn cancel(&self) -> bool {
        self.inner.cancel()
    }

    fn is_cancelled(&self) -> bool {
        self.inner.is_cancelled()
    }

    fn last_snapshot(&self) -> Option<PyJobSnapshot> {
        self.inner.last_snapshot().map(Into::into)
    }
}

#[pymodule]
fn docseek_rust(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<PyCancellationToken>()?;
    module.add_class::<PyJobController>()?;
    module.add_class::<PyJobSnapshot>()?;
    module.add_class::<PyScanBatch>()?;
    module.add_class::<PyScanSession>()?;
    Ok(())
}
