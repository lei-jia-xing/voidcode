//! Supervisor snapshot-diff: what did this exec change? Honest limits in contract §2.4.

use crate::protocol::{ObservedChange, MAX_OBSERVED_CHANGES};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap};
use std::io::Read;
use std::path::{Path, PathBuf};

/// Files at most this big are always hashed; larger files hash only when size/mtime moved.
const ALWAYS_HASH_BYTES: u64 = 1024 * 1024;

#[derive(Debug, Clone)]
struct Entry {
    size: u64,
    mtime_ns: i64,
    hash: Option<String>,
}

pub struct Snapshot {
    entries: BTreeMap<PathBuf, Entry>,
}

fn file_hash(path: &Path) -> Option<String> {
    let mut file = std::fs::File::open(path).ok()?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 65536];
    loop {
        let n = file.read(&mut buf).ok()?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Some(format!("{:x}", hasher.finalize()))
}

fn mtime_ns(meta: &std::fs::Metadata) -> i64 {
    meta.modified()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_nanos().min(u128::from(u64::MAX)) as i64)
        .unwrap_or(0)
}

fn walk(root: &Path, entries: &mut BTreeMap<PathBuf, Entry>, scratch_root: Option<&Path>) {
    let mut stack = vec![root.to_path_buf()];
    // ponytail: iterative walk with a hard entry cap; a wider scan needs a real watcher.
    const CAP: usize = 200_000;
    while let Some(dir) = stack.pop() {
        if entries.len() >= CAP {
            return;
        }
        let read = match std::fs::read_dir(&dir) {
            Ok(r) => r,
            Err(_) => continue,
        };
        for item in read.flatten() {
            if entries.len() >= CAP {
                return;
            }
            let path = item.path();
            // Never descend into the scratch sidecar dir itself, and never follow symlinks here:
            // symlinked content is attributed at its canonical home, not at every alias.
            let meta = match std::fs::symlink_metadata(&path) {
                Ok(m) => m,
                Err(_) => continue,
            };
            if meta.file_type().is_symlink() {
                continue;
            }
            if meta.is_dir() {
                if Some(path.as_path()) == scratch_root {
                    continue;
                }
                stack.push(path);
                continue;
            }
            if !meta.is_file() {
                continue;
            }
            let size = meta.len();
            let mtime = mtime_ns(&meta);
            let hash = if size <= ALWAYS_HASH_BYTES {
                file_hash(&path)
            } else {
                None
            };
            entries.insert(path, Entry { size, mtime_ns: mtime, hash });
        }
    }
}

impl Snapshot {
    pub fn capture(roots: &[PathBuf], scratch_root: Option<&Path>) -> Self {
        let mut entries = BTreeMap::new();
        for root in roots {
            if root.is_dir() {
                walk(root, &mut entries, scratch_root);
            } else if root.is_file() {
                if let Ok(meta) = std::fs::symlink_metadata(root) {
                    if meta.is_file() {
                        let size = meta.len();
                        entries.insert(
                            root.clone(),
                            Entry {
                                size,
                                mtime_ns: mtime_ns(&meta),
                                hash: if size <= ALWAYS_HASH_BYTES { file_hash(root) } else { None },
                            },
                        );
                    }
                }
            }
        }
        Self { entries }
    }

    /// Diff against a post-state capture. `canonicalize` maps snapshot paths to report paths.
    pub fn diff(mut before: Snapshot, after: Snapshot) -> (Vec<ObservedChange>, bool) {
        let mut changes = Vec::new();
        // Deleted + updated: walk the before set.
        let mut gone_hashes: HashMap<String, Vec<PathBuf>> = HashMap::new();
        for (path, entry) in before.entries.iter() {
            match after.entries.get(path) {
                None => {
                    let hash = entry.hash.clone().or_else(|| file_hash_missing(path));
                    if let Some(h) = hash.clone() {
                        gone_hashes.entry(h).or_default().push(path.clone());
                    }
                    changes.push(ObservedChange {
                        path: display(path),
                        operation: "delete".to_string(),
                        old_path: None,
                        content_hash: hash,
                        scratch: false,
                    });
                }
                Some(later) => {
                    if later.size != entry.size || later.mtime_ns != entry.mtime_ns {
                        let hash = later.hash.clone().or_else(|| file_hash(path));
                        // mtime-only touches (same content) are NOT changes.
                        let same = match (&entry.hash, &hash) {
                            (Some(a), Some(b)) => a == b,
                            _ => false,
                        };
                        if !same {
                            changes.push(ObservedChange {
                                path: display(path),
                                operation: "update".to_string(),
                                old_path: None,
                                content_hash: hash,
                                scratch: false,
                            });
                        }
                    }
                }
            }
        }
        // Created: walk the after set for paths absent before.
        let mut created: Vec<(PathBuf, Option<String>)> = vec![];
        for (path, entry) in after.entries.iter() {
            if !before.entries.contains_key(path) {
                let hash = entry.hash.clone().or_else(|| file_hash(path));
                created.push((path.clone(), hash));
            }
        }
        // Move heuristic: (gone, new) pairs with identical content hash become moves.
        // Ambiguous pairs degrade to create+delete, never a wrong move claim.
        let mut consumed_gone: std::collections::HashSet<PathBuf> = std::collections::HashSet::new();
        let mut moves: Vec<ObservedChange> = vec![];
        for (path, hash) in &created {
            if let Some(h) = hash {
                if let Some(candidates) = gone_hashes.get(h) {
                    let free: Vec<&PathBuf> = candidates.iter().filter(|p| !consumed_gone.contains(*p)).collect();
                    if free.len() == 1 {
                        let old = free[0].clone();
                        consumed_gone.insert(old.clone());
                        moves.push(ObservedChange {
                            path: display(path),
                            operation: "move".to_string(),
                            old_path: Some(display(&old)),
                            content_hash: Some(h.clone()),
                            scratch: false,
                        });
                        continue;
                    }
                }
            }
            changes.push(ObservedChange {
                path: display(path),
                operation: "create".to_string(),
                old_path: None,
                content_hash: hash.clone(),
                scratch: false,
            });
        }
        // Drop the delete halves that became moves.
        changes.retain(|c| !(c.operation == "delete" && c.path.is_empty()));
        let mut filtered: Vec<ObservedChange> = Vec::with_capacity(changes.len() + moves.len());
        for c in changes {
            if c.operation == "delete" && consumed_gone.contains(&PathBuf::from(&c.path)) {
                continue;
            }
            // Large-file before-hashes were deferred; a delete of a large file reports no hash.
            filtered.push(c);
        }
        filtered.extend(moves);
        filtered.sort_by(|a, b| a.path.cmp(&b.path));
        // The before map is consumed only for borrow discipline; rebind to silence moves.
        before.entries.clear();
        if filtered.len() > MAX_OBSERVED_CHANGES {
            filtered.truncate(MAX_OBSERVED_CHANGES);
            (filtered, true)
        } else {
            (filtered, false)
        }
    }
}

fn display(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

/// A path that is gone cannot be hashed; large-file deletes report `content_hash: None`.
fn file_hash_missing(_path: &Path) -> Option<String> {
    None
}
