use std::fs;
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};

use docseek_core::{
    Candidate, CandidatePriority, CandidateScheduler, CoreError, JobController, JobState,
    ScanSession, ScannerConfig, MAX_SCAN_BATCH_SIZE,
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

fn collect_candidates(
    session: &mut ScanSession,
    requested_size: usize,
) -> Result<Vec<Candidate>, CoreError> {
    let mut candidates = Vec::new();
    loop {
        let batch = session.next_batch(requested_size)?;
        assert!(batch.candidates.len() <= MAX_SCAN_BATCH_SIZE);
        candidates.extend(batch.candidates);
        if batch.finished {
            return Ok(candidates);
        }
    }
}

#[test]
fn scanner_filters_candidates_and_preserves_priority_lanes() {
    let root = temp_root("scan");
    fs::create_dir_all(root.join("nested")).expect("create root");
    fs::create_dir_all(root.join(".git")).expect("create ignored root");
    write_file(&root.join("notes.txt"), "notes");
    write_file(&root.join("modern.docx"), "docx");
    write_file(&root.join("legacy.wps"), "wps");
    write_file(&root.join("mail.eml"), "mail");
    write_file(&root.join("nested").join("report.xlsx"), "xlsx");
    write_file(&root.join(".git").join("hidden.txt"), "hidden");
    write_file(&root.join("skip.tmp"), "tmp");
    write_file(&root.join("~$locked.docx"), "temporary office lock");
    write_file(&root.join("unknown.xyz"), "unknown");

    let controller = JobController::new();
    let mut session = controller
        .start_scan(&root, ScannerConfig::default())
        .expect("scan starts");
    let candidates = collect_candidates(&mut session, MAX_SCAN_BATCH_SIZE).expect("scan succeeds");

    assert_eq!(candidates.len(), 5);
    assert_eq!(
        candidates
            .iter()
            .map(|candidate| candidate.priority)
            .collect::<Vec<_>>(),
        vec![
            CandidatePriority::ModernOffice,
            CandidatePriority::ModernOffice,
            CandidatePriority::Direct,
            CandidatePriority::OfficeCompatibility,
            CandidatePriority::Other,
        ]
    );
    assert!(candidates.iter().all(|candidate| {
        !candidate.path.to_string_lossy().contains(".git")
            && !candidate.path.to_string_lossy().ends_with(".tmp")
            && !candidate.path.to_string_lossy().contains("~$")
    }));

    let snapshot = session.snapshot().expect("snapshot exists");
    assert_eq!(snapshot.state, JobState::Succeeded);
    assert_eq!(snapshot.candidates_discovered, 5);
    assert_eq!(snapshot.candidates_emitted, 5);

    fs::remove_dir_all(root).expect("remove fixture");
}

#[test]
fn scanner_streams_bounded_batches_and_tracks_progress() {
    let root = temp_root("stream");
    fs::create_dir_all(&root).expect("create root");
    for index in 0..300 {
        write_file(&root.join(format!("file-{index:03}.txt")), "content");
    }

    let controller = JobController::new();
    let mut session = controller
        .start_scan(&root, ScannerConfig::default())
        .expect("scan starts");
    assert!(matches!(
        session.next_batch(0),
        Err(CoreError::InvalidBatchSize)
    ));

    let mut batch_sizes = Vec::new();
    let mut total = 0;
    loop {
        let batch = session.next_batch(10_000).expect("scan batch succeeds");
        batch_sizes.push(batch.candidates.len());
        total += batch.candidates.len();
        assert!(batch.candidates.len() <= MAX_SCAN_BATCH_SIZE);
        if batch.finished {
            break;
        }
    }

    assert_eq!(batch_sizes, vec![128, 128, 44, 0]);
    assert_eq!(total, 300);
    let snapshot = controller.last_snapshot().expect("snapshot exists");
    assert_eq!(snapshot.state, JobState::Succeeded);
    assert_eq!(snapshot.files_seen, 300);
    assert_eq!(snapshot.candidates_discovered, 300);
    assert_eq!(snapshot.candidates_emitted, 300);

    fs::remove_dir_all(root).expect("remove fixture");
}

#[test]
fn scanner_honours_empty_exclusions_glob_classes_and_unicode_paths() {
    let root = temp_root("filters");
    let unicode_dir = root.join("项目资料");
    fs::create_dir_all(&unicode_dir).expect("create root");
    fs::create_dir_all(root.join("excluded")).expect("create excluded directory");
    write_file(&root.join("keep.txt"), "keep");
    write_file(&root.join("secret.txt"), "secret");
    write_file(&root.join("report1.txt"), "one");
    write_file(&root.join("report7.txt"), "seven");
    write_file(&root.join("reporta.txt"), "letter");
    write_file(&root.join("trace.log"), "log");
    write_file(&unicode_dir.join("说明.txt"), "unicode");
    write_file(&root.join("excluded").join("nested.txt"), "nested");

    let config = ScannerConfig {
        excluded_paths: vec![root.join("excluded")],
        excluded_file_patterns: vec![
            "secret.*".to_owned(),
            "report[0-9].txt".to_owned(),
            ".log".to_owned(),
        ],
        ..ScannerConfig::default()
    };
    let controller = JobController::new();
    let mut session = controller.start_scan(&root, config).expect("scan starts");
    let candidates = collect_candidates(&mut session, 128).expect("scan succeeds");
    let mut paths = candidates
        .iter()
        .map(|candidate| candidate.path.clone())
        .collect::<Vec<_>>();
    paths.sort();

    assert_eq!(paths.len(), 3);
    assert!(paths.iter().any(|path| path.ends_with("keep.txt")));
    assert!(paths.iter().any(|path| path.ends_with("reporta.txt")));
    assert!(paths.iter().any(|path| path.ends_with("说明.txt")));
    assert!(!paths.iter().any(|path| path.ends_with("nested.txt")));

    fs::remove_dir_all(root).expect("remove fixture");
}

#[test]
fn controller_rejects_a_second_active_job_and_recovers_after_drop() {
    let root = temp_root("busy");
    fs::create_dir_all(&root).expect("create root");
    write_file(&root.join("one.txt"), "one");

    let controller = JobController::new();
    let session = controller
        .start_scan(&root, ScannerConfig::default())
        .expect("first scan starts");
    assert!(matches!(
        controller.start_scan(&root, ScannerConfig::default()),
        Err(CoreError::Busy)
    ));
    drop(session);

    let next_session = controller
        .start_scan(&root, ScannerConfig::default())
        .expect("controller recovers after dropped session");
    drop(next_session);

    fs::remove_dir_all(root).expect("remove fixture");
}

#[test]
fn cancellation_from_another_thread_transitions_through_cancelling() {
    let root = temp_root("cancel-thread");
    fs::create_dir_all(&root).expect("create root");
    for index in 0..100 {
        write_file(&root.join(format!("file-{index:03}.txt")), "content");
    }

    let controller = JobController::new();
    let session = controller
        .start_scan(&root, ScannerConfig::default())
        .expect("scan starts");
    let (ready_sender, ready_receiver) = mpsc::sync_channel(0);
    let (go_sender, go_receiver) = mpsc::sync_channel(0);
    let worker = thread::spawn(move || {
        let mut session = session;
        ready_sender.send(()).expect("signal worker ready");
        go_receiver.recv().expect("wait for cancellation");
        session.next_batch(1)
    });

    ready_receiver.recv().expect("worker is ready");
    let controller_for_cancel = controller.clone();
    let canceller = thread::spawn(move || controller_for_cancel.cancel());
    assert!(canceller.join().expect("join canceller"));
    assert!(controller.cancel());
    assert_eq!(
        controller.last_snapshot().expect("snapshot exists").state,
        JobState::Cancelling
    );
    go_sender.send(()).expect("release worker");

    assert!(matches!(
        worker.join().expect("join worker"),
        Err(CoreError::Cancelled)
    ));
    assert_eq!(
        controller.last_snapshot().expect("snapshot exists").state,
        JobState::Cancelled
    );
    assert!(!controller.cancel());

    fs::remove_dir_all(root).expect("remove fixture");
}

#[test]
fn scheduler_preserves_lane_priority_and_encounter_order() {
    let candidate = |name: &str, priority: CandidatePriority| Candidate {
        path: PathBuf::from(name),
        extension: String::from(".fixture"),
        size: 1,
        priority,
    };
    let mut scheduler = CandidateScheduler::new();
    scheduler.extend([
        candidate("other-a", CandidatePriority::Other),
        candidate("direct-a", CandidatePriority::Direct),
        candidate("modern-a", CandidatePriority::ModernOffice),
        candidate("office-a", CandidatePriority::OfficeCompatibility),
        candidate("direct-b", CandidatePriority::Direct),
        candidate("modern-b", CandidatePriority::ModernOffice),
    ]);

    let paths = scheduler
        .into_sorted_vec()
        .into_iter()
        .map(|candidate| candidate.path)
        .collect::<Vec<_>>();
    assert_eq!(
        paths,
        vec![
            PathBuf::from("modern-a"),
            PathBuf::from("modern-b"),
            PathBuf::from("direct-a"),
            PathBuf::from("direct-b"),
            PathBuf::from("office-a"),
            PathBuf::from("other-a"),
        ]
    );
}

#[test]
fn controller_records_successful_lifecycle() {
    let root = temp_root("controller");
    fs::create_dir_all(&root).expect("create root");
    write_file(&root.join("one.txt"), "one");

    let controller = JobController::new();
    let mut session = controller
        .start_scan(&root, ScannerConfig::default())
        .expect("scan starts");
    let candidates = collect_candidates(&mut session, 128).expect("scan succeeds");
    assert_eq!(candidates.len(), 1);
    let snapshot = controller.last_snapshot().expect("snapshot exists");
    assert_eq!(snapshot.state, JobState::Succeeded);
    assert_eq!(snapshot.candidates_discovered, 1);
    assert_eq!(snapshot.candidates_emitted, 1);
    assert!(!controller.cancel());

    fs::remove_dir_all(root).expect("remove fixture");
}

#[cfg(unix)]
#[test]
fn scanner_continues_after_a_broken_directory_entry() {
    use std::os::unix::fs::symlink;

    let root = temp_root("broken-entry");
    fs::create_dir_all(&root).expect("create root");
    write_file(&root.join("keep.txt"), "keep");
    symlink(root.join("missing.txt"), root.join("broken.txt")).expect("create broken link");

    let controller = JobController::new();
    let mut session = controller
        .start_scan(&root, ScannerConfig::default())
        .expect("scan starts");
    let candidates = collect_candidates(&mut session, 128).expect("scan succeeds");
    assert_eq!(candidates.len(), 1);
    assert_eq!(
        controller.last_snapshot().expect("snapshot exists").errors,
        1
    );

    fs::remove_dir_all(root).expect("remove fixture");
}

#[cfg(unix)]
#[test]
fn scanner_follows_file_symlinks_like_the_python_baseline() {
    use std::os::unix::fs::symlink;

    let root = temp_root("symlink");
    fs::create_dir_all(&root).expect("create root");
    write_file(&root.join("target.txt"), "target");
    symlink(root.join("target.txt"), root.join("alias.txt")).expect("create symlink");

    let controller = JobController::new();
    let mut session = controller
        .start_scan(&root, ScannerConfig::default())
        .expect("scan starts");
    let candidates = collect_candidates(&mut session, 128).expect("scan succeeds");
    assert_eq!(candidates.len(), 2);
    assert!(candidates
        .iter()
        .any(|candidate| candidate.path.ends_with("target.txt")));
    assert!(candidates
        .iter()
        .any(|candidate| candidate.path.ends_with("alias.txt")));

    fs::remove_dir_all(root).expect("remove fixture");
}

#[cfg(unix)]
#[test]
fn scanner_applies_size_limit_to_symlink_targets() {
    use std::os::unix::fs::symlink;

    let root = temp_root("symlink-size");
    fs::create_dir_all(&root).expect("create root");
    write_file(&root.join("target.txt"), &"x".repeat(64));
    symlink(root.join("target.txt"), root.join("alias.txt")).expect("create symlink");

    let controller = JobController::new();
    let config = ScannerConfig {
        max_file_size: Some(32),
        ..ScannerConfig::default()
    };
    let mut session = controller.start_scan(&root, config).expect("scan starts");
    let candidates = collect_candidates(&mut session, 128).expect("scan succeeds");
    assert!(candidates.is_empty());

    fs::remove_dir_all(root).expect("remove fixture");
}
