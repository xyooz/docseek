// PyO3 0.22's generated wrappers trigger this lint on PyResult return types
// with current Clippy. Keep the exception scoped to the binding crate; core
// remains checked with -D warnings without this allowance.
#![allow(clippy::useless_conversion)]

use std::path::PathBuf;

use docseek_core::{CoreError, JobController, JobSnapshot, JobState, ScannerConfig};
use pyo3::exceptions::{PyInterruptedError, PyRuntimeError};
use pyo3::prelude::*;

fn to_python_error(error: CoreError) -> PyErr {
    match error {
        CoreError::Cancelled => PyInterruptedError::new_err("DocSeek core job cancelled"),
        other => PyRuntimeError::new_err(other.to_string()),
    }
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

#[pyclass(name = "ScanResult")]
pub struct PyScanResult {
    #[pyo3(get)]
    pub job_id: u64,
    #[pyo3(get)]
    pub files_seen: usize,
    #[pyo3(get)]
    pub directories_seen: usize,
    #[pyo3(get)]
    pub errors: usize,
    #[pyo3(get)]
    pub candidates: Vec<String>,
}

#[pyclass(name = "JobSnapshot")]
pub struct PyJobSnapshot {
    #[pyo3(get)]
    pub job_id: u64,
    #[pyo3(get)]
    pub state: String,
    #[pyo3(get)]
    pub files_seen: usize,
    #[pyo3(get)]
    pub candidates: usize,
    #[pyo3(get)]
    pub errors: usize,
}

impl From<JobSnapshot> for PyJobSnapshot {
    fn from(snapshot: JobSnapshot) -> Self {
        Self {
            job_id: snapshot.id.get(),
            state: job_state_name(snapshot.state).to_owned(),
            files_seen: snapshot.files_seen,
            candidates: snapshot.candidates,
            errors: snapshot.errors,
        }
    }
}

fn job_state_name(state: JobState) -> &'static str {
    match state {
        JobState::Queued => "queued",
        JobState::Running => "running",
        JobState::Succeeded => "succeeded",
        JobState::Cancelled => "cancelled",
        JobState::Failed => "failed",
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

    fn cancel(&self) -> bool {
        self.inner.cancel()
    }

    fn is_cancelled(&self) -> bool {
        self.inner.is_cancelled()
    }

    fn scan(&self, py: Python<'_>, root: PathBuf) -> PyResult<PyScanResult> {
        let outcome = py
            .allow_threads(|| self.inner.run_scan(root, ScannerConfig::default()))
            .map_err(to_python_error)?;
        let candidates = outcome
            .scheduler
            .into_sorted_vec()
            .into_iter()
            .map(|candidate| candidate.path.to_string_lossy().into_owned())
            .collect();
        Ok(PyScanResult {
            job_id: outcome.job_id.get(),
            files_seen: outcome.report.files_seen,
            directories_seen: outcome.report.directories_seen,
            errors: outcome.report.errors,
            candidates,
        })
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
    module.add_class::<PyScanResult>()?;
    Ok(())
}
