//! Lets `binit init` skip re-hashing a generation that has not changed since it was verified.
//!
//! After a full verification, `record` stores the inode, size, times, mode and owner of the
//! generation's lock, every locked file and every directory on their paths. `unchanged` compares
//! them with `lstat`. Any write, rename, chmod or chown moves a file's ctime, which only root can
//! set back, so a match means the verified bytes are still in place. Anything else, including a
//! missing, damaged or incompatible stamp, makes the caller verify in full. The stamp is a
//! disposable cache. The checksum lets startup reject a partially persisted record after a crash.

use std::collections::BTreeSet;
use std::ffi::CString;
use std::fmt::Write;
use std::fs;
use std::mem::MaybeUninit;
use std::os::fd::AsRawFd;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Component, Path, PathBuf};
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
    let meta = if relative == "." {
        fs::symlink_metadata(root)
    } else {
        fs::symlink_metadata(root.join(relative))
    }
    .ok()?;
    (!meta.file_type().is_symlink()).then_some(meta)
}

#[derive(PartialEq, Eq)]
struct Fingerprint {
    device: u64,
    inode: u64,
    size: u64,
    modified: i64,
    modified_ns: i64,
    changed: i64,
    changed_ns: i64,
    mode: u32,
    owner: u32,
}

impl From<&fs::Metadata> for Fingerprint {
    fn from(meta: &fs::Metadata) -> Self {
        Self {
            device: meta.dev(),
            inode: meta.ino(),
            size: meta.size(),
            modified: meta.mtime(),
            modified_ns: meta.mtime_nsec(),
            changed: meta.ctime(),
            changed_ns: meta.ctime_nsec(),
            mode: meta.mode(),
            owner: meta.uid(),
        }
    }
}

fn metadata_at(root: &fs::File, relative: &str) -> Option<Fingerprint> {
    if relative != "."
        && Path::new(relative)
            .components()
            .any(|part| !matches!(part, Component::Normal(_)))
    {
        return None;
    }
    let relative = CString::new(relative).ok()?;
    let mut meta = MaybeUninit::<libc::stat>::uninit();
    // `root` stays open while the kernel resolves the relative path and fills the stat.
    let status = unsafe {
        libc::fstatat(
            root.as_raw_fd(),
            relative.as_ptr(),
            meta.as_mut_ptr(),
            libc::AT_SYMLINK_NOFOLLOW,
        )
    };
    if status != 0 {
        return None;
    }
    let meta = unsafe { meta.assume_init() };
    if meta.st_mode & libc::S_IFMT == libc::S_IFLNK {
        return None;
    }
    Some(Fingerprint {
        device: meta.st_dev,
        inode: meta.st_ino,
        size: meta.st_size.try_into().ok()?,
        modified: meta.st_mtime,
        modified_ns: meta.st_mtime_nsec,
        changed: meta.st_ctime,
        changed_ns: meta.st_ctime_nsec,
        mode: meta.st_mode,
        owner: meta.st_uid,
    })
}

fn write_line(output: &mut String, meta: &Fingerprint, relative: &str) {
    write!(
        output,
        "{} {} {} {} {} {} {} {} {} {relative}",
        meta.device,
        meta.inode,
        meta.size,
        meta.modified,
        meta.modified_ns,
        meta.changed,
        meta.changed_ns,
        meta.mode,
        meta.owner,
    )
    .expect("format metadata into a string");
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
        write_line(&mut text, &Fingerprint::from(&meta), &relative);
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
    let Ok(root_dir) = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_PATH | libc::O_DIRECTORY | libc::O_NOFOLLOW)
        .open(&root)
    else {
        return false;
    };
    let (mut directory, mut lock) = (None, false);
    let mut observed = String::new();
    for recorded in lines {
        let Some(relative) = recorded.splitn(10, ' ').nth(9) else {
            return false;
        };
        let Some(meta) = metadata_at(&root_dir, relative) else {
            return false;
        };
        observed.clear();
        write_line(&mut observed, &meta, relative);
        if observed != recorded {
            return false;
        }
        if relative == "." {
            directory = Some(meta);
        }
        lock |= relative == "lock.toml";
    }
    // The path used by the caller must still resolve to the directory we checked.
    lock && directory.is_some_and(|anchored| {
        metadata(&root, ".").is_some_and(|named| Fingerprint::from(&named) == anchored)
    })
}

#[cfg(test)]
mod tests {
    use super::{Fingerprint, covered, metadata, metadata_at};
    use std::fs;
    use std::os::unix::fs::{OpenOptionsExt, symlink};

    #[test]
    fn descriptor_metadata_matches_path_metadata_and_rejects_links() {
        let work = crate::private_tempdir();
        let root = work.path().join("generation");
        fs::create_dir(&root).unwrap();
        fs::write(root.join("entry"), b"fixture").unwrap();
        symlink("entry", root.join("linked")).unwrap();
        let anchored = fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_PATH | libc::O_DIRECTORY | libc::O_NOFOLLOW)
            .open(&root)
            .unwrap();
        for relative in [".", "entry"] {
            assert!(
                metadata_at(&anchored, relative)
                    == metadata(&root, relative).as_ref().map(Fingerprint::from)
            );
        }
        let absolute = root.join("entry");
        for relative in ["linked", "../generation/entry", absolute.to_str().unwrap()] {
            assert!(metadata_at(&anchored, relative).is_none());
        }
    }

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
