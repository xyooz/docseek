use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use docseek_core::{
    CancellationToken, CandidatePriority, CoreError, JobController, JobState, Scanner,
    ScannerConfig,
};

fn temp_root(label: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock before epoch")
        .as_nanos();
    std::env::temp_dir().join(format!("docseek-core-{label}-{nonce}"))
}

fn write_file(path: &Path, contents: &str) {
    fs::write(path, contents).expect("write fixture");
}

#[test]
fn scanner_filters_candidates_and_scheduler_keeps_lanes_stable() {
    let root = temp_root("scan");
    fs::create_dir_all(root.join("nested")).expect("create root");
    fs::create_dir_all(root.join(".git")).expect("create ignored root");
    write_file(&root.join("notes.txt"), "notes");
    write_file(&root.join("modern.docx"), "docx");
    write_file(&root.join("legacy.wps"), "wps");
    write_file(&root.join("nested").join("report.xlsx"), "xlsx");
    write_file(&root.join(".git").join("hidden.txt"), "hidden");
    write_file(&root.join("skip.tmp"), "tmp");

    let report = Scanner::new(ScannerConfig::default())
        .scan(&root, &CancellationToken::new())
        .expect("scan succeeds");
    assert_eq!(report.candidates.len(), 4);
    assert!(report
        .candidates
        .iter()
        .all(|candidate| !candidate.path.to_string_lossy().contains(".git")));

    let mut scheduler = docseek_core::CandidateScheduler::from_candidates(report.candidates);
    assert_eq!(
        scheduler.pop().expect("first candidate").priority,
        CandidatePriority::ModernOffice
    );
    assert_eq!(
        scheduler.pop().expect("second candidate").priority,
        CandidatePriority::ModernOffice
    );
    assert_eq!(
        scheduler.pop().expect("third candidate").priority,
        CandidatePriority::Direct
    );
    assert_eq!(
        scheduler.pop().expect("fourth candidate").priority,
        CandidatePriority::OfficeCompatibility
    );
    assert!(scheduler.is_empty());

    fs::remove_dir_all(root).expect("remove fixture");
}

#[test]
fn scanner_honours_exclusions_and_cancellation() {
    let root = temp_root("cancel");
    fs::create_dir_all(root.join("excluded")).expect("create root");
    write_file(&root.join("keep.txt"), "keep");
    write_file(&root.join("secret.txt"), "secret");
    write_file(&root.join("excluded").join("nested.txt"), "nested");

    let config = ScannerConfig {
        excluded_paths: vec![root.join("excluded")],
        excluded_file_patterns: vec!["secret.*".to_owned()],
        ..ScannerConfig::default()
    };
    let report = Scanner::new(config)
        .scan(&root, &CancellationToken::new())
        .expect("scan succeeds");
    assert_eq!(report.candidates.len(), 1);
    assert!(report.candidates[0].path.ends_with("keep.txt"));

    let token = CancellationToken::new();
    token.cancel();
    assert!(matches!(
        Scanner::new(ScannerConfig::default()).scan(&root, &token),
        Err(CoreError::Cancelled)
    ));

    fs::remove_dir_all(root).expect("remove fixture");
}

#[test]
fn controller_records_successful_lifecycle() {
    let root = temp_root("controller");
    fs::create_dir_all(&root).expect("create root");
    write_file(&root.join("one.txt"), "one");

    let controller = JobController::new();
    let outcome = controller
        .run_scan(&root, ScannerConfig::default())
        .expect("controller scan succeeds");
    let snapshot = controller
        .snapshot(outcome.job_id)
        .expect("snapshot exists");
    assert_eq!(snapshot.state, JobState::Succeeded);
    assert_eq!(snapshot.candidates, 1);
    assert!(!controller.cancel());

    fs::remove_dir_all(root).expect("remove fixture");
}
