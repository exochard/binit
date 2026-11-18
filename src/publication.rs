use crate::{Result, private_dir};
use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};

fn write_new_file(path: &Path, bytes: &[u8], mode: u32) -> Result<()> {
    private_dir(path.parent().ok_or("missing package parent")?)?;
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(mode)
        .open(path)?;
    file.write_all(bytes)?;
    file.set_permissions(fs::Permissions::from_mode(mode))?;
    file.sync_all()?;
    Ok(())
}

// The caller supplies a fresh stage below its canonical, checked generation
// directory and publishes it only after this function succeeds.
pub(super) fn write_staging(
    staging: &Path,
    packages: BTreeMap<String, Vec<u8>>,
    lock: &[u8],
) -> Result<()> {
    let entries: Vec<_> = packages.into_iter().collect();
    for (name, _) in &entries {
        if name.is_empty()
            || name
                .split('/')
                .next()
                .is_some_and(|head| head.eq_ignore_ascii_case("lock.toml"))
            || !Path::new(name)
                .components()
                .all(|part| matches!(part, Component::Normal(_)))
            || Path::new(name)
                .components()
                .collect::<PathBuf>()
                .as_os_str()
                != Path::new(name).as_os_str()
        {
            return Err("invalid generation package path".into());
        }
    }
    private_dir(staging)?;
    // Create and check shared parents before writers can race to create them.
    for (name, _) in &entries {
        let mut parent = staging.to_path_buf();
        for part in Path::new(name).parent().unwrap().components() {
            parent.push(part.as_os_str());
            private_dir(&parent)?;
        }
    }

    write_entries(staging, &entries, WRITERS)?;
    write_new_file(&staging.join("lock.toml"), lock, 0o600)
}

/// Writers syncing at once. Concurrent fsyncs share filesystem commits, so this is most of
/// prepare's speed; it is a ceiling, independent of catalog size and CPU count.
const WRITERS: usize = 32;

