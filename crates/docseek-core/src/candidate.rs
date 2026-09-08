use std::path::Path;

/// Lower values are earlier lanes in the indexing pipeline.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
pub enum CandidatePriority {
    ModernOffice = 0,
    Direct = 1,
    OfficeCompatibility = 2,
    Other = 3,
}

const MODERN_OFFICE_EXTENSIONS: &[&str] = &[".docx", ".xlsx", ".pptx", ".pdf"];
const DIRECT_EXTENSIONS: &[&str] = &[
    ".txt", ".md", ".log", ".csv", ".tsv", ".html", ".htm", ".xhtml", ".xml", ".docx", ".xlsx",
    ".pptx", ".pdf",
];
const OFFICE_COMPATIBILITY_EXTENSIONS: &[&str] = &[
    ".doc", ".dot", ".rtf", ".odt", ".ppt", ".pps", ".odp", ".xls", ".xlsb", ".ods", ".wps",
    ".wpt", ".et", ".ett", ".etx", ".ettx", ".xlt", ".dps", ".dpt", ".epub", ".eml", ".msg",
    ".mbox", ".pst", ".pages", ".numbers", ".key",
];

pub fn normalize_extension(extension: &str) -> String {
    let value = extension.trim().to_ascii_lowercase();
    if value.is_empty() {
        String::new()
    } else if value.starts_with('.') {
        value
    } else {
        format!(".{value}")
    }
}

pub fn priority_for_extension(extension: &str) -> CandidatePriority {
    let extension = normalize_extension(extension);
    if MODERN_OFFICE_EXTENSIONS.contains(&extension.as_str()) {
        CandidatePriority::ModernOffice
    } else if DIRECT_EXTENSIONS.contains(&extension.as_str()) {
        CandidatePriority::Direct
    } else if OFFICE_COMPATIBILITY_EXTENSIONS.contains(&extension.as_str()) {
        CandidatePriority::OfficeCompatibility
    } else {
        CandidatePriority::Other
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Candidate {
    pub path: std::path::PathBuf,
    pub extension: String,
    pub size: u64,
    pub priority: CandidatePriority,
}

impl Candidate {
    pub(crate) fn from_metadata(path: std::path::PathBuf, size: u64) -> Self {
        let extension = path
            .extension()
            .and_then(|value| value.to_str())
            .map(normalize_extension)
            .unwrap_or_default();
        let priority = priority_for_extension(&extension);
        Self {
            path,
            extension,
            size,
            priority,
        }
    }

    pub fn extension_for_path(path: &Path) -> String {
        path.extension()
            .and_then(|value| value.to_str())
            .map(normalize_extension)
            .unwrap_or_default()
    }
}

pub(crate) fn known_extensions() -> impl Iterator<Item = &'static str> {
    [
        ".txt", ".md", ".log", ".csv", ".tsv", ".html", ".htm", ".xhtml", ".xml", ".docx", ".xlsx",
        ".pptx", ".pdf", ".doc", ".dot", ".rtf", ".odt", ".ppt", ".pps", ".odp", ".xls", ".xlsb",
        ".ods", ".epub", ".eml", ".msg", ".mbox", ".pst", ".pages", ".numbers", ".key", ".wps",
        ".wpt", ".et", ".ett", ".etx", ".ettx", ".xlt", ".dps", ".dpt",
    ]
    .into_iter()
}
