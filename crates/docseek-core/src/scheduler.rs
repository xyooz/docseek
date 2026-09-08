use std::cmp::Ordering;
use std::collections::BinaryHeap;

use crate::Candidate;

#[derive(Debug)]
struct QueuedCandidate {
    sequence: u64,
    candidate: Candidate,
}

impl Eq for QueuedCandidate {}

impl PartialEq for QueuedCandidate {
    fn eq(&self, other: &Self) -> bool {
        self.sequence == other.sequence
    }
}

impl Ord for QueuedCandidate {
    fn cmp(&self, other: &Self) -> Ordering {
        // BinaryHeap is a max-heap. Reverse both fields so the lowest lane
        // and earliest discovery sequence are returned first.
        other
            .candidate
            .priority
            .cmp(&self.candidate.priority)
            .then_with(|| other.sequence.cmp(&self.sequence))
    }
}

impl PartialOrd for QueuedCandidate {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

/// Stable, priority-aware candidate queue.
#[derive(Debug, Default)]
pub struct CandidateScheduler {
    queue: BinaryHeap<QueuedCandidate>,
    next_sequence: u64,
}

impl CandidateScheduler {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn push(&mut self, candidate: Candidate) {
        let sequence = self.next_sequence;
        self.next_sequence = self.next_sequence.saturating_add(1);
        self.queue.push(QueuedCandidate {
            sequence,
            candidate,
        });
    }

    pub fn extend<I>(&mut self, candidates: I)
    where
        I: IntoIterator<Item = Candidate>,
    {
        for candidate in candidates {
            self.push(candidate);
        }
    }

    pub fn pop(&mut self) -> Option<Candidate> {
        self.queue.pop().map(|queued| queued.candidate)
    }

    pub fn len(&self) -> usize {
        self.queue.len()
    }

    pub fn is_empty(&self) -> bool {
        self.queue.is_empty()
    }

    pub fn into_sorted_vec(mut self) -> Vec<Candidate> {
        let mut candidates = Vec::with_capacity(self.len());
        while let Some(candidate) = self.pop() {
            candidates.push(candidate);
        }
        candidates
    }

    pub fn from_candidates<I>(candidates: I) -> Self
    where
        I: IntoIterator<Item = Candidate>,
    {
        let mut scheduler = Self::new();
        scheduler.extend(candidates);
        scheduler
    }
}
