use std::fmt;
use std::io;
use std::path::PathBuf;

/// Errors that belong to the orchestration layer rather than to a parser.
#[derive(Debug)]
pub enum CoreError {
    Cancelled,
    Busy,
    InvalidBatchSize,
    RootNotDirectory(PathBuf),
    Io { path: PathBuf, source: io::Error },
}

impl CoreError {
    pub(crate) fn io(path: impl Into<PathBuf>, source: io::Error) -> Self {
        Self::Io {
            path: path.into(),
            source,
        }
    }
}

impl fmt::Display for CoreError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Cancelled => formatter.write_str("job cancelled"),
            Self::Busy => formatter.write_str("another job is already running"),
            Self::InvalidBatchSize => {
                formatter.write_str("scan batch size must be greater than zero")
            }
            Self::RootNotDirectory(path) => {
                write!(
                    formatter,
                    "scan root is not a directory: {}",
                    path.display()
                )
            }
            Self::Io { path, source } => {
                write!(formatter, "I/O error at {}: {source}", path.display())
            }
        }
    }
}

impl std::error::Error for CoreError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io { source, .. } => Some(source),
            _ => None,
        }
    }
}