/// Writes and syncs every entry, and returns the first failure only after every write has
/// finished. Up to `writers - 1` threads join the calling thread; when the system refuses a
/// thread, the ones already running and the calling thread do the rest.
fn write_entries(staging: &Path, entries: &[(String, Vec<u8>)], writers: usize) -> Result<()> {
    let next = AtomicUsize::new(0);
    let work = || -> std::result::Result<(), String> {
        let mut outcome = Ok(());
        while let Some((name, bytes)) = entries.get(next.fetch_add(1, Ordering::Relaxed)) {
            let mode = if name.starts_with("bin/") {
                0o700
            } else {
                0o600
            };
            if let Err(error) = write_new_file(&staging.join(name), bytes, mode) {
                outcome = outcome.and(Err(error.to_string()));
            }
        }
        outcome
    };
    let result = std::thread::scope(|scope| {
        let handles: Vec<_> = (1..writers.min(entries.len()))
            .map_while(|_| std::thread::Builder::new().spawn_scoped(scope, work).ok())
            .collect();
        let mut outcome = work();
        for handle in handles {
            let joined = handle
                .join()
                .unwrap_or_else(|_| Err("generation writer panicked".to_owned()));
            if let Err(error) = joined {
                outcome = outcome.and(Err(error));
            }
        }
        outcome
    });
    result.map_err(|error| error.into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;
    use std::fs;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn writes_every_entry_with_any_number_of_writers() {
        let entries: Vec<_> = (0..70)
            .map(|index| (format!("f{index}"), index.to_string().into_bytes()))
            .collect();
        // One writer is the calling thread alone, as when the system refuses every thread.
        for writers in [1, 3, WRITERS, 200] {
            // write_entries requires a private staging directory, whatever the caller's umask.
            let root = crate::private_tempdir();
            write_entries(root.path(), &entries, writers).unwrap();
            for (name, bytes) in &entries {
                assert_eq!(
                    &fs::read(root.path().join(name)).unwrap(),
                    bytes,
                    "{writers}"
                );
            }
        }
        // A failed write fails the whole set, whichever thread meets it.
        let root = tempfile::tempdir().unwrap();
        fs::write(root.path().join("f40"), b"taken").unwrap();
        assert!(write_entries(root.path(), &entries, 4).is_err());
    }

    #[test]
    fn writes_nested_packages_with_private_directories_and_executable_modes() {
        let root = tempfile::tempdir().unwrap();
        let stage = root.path().join("stage");
        let packages = BTreeMap::from([
            ("runtime.bash".into(), b"runtime".to_vec()),
            ("plugins/sample/lib/entry.bash".into(), b"plugin".to_vec()),
            ("bin/tool".into(), b"program".to_vec()),
        ]);
        write_staging(&stage, packages.clone(), b"metadata").unwrap();
        for (name, content) in packages {
            assert_eq!(fs::read(stage.join(&name)).unwrap(), content);
            let mode = fs::metadata(stage.join(&name))
                .unwrap()
                .permissions()
                .mode()
                & 0o777;
            assert_eq!(
                mode,
                if name.starts_with("bin/") {
                    0o700
                } else {
                    0o600
                }
            );
        }
        for name in ["", "plugins", "plugins/sample", "plugins/sample/lib", "bin"] {
            assert_eq!(
                fs::metadata(stage.join(name)).unwrap().permissions().mode() & 0o777,
                0o700
            );
        }
        assert_eq!(fs::read(stage.join("lock.toml")).unwrap(), b"metadata");
    }

    #[test]
    fn rejects_path_escape_before_writing_packages() {
        let root = tempfile::tempdir().unwrap();
        let stage = root.path().join("stage");
        let packages = BTreeMap::from([
            ("ok".into(), b"safe".to_vec()),
            ("z/../../escape".into(), b"escape".to_vec()),
        ]);
        assert!(write_staging(&stage, packages, b"metadata").is_err());
        assert!(!stage.join("ok").exists());
        assert!(!root.path().join("escape").exists());
    }

    #[test]
    fn rejects_aliased_names_and_reserved_metadata_before_writing() {
        for name in [
            "plugins//x",
            "plugins/./x",
            "lock.toml",
            "LOCK.TOML",
            "lock.toml/child",
            "LOCK.TOML/child",
        ] {
            let root = tempfile::tempdir().unwrap();
            let packages = BTreeMap::from([
                ("ok".into(), b"safe".to_vec()),
                (name.into(), b"invalid destination".to_vec()),
            ]);
            assert!(
                write_staging(root.path(), packages, b"metadata").is_err(),
                "{name}"
            );
            assert!(!root.path().join("ok").exists());
        }
    }

    #[test]
    fn refuses_existing_destination_without_replacing_its_data() {
        let root = tempfile::tempdir().unwrap();
        fs::write(root.path().join("existing"), b"keep").unwrap();
        let packages = BTreeMap::from([("existing".into(), b"replace".to_vec())]);
        assert!(write_staging(root.path(), packages, b"metadata").is_err());
        assert_eq!(fs::read(root.path().join("existing")).unwrap(), b"keep");
    }

    #[test]
    fn refuses_symlinked_package_parent() {
        let root = tempfile::tempdir().unwrap();
        let stage = root.path().join("stage");
        private_dir(&stage).unwrap();
        let outside = root.path().join("outside");
        fs::create_dir(&outside).unwrap();
        std::os::unix::fs::symlink(&outside, stage.join("plugins")).unwrap();
        let packages = BTreeMap::from([("plugins/entry".into(), b"keep inside".to_vec())]);
        assert!(write_staging(&stage, packages, b"metadata").is_err());
        assert!(!outside.join("entry").exists());
    }

    #[test]
    fn metadata_install_is_exclusive_and_does_not_follow_a_symlink() {
        let root = tempfile::tempdir().unwrap();
        let stage = root.path().join("stage");
        private_dir(&stage).unwrap();
        let outside = root.path().join("outside");
        fs::write(&outside, b"keep").unwrap();
        std::os::unix::fs::symlink(&outside, stage.join("lock.toml")).unwrap();
        let packages = BTreeMap::from([("entry".into(), b"package".to_vec())]);
        assert!(write_staging(&stage, packages, b"metadata").is_err());
        assert_eq!(fs::read(&outside).unwrap(), b"keep");
        assert_eq!(fs::read(stage.join("entry")).unwrap(), b"package");
    }

    #[test]
    fn returns_writer_failure_after_other_started_writers_finish() {
        let root = crate::private_tempdir();
        fs::create_dir(root.path().join("blocked")).unwrap();
        let packages = BTreeMap::from([
            ("blocked".into(), b"cannot replace directory".to_vec()),
            ("good-1".into(), b"one".to_vec()),
            ("good-2".into(), b"two".to_vec()),
            ("good-3".into(), b"three".to_vec()),
        ]);
        assert!(write_staging(root.path(), packages, b"metadata").is_err());
        for name in ["good-1", "good-2", "good-3"] {
            assert!(root.path().join(name).is_file());
        }
        assert!(root.path().join("blocked").is_dir());
        assert!(!root.path().join("lock.toml").exists());
    }
}
