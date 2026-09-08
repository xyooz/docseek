use std::collections::{BTreeSet, HashMap, HashSet};
use std::fs::{self, DirEntry, ReadDir};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use crate::candidate::{known_extensions, normalize_extension};
use crate::{CancellationToken, Candidate, CoreError};

pub const MAX_SCAN_BATCH_SIZE: usize = 128;

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
                .map(|name| name.to_lowercase())
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
pub struct ScanProgress {
    pub directories_seen: usize,
    pub files_seen: usize,
    pub candidates_discovered: usize,
    pub candidates_emitted: usize,
    pub errors: usize,
    pub current_path: Option<PathBuf>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ScanReport {
    pub directories_seen: usize,
    pub files_seen: usize,
    pub candidates_discovered: usize,
    pub candidates_emitted: usize,
    pub errors: usize,
    pub finished: bool,
    pub current_path: Option<PathBuf>,
}

impl ScanReport {
    pub(crate) fn from_progress(progress: &ScanProgress) -> Self {
        Self {
            directories_seen: progress.directories_seen,
            files_seen: progress.files_seen,
            candidates_discovered: progress.candidates_discovered,
            candidates_emitted: progress.candidates_emitted,
            errors: progress.errors,
            finished: true,
            current_path: progress.current_path.clone(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ScanBatch {
    pub candidates: Vec<Candidate>,
    pub finished: bool,
    pub progress: ScanProgress,
}

#[derive(Clone, Debug)]
pub struct Scanner {
    config: ScannerConfig,
    excluded_paths: Vec<PathBuf>,
}

pub(crate) struct DiscoveryBatch {
    pub candidates: Vec<Candidate>,
    pub finished: bool,
    pub progress: ScanProgress,
}

pub(crate) struct DiscoverySession {
    config: ScannerConfig,
    excluded_paths: Vec<PathBuf>,
    pending_directories: Vec<PathBuf>,
    current_entries: Option<ReadDir>,
    visited_directories: HashSet<PathBuf>,
    cancellation: CancellationToken,
    progress: Arc<Mutex<ScanProgress>>,
    exhausted: bool,
    finished: bool,
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
            .map(|name| name.to_lowercase())
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
        let progress = Arc::new(Mutex::new(ScanProgress::default()));
        let mut session = self.start_session_with_progress(root, cancellation.clone(), progress)?;
        loop {
            let batch = session.next_candidates(MAX_SCAN_BATCH_SIZE)?;
            if batch.finished {
                return Ok(ScanReport::from_progress(&batch.progress));
            }
        }
    }

    pub(crate) fn start_session_with_progress(
        &self,
        root: impl AsRef<Path>,
        cancellation: CancellationToken,
        progress: Arc<Mutex<ScanProgress>>,
    ) -> Result<DiscoverySession, CoreError> {
        let requested_root = root.as_ref().to_path_buf();
        let root = requested_root
            .canonicalize()
            .map_err(|source| CoreError::io(requested_root.clone(), source))?;
        let metadata = fs::metadata(&root).map_err(|source| CoreError::io(&root, source))?;
        if !metadata.is_dir() {
            return Err(CoreError::RootNotDirectory(root));
        }

        {
            let mut current = progress.lock().expect("scan progress mutex poisoned");
            current.directories_seen = 1;
            current.current_path = Some(root.clone());
        }

        let mut visited_directories = HashSet::new();
        visited_directories.insert(root.clone());
        let root_excluded = self.is_excluded_path(&root);
        Ok(DiscoverySession {
            config: self.config.clone(),
            excluded_paths: self.excluded_paths.clone(),
            pending_directories: if root_excluded {
                Vec::new()
            } else {
                vec![root]
            },
            current_entries: None,
            visited_directories,
            cancellation,
            progress,
            exhausted: root_excluded,
            finished: false,
        })
    }

    fn is_excluded_path(&self, path: &Path) -> bool {
        if self.excluded_paths.is_empty() {
            return false;
        }
        let normalized = normalize_path(path);
        self.excluded_paths
            .iter()
            .any(|excluded| is_under(&normalized, excluded))
    }
}

impl DiscoverySession {
    pub(crate) fn next_candidates(
        &mut self,
        requested_size: usize,
    ) -> Result<DiscoveryBatch, CoreError> {
        if requested_size == 0 {
            return Err(CoreError::InvalidBatchSize);
        }
        let batch_size = requested_size.min(MAX_SCAN_BATCH_SIZE);
        if self.finished {
            return Ok(DiscoveryBatch {
                candidates: Vec::new(),
                finished: true,
                progress: self.progress_snapshot(),
            });
        }

        let mut candidates = Vec::with_capacity(batch_size);
        while candidates.len() < batch_size && !self.exhausted {
            self.cancellation.checkpoint()?;
            match self.next_candidate()? {
                Some(candidate) => candidates.push(candidate),
                None => self.exhausted = true,
            }
        }
        if self.exhausted && candidates.is_empty() {
            self.finished = true;
        }
        self.update_progress(|progress| {
            progress.candidates_emitted =
                progress.candidates_emitted.saturating_add(candidates.len());
        });
        Ok(DiscoveryBatch {
            candidates,
            finished: self.finished,
            progress: self.progress_snapshot(),
        })
    }

    pub(crate) fn snapshot(&self) -> ScanProgress {
        self.progress_snapshot()
    }

    fn next_candidate(&mut self) -> Result<Option<Candidate>, CoreError> {
        loop {
            self.cancellation.checkpoint()?;
            if self.current_entries.is_none() && !self.open_next_directory()? {
                return Ok(None);
            }

            let next_entry = self
                .current_entries
                .as_mut()
                .and_then(|entries| entries.next());
            match next_entry {
                Some(Ok(entry)) => {
                    if let Some(candidate) = self.inspect_entry(entry)? {
                        return Ok(Some(candidate));
                    }
                }
                Some(Err(_)) => {
                    self.update_progress(|progress| progress.errors += 1);
                }
                None => {
                    self.current_entries = None;
                }
            }
        }
    }

    fn open_next_directory(&mut self) -> Result<bool, CoreError> {
        while let Some(directory) = self.pending_directories.pop() {
            self.cancellation.checkpoint()?;
            self.update_progress(|progress| progress.current_path = Some(directory.clone()));
            match fs::read_dir(&directory) {
                Ok(entries) => {
                    self.current_entries = Some(entries);
                    return Ok(true);
                }
                Err(_) => {
                    self.update_progress(|progress| progress.errors += 1);
                }
            }
        }
        Ok(false)
    }

    fn inspect_entry(&mut self, entry: DirEntry) -> Result<Option<Candidate>, CoreError> {
        let path = entry.path();
        self.update_progress(|progress| progress.current_path = Some(path.clone()));
        if self.is_excluded_path(&path) {
            return Ok(None);
        }

        let file_type = match entry.file_type() {
            Ok(file_type) => file_type,
            Err(_) => {
                self.update_progress(|progress| progress.errors += 1);
                return Ok(None);
            }
        };
        let is_symlink = file_type.is_symlink();
        let followed_metadata = if is_symlink {
            match fs::metadata(&path) {
                Ok(metadata) => Some(metadata),
                Err(_) => {
                    self.update_progress(|progress| progress.errors += 1);
                    return Ok(None);
                }
            }
        } else {
            None
        };
        let (is_directory, is_file) = match followed_metadata.as_ref() {
            Some(metadata) => (metadata.is_dir(), metadata.is_file()),
            None => (file_type.is_dir(), file_type.is_file()),
        };

        if is_directory {
            let name = entry.file_name().to_string_lossy().to_lowercase();
            if !self.config.ignored_dir_names.contains(&name) {
                let identity = if is_symlink {
                    normalize_path(&path)
                } else {
                    path.clone()
                };
                if self.visited_directories.insert(identity) {
                    self.pending_directories.push(path);
                    self.update_progress(|progress| progress.directories_seen += 1);
                }
            }
            return Ok(None);
        }
        if !is_file {
            return Ok(None);
        }

        self.update_progress(|progress| progress.files_seen += 1);
        let file_name = entry.file_name();
        let name = file_name.to_string_lossy();
        if name.starts_with("~$") || name.ends_with(".tmp") {
            return Ok(None);
        }

        let extension = Candidate::extension_for_path(&path);
        if !self.config.enabled_extensions.contains(&extension) || self.matches_file_pattern(&path)
        {
            return Ok(None);
        }

        let metadata = match followed_metadata {
            Some(metadata) => metadata,
            None => match entry.metadata() {
                Ok(metadata) => metadata,
                Err(_) => {
                    self.update_progress(|progress| progress.errors += 1);
                    return Ok(None);
                }
            },
        };
        if self
            .config
            .max_file_size
            .is_some_and(|limit| metadata.len() > limit)
        {
            return Ok(None);
        }

        let candidate = Candidate::from_metadata(path, metadata.len());
        self.update_progress(|progress| progress.candidates_discovered += 1);
        Ok(Some(candidate))
    }

    fn is_excluded_path(&self, path: &Path) -> bool {
        if self.excluded_paths.is_empty() {
            return false;
        }
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
            .to_lowercase();
        self.config
            .excluded_file_patterns
            .iter()
            .any(|pattern| glob_matches(pattern, &name))
    }

    fn update_progress(&self, update: impl FnOnce(&mut ScanProgress)) {
        let mut progress = self.progress.lock().expect("scan progress mutex poisoned");
        update(&mut progress);
    }

    fn progress_snapshot(&self) -> ScanProgress {
        self.progress
            .lock()
            .expect("scan progress mutex poisoned")
            .clone()
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
    let pattern = pattern.trim().to_lowercase();
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
    let pattern: Vec<char> = pattern.chars().collect();
    let value: Vec<char> = value.chars().collect();
    let mut memo = HashMap::new();
    glob_matches_from(&pattern, 0, &value, 0, &mut memo)
}

fn glob_matches_from(
    pattern: &[char],
    pattern_index: usize,
    value: &[char],
    value_index: usize,
    memo: &mut HashMap<(usize, usize), bool>,
) -> bool {
    if let Some(result) = memo.get(&(pattern_index, value_index)) {
        return *result;
    }
    let result = if pattern_index == pattern.len() {
        value_index == value.len()
    } else {
        match pattern[pattern_index] {
            '*' => {
                glob_matches_from(pattern, pattern_index + 1, value, value_index, memo)
                    || (value_index < value.len()
                        && glob_matches_from(pattern, pattern_index, value, value_index + 1, memo))
            }
            '?' if value_index < value.len() => {
                glob_matches_from(pattern, pattern_index + 1, value, value_index + 1, memo)
            }
            '[' if value_index < value.len() => {
                if let Some((matched, next_index)) =
                    match_character_class(pattern, pattern_index, value[value_index])
                {
                    matched && glob_matches_from(pattern, next_index, value, value_index + 1, memo)
                } else {
                    pattern[pattern_index] == value[value_index]
                        && glob_matches_from(
                            pattern,
                            pattern_index + 1,
                            value,
                            value_index + 1,
                            memo,
                        )
                }
            }
            character if value_index < value.len() && character == value[value_index] => {
                glob_matches_from(pattern, pattern_index + 1, value, value_index + 1, memo)
            }
            _ => false,
        }
    };
    memo.insert((pattern_index, value_index), result);
    result
}

fn match_character_class(pattern: &[char], start: usize, value: char) -> Option<(bool, usize)> {
    let mut index = start + 1;
    if index >= pattern.len() {
        return None;
    }
    let negated = matches!(pattern[index], '!' | '^');
    if negated {
        index += 1;
    }

    let mut matched = false;
    let mut saw_character = false;
    while index < pattern.len() {
        if pattern[index] == ']' && saw_character {
            let result = if negated { !matched } else { matched };
            return Some((result, index + 1));
        }
        if index + 2 < pattern.len() && pattern[index + 1] == '-' && pattern[index + 2] != ']' {
            let first = pattern[index];
            let last = pattern[index + 2];
            if first <= value && value <= last {
                matched = true;
            }
            index += 3;
        } else {
            if pattern[index] == value {
                matched = true;
            }
            index += 1;
        }
        saw_character = true;
    }
    None
}
