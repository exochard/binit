//! Lets `binit init` skip re-hashing a generation that has not changed since it was verified.
//!
//! After a full verification, `record` stores the inode, size, times, mode and owner of the
//! generation's lock, every locked file and every directory on their paths. `unchanged` compares
//! them with `lstat`. Any write, rename, chmod or chown moves a file's ctime, which only root can
//! set back, so a match means the verified bytes are still in place. Anything else, including a
//! missing, damaged or incompatible stamp, makes the caller verify in full. The stamp is a
//! disposable cache. The checksum lets startup reject a partially persisted record after a crash.

use std::collections::BTreeSet;
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

/// The kernel sets ctime from a clock that ticks every few milliseconds, so a write in the same
/// tick as a stamp can leave it unchanged. As with Git's racily clean index entries, nothing
/// changed this close to the start of verification is stamped; a later start stamps it.
const SETTLED_SECONDS: i64 = 2;

/// Bump whenever `verify_generation` checks more, so stamps from builds with older rules lapse;
/// development builds between releases share one package version.
const VERIFY_RULES: u32 = 1;

/// Ties a stamp to the rules and build that produced it, the generation and its path count.
fn header(id: &str, paths: usize) -> String {
    format!(
        "binit-verified 2 {} rules-{VERIFY_RULES} {id} {paths}",
        env!("CARGO_PKG_VERSION")
    )
}

fn stamp_path(state: &Path, id: &str) -> PathBuf {
    state.join("generations").join(format!("{id}.verified"))
}

/// Removes a generation's stamp, so a failed full verification is never skipped later.
pub fn forget(state: &Path, id: &str) {
    let _ = fs::remove_file(stamp_path(state, id));
}

/// Seconds since the epoch, read before verification starts; `record` uses it as the cut-off.
pub fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_secs() as i64)
}

/// Relative paths a stamp covers: the lock, the locked files and each directory above them.
fn covered<'a>(files: impl Iterator<Item = &'a String>) -> BTreeSet<String> {
    let mut paths = BTreeSet::from([".".to_owned(), "lock.toml".to_owned()]);
    for file in files {
        let mut prefix = String::new();
        for component in file.split('/') {
            if !prefix.is_empty() {
                prefix.push('/');
            }
            prefix.push_str(component);
            paths.insert(prefix.clone());
        }
    }
    if paths.iter().any(|path| path.starts_with("bin/")) {
        paths.insert("bin".to_owned());
    }
    paths
}

fn metadata(root: &Path, relative: &str) -> Option<fs::Metadata> {
    // `root/.` would follow a symlinked generation directory; lstat the directory itself.
    let path = if relative == "." {
        root.to_path_buf()
    } else {
        root.join(relative)
    };
    let meta = fs::symlink_metadata(path).ok()?;
    (!meta.file_type().is_symlink()).then_some(meta)
}

fn line(meta: &fs::Metadata, relative: &str) -> String {
    format!(
        "{} {} {} {} {} {} {} {} {} {relative}",
        meta.dev(),
        meta.ino(),
        meta.size(),
        meta.mtime(),
        meta.mtime_nsec(),
        meta.ctime(),
        meta.ctime_nsec(),
        meta.mode(),
        meta.uid()
    )
}

/// Writes the stamp for a generation that has just passed full verification, unless one of its
/// paths changed after `started - SETTLED_SECONDS`, which includes anything changed while the
/// verification ran.
pub fn record<'a>(
    state: &Path,
    id: &str,
    files: impl Iterator<Item = &'a String>,
    started: i64,
) -> crate::Result<()> {
    let root = state.join("generations").join(id);
    let paths = covered(files);
    let mut text = header(id, paths.len()) + "\n";
    for relative in paths {
        let meta = metadata(&root, &relative).ok_or("generation changed during verification")?;
        if meta.ctime() > started - SETTLED_SECONDS || meta.mtime() > started - SETTLED_SECONDS {
            return Ok(());
        }
        text.push_str(&line(&meta, &relative));
        text.push('\n');
    }
    let bytes = format!("{}\n{text}", crate::hash(text.as_bytes()));
    crate::write_atomic(&stamp_path(state, id), bytes.as_bytes(), 0o600, false)
}

/// Whether every path in the generation's stamp still has the recorded metadata.
pub fn unchanged(state: &Path, id: &str) -> bool {
    let Ok(text) = crate::read_regular(&stamp_path(state, id)) else {
        return false;
    };
    let Ok(text) = String::from_utf8(text) else {
        return false;
    };
    let Some((checksum, text)) = text.split_once('\n') else {
        return false;
    };
    if crate::hash(text.as_bytes()) != checksum {
        return false;
    }
    let mut lines = text.lines();
    let Some(first) = lines.next() else {
        return false;
    };
    let count = text.lines().count() - 1;
    if first != header(id, count) {
        return false;
    }
    let root = state.join("generations").join(id);
    let (mut directory, mut lock) = (false, false);
    for recorded in lines {
        let Some(relative) = recorded.splitn(10, ' ').nth(9) else {
            return false;
        };
        match metadata(&root, relative) {
            Some(meta) if line(&meta, relative) == recorded => {}
            _ => return false,
        }
        directory |= relative == ".";
        lock |= relative == "lock.toml";
    }
    directory && lock
}

#[cfg(test)]
mod tests {
    use super::covered;

    #[test]
    fn covers_the_lock_files_and_every_parent_directory() {
        let files = [
            "init.bash".to_owned(),
            "plugins/a/x.bash".to_owned(),
            "bin/tool".to_owned(),
        ];
        let paths: Vec<_> = covered(files.iter()).into_iter().collect();
        assert_eq!(
            paths,
            [
                ".",
                "bin",
                "bin/tool",
                "init.bash",
                "lock.toml",
                "plugins",
                "plugins/a",
                "plugins/a/x.bash"
            ]
        );
    }
}
