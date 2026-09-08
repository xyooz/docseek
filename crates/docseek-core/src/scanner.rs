use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use crate::candidate::{known_extensions, normalize_extension};
use crate::{CancellationToken, Candidate, CoreError};

const DEFAULT_IGNORED_DIR_NAMES: &[&str] = &[
    ".git",
    ".svn",
    ".hg",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
    "$recycle.bin",
    "system volume information",
];

#[derive(Clone, Debug)]
pub struct ScannerConfig {
    pub enabled_extensions: BTreeSet<String>,
    pub ignored_dir_names: BTreeSet<String>,
    pub excluded_paths: Vec<PathBuf>,
    pub excluded_file_patterns: Vec<String>,
    pub max_file_size: Option<u64>,
}

impl Default for ScannerConfig {
    fn default() -> Self {
        Self {
            enabled_extensions: known_extensions().map(normalize_extension).collect(),
            ignored_dir_names: DEFAULT_IGNORED_DIR_NAMES
                .iter()
                .map(|name| name.to_ascii_lowercase())
                .collect(),
            excluded_paths: Vec::new(),
            excluded_file_patterns: Vec::new(),
            max_file_size: Some(200 * 1024 * 1024),
        }
    }
}

impl ScannerConfig {
    pub fn with_enabled_extensions<I, S>(mut self, extensions: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        self.enabled_extensions = extensions
            .into_iter()
            .map(|extension| normalize_extension(extension.as_ref()))
            .filter(|extension| !extension.is_empty())
            .collect();
        self
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ScanReport {
    pub files_seen: usize,
    pub directories_seen: usize,
    pub errors: usize,
    pub candidates: Vec<Candidate>,
}

#[derive(Clone, Debug)]
pub struct Scanner {
    config: ScannerConfig,
    excluded_paths: Vec<PathBuf>,
}

impl Scanner {
    pub fn new(mut config: ScannerConfig) -> Self {
        config.enabled_extensions = config
            .enabled_extensions
            .iter()
            .map(|extension| normalize_extension(extension))
            .filter(|extension| !extension.is_empty())
            .collect();
        config.ignored_dir_names = config
            .ignored_dir_names
            .iter()
            .map(|name| name.to_ascii_lowercase())
            .collect();
        config.excluded_file_patterns = config
            .excluded_file_patterns
            .iter()
            .map(|pattern| normalize_file_pattern(pattern))
            .filter(|pattern| !pattern.is_empty())
            .collect();
        let excluded_paths = config
            .excluded_paths
            .iter()
            .map(|path| normalize_path(path))
            .collect();
        Self {
            config,
            excluded_paths,
        }
    }

    pub fn scan(
        &self,
        root: impl AsRef<Path>,
        cancellation: &CancellationToken,
    ) -> Result<ScanReport, CoreError> {
        let requested_root = root.as_ref().to_path_buf();
        let root = requested_root
            .canonicalize()
            .map_err(|source| CoreError::io(requested_root.clone(), source))?;
        let metadata = fs::metadata(&root).map_err(|source| CoreError::io(&root, source))?;
        if !metadata.is_dir() {
            return Err(CoreError::RootNotDirectory(root));
        }

        let mut report = ScanReport {
            directories_seen: 1,
            ..ScanReport::default()
        };
        let mut pending = vec![root];

        while let Some(directory) = pending.pop() {
            cancellation.checkpoint()?;
            let entries = match fs::read_dir(&directory) {
                Ok(entries) => entries.collect::<Result<Vec<_>, _>>(),
                Err(_) => {
                    report.errors += 1;
                    continue;
                }
            };
            let mut entries = match entries {
                Ok(entries) => entries,
                Err(_) => {
                    report.errors += 1;
                    continue;
                }
            };
            entries.sort_by_key(|entry| entry.path());

            for entry in entries {
                cancellation.checkpoint()?;
                let path = entry.path();
                if self.is_excluded_path(&path) {
                    continue;
                }
                let file_type = match entry.file_type() {
                    Ok(file_type) => file_type,
                    Err(_) => {
                        report.errors += 1;
                        continue;
                    }
                };
                // Phase 1 does not follow symlinks. This keeps a scan bounded
                // and avoids directory cycles; symlink policy can be added to
                // ScannerConfig when Python parity is implemented.
                if file_type.is_symlink() {
                    continue;
                }
                if file_type.is_dir() {
                    let name = entry.file_name().to_string_lossy().to_ascii_lowercase();
                    if !self.config.ignored_dir_names.contains(&name) {
                        pending.push(path);
                        report.directories_seen += 1;
                    }
                    continue;
                }
                if !file_type.is_file() {
                    continue;
                }

                report.files_seen += 1;
                let metadata = match entry.metadata() {
                    Ok(metadata) => metadata,
                    Err(_) => {
                        report.errors += 1;
                        continue;
                    }
                };
                if self
                    .config
                    .max_file_size
                    .is_some_and(|limit| metadata.len() > limit)
                {
                    continue;
                }

                let extension = Candidate::extension_for_path(&path);
                if !self.config.enabled_extensions.contains(&extension)
                    || self.matches_file_pattern(&path)
                {
                    continue;
                }
                report
                    .candidates
                    .push(Candidate::from_metadata(path, metadata.len()));
            }
        }

        Ok(report)
    }

    fn is_excluded_path(&self, path: &Path) -> bool {
        let normalized = normalize_path(path);
        self.excluded_paths
            .iter()
            .any(|excluded| is_under(&normalized, excluded))
    }

    fn matches_file_pattern(&self, path: &Path) -> bool {
        let name = path
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or_default()
            .to_ascii_lowercase();
        self.config
            .excluded_file_patterns
            .iter()
            .map(|pattern| pattern.to_ascii_lowercase())
            .any(|pattern| glob_matches(&pattern, &name))
    }
}

fn normalize_path(path: &Path) -> PathBuf {
    path.canonicalize().unwrap_or_else(|_| {
        if path.is_absolute() {
            path.to_path_buf()
        } else {
            std::env::current_dir()
                .map(|current| current.join(path))
                .unwrap_or_else(|_| path.to_path_buf())
        }
    })
}

fn is_under(path: &Path, parent: &Path) -> bool {
    path == parent || path.strip_prefix(parent).is_ok()
}

fn normalize_file_pattern(pattern: &str) -> String {
    let pattern = pattern.trim().to_ascii_lowercase();
    if pattern.starts_with('.')
        && !pattern
            .chars()
            .any(|character| matches!(character, '*' | '?' | '['))
    {
        format!("*{pattern}")
    } else {
        pattern
    }
}

fn glob_matches(pattern: &str, value: &str) -> bool {
    let pattern = pattern.as_bytes();
    let value = value.as_bytes();
    let mut states = vec![vec![false; value.len() + 1]; pattern.len() + 1];
    states[0][0] = true;
    for pattern_index in 0..pattern.len() {
        for value_index in 0..=value.len() {
            if !states[pattern_index][value_index] {
                continue;
            }
            match pattern[pattern_index] {
                b'*' => {
                    states[pattern_index + 1][value_index] = true;
                    if value_index < value.len() {
                        states[pattern_index][value_index + 1] = true;
                    }
                }
                b'?' if value_index < value.len() => {
                    states[pattern_index + 1][value_index + 1] = true;
                }
                character if value_index < value.len() && character == value[value_index] => {
                    states[pattern_index + 1][value_index + 1] = true;
                }
                _ => {}
            }
        }
    }
    states[pattern.len()][value.len()]
}
