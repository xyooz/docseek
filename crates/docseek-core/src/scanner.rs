use std::collections::{BTreeSet, HashMap, HashSet};
use std::fs::{self, DirEntry, ReadDir};
use std::io;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use crate::candidate::{known_extensions, normalize_extension};
use crate::{CancellationToken, Candidate, CoreError};

pub const MAX_SCAN_BATCH_SIZE: usize = 128;
pub const MAX_SCAN_WORK_ITEMS: usize = 2_000;

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
    /// Optional standalone scanner limit. The Python backend adapter leaves
    /// this unset so the Python indexer can apply its lifecycle policy and
    /// record oversized-file issues.
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
            max_file_size: None,
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
    pub excluded: usize,
    pub errors: usize,
    pub current_path: Option<PathBuf>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ScanReport {
    pub directories_seen: usize,
    pub files_seen: usize,
    pub candidates_discovered: usize,
    pub candidates_emitted: usize,
    pub excluded: usize,
    pub errors: usize,
    pub finished: bool,
    pub current_path: Option<PathBuf>,
    pub issues: Vec<ScanIssue>,
}

impl ScanReport {
    pub(crate) fn from_progress(progress: &ScanProgress, issues: Vec<ScanIssue>) -> Self {
        Self {
            directories_seen: progress.directories_seen,
            files_seen: progress.files_seen,
            candidates_discovered: progress.candidates_discovered,
            candidates_emitted: progress.candidates_emitted,
            excluded: progress.excluded,
            errors: progress.errors,
            finished: true,
            current_path: progress.current_path.clone(),
            issues,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ScanIssue {
    pub path: PathBuf,
    pub error_code: String,
    pub detail: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ScanBatch {
    pub candidates: Vec<Candidate>,
    pub finished: bool,
    pub progress: ScanProgress,
    pub issues: Vec<ScanIssue>,
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
    pub issues: Vec<ScanIssue>,
}

pub(crate) struct DiscoverySession {
    config: ScannerConfig,
    excluded_paths: Vec<PathBuf>,
    pending_directories: Vec<PathBuf>,
    current_entries: Option<ReadDir>,
    visited_directories: HashSet<PathBuf>,
    cancellation: CancellationToken,
    progress: Arc<Mutex<ScanProgress>>,
    pending_issues: Vec<ScanIssue>,
    work_items: usize,
    current_directory: Option<PathBuf>,
    exhausted: bool,
    finished: bool,
}

enum NextCandidate {
    Candidate(Option<Candidate>),
    WorkBudget,
}

enum OpenDirectory {
    Opened,
    WorkBudget,
    Exhausted,
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
        let mut issues = Vec::new();
        loop {
            let batch = session.next_candidates(MAX_SCAN_BATCH_SIZE)?;
            issues.extend(batch.issues);
            if batch.finished {
                return Ok(ScanReport::from_progress(&batch.progress, issues));
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
        let root = dunce::canonicalize(&requested_root)
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
        if root_excluded {
            let mut current = progress.lock().expect("scan progress mutex poisoned");
            current.excluded += 1;
        }
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
            pending_issues: Vec::new(),
            work_items: 0,
            current_directory: None,
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
                issues: Vec::new(),
            });
        }

        self.work_items = 0;
        let mut candidates = Vec::with_capacity(batch_size);
        while candidates.len() < batch_size && !self.exhausted {
            self.cancellation.checkpoint()?;
            match self.next_candidate()? {
                NextCandidate::Candidate(Some(candidate)) => candidates.push(candidate),
                NextCandidate::Candidate(None) => self.exhausted = true,
                NextCandidate::WorkBudget => break,
            }
        }
        if self.exhausted && candidates.is_empty() {
            self.finished = true;
        }
        self.update_progress(|progress| {
            progress.candidates_emitted =
                progress.candidates_emitted.saturating_add(candidates.len());
        });
        let issues = std::mem::take(&mut self.pending_issues);
        Ok(DiscoveryBatch {
            candidates,
            finished: self.finished,
            progress: self.progress_snapshot(),
            issues,
        })
    }

    pub(crate) fn snapshot(&self) -> ScanProgress {
        self.progress_snapshot()
    }

    fn next_candidate(&mut self) -> Result<NextCandidate, CoreError> {
        loop {
            self.cancellation.checkpoint()?;
            if self.work_items >= MAX_SCAN_WORK_ITEMS {
                return Ok(NextCandidate::WorkBudget);
            }
            if self.current_entries.is_none() {
                match self.open_next_directory()? {
                    OpenDirectory::Opened => {}
                    OpenDirectory::WorkBudget => return Ok(NextCandidate::WorkBudget),
                    OpenDirectory::Exhausted => {
                        return Ok(NextCandidate::Candidate(None));
                    }
                }
            }

            let next_entry = self
                .current_entries
                .as_mut()
                .and_then(|entries| entries.next());
            match next_entry {
                Some(Ok(entry)) => {
                    self.work_items += 1;
                    if let Some(candidate) = self.inspect_entry(entry)? {
                        return Ok(NextCandidate::Candidate(Some(candidate)));
                    }
                    if self.work_items >= MAX_SCAN_WORK_ITEMS {
                        return Ok(NextCandidate::WorkBudget);
                    }
                }
                Some(Err(error)) => {
                    self.work_items += 1;
                    let path = self
                        .current_directory
                        .clone()
                        .unwrap_or_else(|| PathBuf::from("."));
                    self.record_issue(path, &error);
                    if self.work_items >= MAX_SCAN_WORK_ITEMS {
                        return Ok(NextCandidate::WorkBudget);
                    }
                }
                None => {
                    self.current_entries = None;
                }
            }
        }
    }

    fn open_next_directory(&mut self) -> Result<OpenDirectory, CoreError> {
        while let Some(directory) = self.pending_directories.pop() {
            self.cancellation.checkpoint()?;
            if self.work_items >= MAX_SCAN_WORK_ITEMS {
                return Ok(OpenDirectory::WorkBudget);
            }
            self.work_items += 1;
            self.current_directory = Some(directory.clone());
            self.update_progress(|progress| progress.current_path = Some(directory.clone()));
            match fs::read_dir(&directory) {
                Ok(entries) => {
                    self.current_entries = Some(entries);
                    return Ok(OpenDirectory::Opened);
                }
                Err(error) => {
                    self.record_issue(external_path(&directory), &error);
                    if self.work_items >= MAX_SCAN_WORK_ITEMS {
                        return Ok(OpenDirectory::WorkBudget);
                    }
                }
            }
        }
        Ok(OpenDirectory::Exhausted)
    }

    fn inspect_entry(&mut self, entry: DirEntry) -> Result<Option<Candidate>, CoreError> {
        // Keep the path used for the external candidate contract separate from
        // canonicalized identity paths. In particular, std::fs::canonicalize
        // on Windows returns a `\\?\\` verbatim path; that prefix must not
        // leak into Python or SQLite path strings.
        let path = external_path(&entry.path());
        self.update_progress(|progress| progress.current_path = Some(path.clone()));
        if self.is_excluded_path(&path) {
            self.update_progress(|progress| progress.excluded += 1);
            return Ok(None);
        }

        let file_type = match entry.file_type() {
            Ok(file_type) => file_type,
            Err(error) => {
                self.record_issue(path, &error);
                return Ok(None);
            }
        };
        let is_symlink = file_type.is_symlink();
        let followed_metadata = if is_symlink {
            match fs::metadata(&path) {
                Ok(metadata) => Some(metadata),
                Err(error) => {
                    self.record_issue(path, &error);
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
        if !self.config.enabled_extensions.contains(&extension) {
            return Ok(None);
        }
        if self.matches_file_pattern(&path) {
            self.update_progress(|progress| progress.excluded += 1);
            return Ok(None);
        }

        let metadata = match followed_metadata {
            Some(metadata) => metadata,
            None => match entry.metadata() {
                Ok(metadata) => metadata,
                Err(error) => {
                    self.record_issue(path, &error);
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

    fn record_issue(&mut self, path: PathBuf, error: &io::Error) {
        self.update_progress(|progress| progress.errors += 1);
        self.pending_issues.push(ScanIssue {
            path,
            error_code: io_error_code(error).to_owned(),
            detail: error.to_string(),
        });
    }

    fn progress_snapshot(&self) -> ScanProgress {
        self.progress
            .lock()
            .expect("scan progress mutex poisoned")
            .clone()
    }
}

fn io_error_code(error: &io::Error) -> &'static str {
    match error.kind() {
        io::ErrorKind::PermissionDenied => "permission_denied",
        io::ErrorKind::NotFound => "file_not_found",
        _ => "os_error",
    }
}

fn normalize_path(path: &Path) -> PathBuf {
    dunce::canonicalize(path)
        .map(|resolved| external_path(&resolved))
        .unwrap_or_else(|_| {
            let absolute = if path.is_absolute() {
                path.to_path_buf()
            } else {
                std::env::current_dir()
                    .map(|current| current.join(path))
                    .unwrap_or_else(|_| path.to_path_buf())
            };
            external_path(&absolute)
        })
}

fn external_path(path: &Path) -> PathBuf {
    dunce::simplified(path).to_path_buf()
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
