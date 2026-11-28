use super::{Result, hash, process::SignalGuard, quote, unique, valid_digest};
use base64::{Engine, engine::general_purpose::STANDARD};
use serde::{Deserialize, Serialize};
use std::ffi::OsString;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};

const START: &str = "# >>> binit >>>\n";
const END: &str = "# <<< binit <<<\n";
const EXECUTABLE_MODE: u32 = 0o755;

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Receipt {
    schema: u32,
    binary: PathBuf,
    sha256: String,
    mode: u32,
    #[serde(deserialize_with = "explicit_option")]
    rc_file: Option<PathBuf>,
    #[serde(deserialize_with = "explicit_option")]
    rc_block: Option<String>,
}

fn explicit_option<'de, T: Deserialize<'de>, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> std::result::Result<Option<T>, D::Error> {
    Option::deserialize(deserializer)
}

impl Receipt {
    fn block(&self, binary: &Path) -> Result<Vec<u8>> {
        if self.schema != 1
            || self.binary != binary
            || self.mode != EXECUTABLE_MODE
            || !valid_digest(&self.sha256)
        {
            return Err("invalid installation receipt".into());
        }
        match (&self.rc_file, &self.rc_block) {
            (None, None) => Ok(Vec::new()),
            (Some(path), Some(encoded)) if path.is_absolute() => {
                let block = STANDARD.decode(encoded)?;
                let marker = block.strip_prefix(b"\n").unwrap_or(&block);
                if !marker.starts_with(START.as_bytes()) || !marker.ends_with(END.as_bytes()) {
                    return Err("invalid installation receipt".into());
                }
                Ok(block)
            }
            _ => Err("invalid installation receipt".into()),
        }
    }
}

#[derive(PartialEq, Eq)]
struct Contents {
    bytes: Vec<u8>,
    mode: u32,
}

struct Target {
    path: PathBuf,
    parent: File,
}

fn uid() -> u32 {
    // Linux returns the effective UID without taking any arguments.
    unsafe { libc::geteuid() }
}

fn descriptor_path(dir: &File) -> PathBuf {
    PathBuf::from(format!("/proc/self/fd/{}", dir.as_raw_fd()))
}

fn directory(path: &Path, create: bool, last_mode: u32) -> Result<File> {
    let open = |path: &Path| {
        OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW)
            .open(path)
    };
    let mut dir = open(Path::new("/"))?;
    let mut components = path
        .components()
        .filter_map(|part| match part {
            Component::Normal(name) => Some(name),
            _ => None,
        })
        .peekable();
    while let Some(name) = components.next() {
        let at = descriptor_path(&dir).join(name);
        dir = match open(&at) {
            Err(error) if error.kind() == io::ErrorKind::NotFound && create => {
                let mode = if components.peek().is_some() {
                    0o777
                } else {
                    last_mode
                };
                match fs::DirBuilder::new().mode(mode).create(&at) {
                    Ok(()) => {}
                    Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
                    Err(error) => return Err(error.into()),
                }
                open(&at)?
            }
            result => result.map_err(|error| format!("{}: {error}", path.display()))?,
        };
        let owner = dir.metadata()?.uid();
        if owner != 0 && owner != uid() {
            return Err(format!("foreign owner in path: {}", path.display()).into());
        }
    }
    Ok(dir)
}

impl Target {
    fn open(path: PathBuf, create: bool, parent_mode: u32) -> Result<Self> {
        let parent = directory(
            path.parent().ok_or("expected a named file")?,
            create,
            parent_mode,
        )?;
        Ok(Self { path, parent })
    }

    fn at(&self) -> PathBuf {
        descriptor_path(&self.parent).join(self.path.file_name().expect("named file"))
    }

    fn read(&self) -> Result<Option<Contents>> {
        let mut file = match OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(self.at())
        {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(format!("{}: {error}", self.path.display()).into()),
        };
        let metadata = file.metadata()?;
        if !metadata.is_file() || metadata.uid() != uid() {
            return Err(format!("expected an owned regular file: {}", self.path.display()).into());
        }
        let mut bytes = Vec::new();
        file.read_to_end(&mut bytes)?;
        Ok(Some(Contents {
            bytes,
            mode: metadata.mode() & 0o7777,
        }))
    }

    fn lock(&self) -> Result<()> {
        // The directory descriptor retains the advisory lock until it closes.
        if unsafe { libc::flock(self.parent.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err(format!(
                "could not lock {}: {}",
                self.path.display(),
                io::Error::last_os_error()
            )
            .into());
        }
        Ok(())
    }

    fn lock_unless_same_parent(&self, other: &Self) -> Result<()> {
        let this = self.parent.metadata()?;
        let other = other.parent.metadata()?;
        if (this.dev(), this.ino()) != (other.dev(), other.ino()) {
            self.lock()?;
        }
        Ok(())
    }

    fn backup(&self, contents: &Contents) -> Result<()> {
        let mut name = self.path.file_name().expect("named file").to_os_string();
        name.push(format!(".binit-{}.bak", unique()));
        self.stage(contents)?
            .persist_noclobber(descriptor_path(&self.parent).join(name))?;
        Ok(())
    }

    fn stage(&self, contents: &Contents) -> Result<tempfile::NamedTempFile> {
        let mut temporary = tempfile::NamedTempFile::new_in(descriptor_path(&self.parent))?;
        temporary.write_all(&contents.bytes)?;
        temporary
            .as_file()
            .set_permissions(fs::Permissions::from_mode(contents.mode))?;
        temporary.as_file().sync_all()?;
        Ok(temporary)
    }

    fn replace(&self, expected: Option<&Contents>, contents: Option<&Contents>) -> Result<()> {
        let staged = contents.map(|contents| self.stage(contents)).transpose()?;
        self.commit(expected, staged)
    }

    fn commit(
        &self,
        expected: Option<&Contents>,
        staged: Option<tempfile::NamedTempFile>,
    ) -> Result<()> {
        // Check again after syncing the replacement, while the real file is still intact.
        if self.read()?.as_ref() != expected {
            return Err(
                format!("file changed during installation: {}", self.path.display()).into(),
            );
        }
        if let Some(staged) = staged {
            if expected.is_none() {
                staged.persist_noclobber(self.at())?;
            } else {
                staged.persist(self.at())?;
            }
        } else {
            fs::remove_file(self.at())?;
        }
        Ok(())
    }
}

struct Change<'a> {
    target: &'a Target,
    before: Option<Contents>,
    after: Option<Contents>,
}

fn transact(
    changes: &[Change<'_>],
    replace: impl Fn(&Target, Option<&Contents>, Option<&Contents>) -> Result<()>,
    check: impl Fn() -> Result<()>,
) -> Result<()> {
    let mut applied = 0;
    let result = (|| {
        for change in changes {
            check()?;
            replace(change.target, change.before.as_ref(), change.after.as_ref())?;
            applied += 1;
        }
        check()
    })();
    if let Err(error) = result {
        let mut errors = vec![error.to_string()];
        for prior in changes[..applied].iter().rev() {
            let restore = replace(prior.target, prior.after.as_ref(), prior.before.as_ref());
            if let Err(error) = restore {
                errors.push(format!(
                    "could not restore {}: {error}",
                    prior.target.path.display()
                ));
            }
        }
        return Err(errors.join("\n").into());
    }
    Ok(())
}

fn absolute(path: &Path) -> Result<PathBuf> {
    let full = if let Ok(suffix) = path.strip_prefix("~") {
        home()?.join(suffix)
    } else if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()?.join(path)
    };
    let mut normalized = PathBuf::from("/");
    for part in full.components() {
        match part {
            Component::Normal(name) => normalized.push(name),
            Component::ParentDir => {
                normalized.pop();
            }
            _ => {}
        }
    }
    Ok(normalized)
}

fn home() -> Result<PathBuf> {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .ok_or_else(|| "HOME must name an absolute directory".into())
}

fn block_at(content: &[u8], block: &[u8]) -> Result<usize> {
    let mut matches = content
        .windows(block.len())
        .enumerate()
        .filter(|(_, window)| *window == block);
    match (matches.next(), matches.next()) {
        (Some((position, _)), None) => Ok(position),
        _ => Err("installed startup block changed".into()),
    }
}

fn startup_contents(
    old: &[u8],
    block: &[u8],
    startup: &str,
    uninstall: bool,
) -> Result<(Vec<u8>, Vec<u8>)> {
    let (before, after) = if block.is_empty() {
        if [START, END].iter().any(|marker| {
            old.windows(marker.len())
                .any(|part| part == marker.as_bytes())
        }) {
            return Err("unrecorded startup block".into());
        }
        (old, &[][..])
    } else {
        let offset = block_at(old, block)?;
        (&old[..offset], &old[offset + block.len()..])
    };
    let mut replacement = Vec::new();
    if uninstall {
        if block.starts_with(b"\n") && !after.is_empty() {
            replacement.push(b'\n');
        }
    } else {
        if block.starts_with(b"\n")
            || (block.is_empty() && !before.is_empty() && !before.ends_with(b"\n"))
        {
            replacement.push(b'\n');
        }
        write!(replacement, "{START}{startup}\n{END}")?;
    }
    Ok(([before, &replacement, after].concat(), replacement))
}

pub(super) fn command(uninstall: bool, args: &[OsString]) -> Result<()> {
    let mut prefix = None;
    let mut source = None;
    let mut rc = None;
    let mut remaining = args.iter();
    while let Some(flag) = remaining.next() {
        let destination = match flag.to_str() {
            Some("--prefix") => &mut prefix,
            Some("--binary") if !uninstall => &mut source,
            Some("--rc-file") if !uninstall => &mut rc,
            _ => return Err(format!("unknown install option: {}", flag.to_string_lossy()).into()),
        };
        let value = remaining
            .next()
            .filter(|value| !value.is_empty())
            .ok_or("missing install path")?;
        if destination.replace(absolute(Path::new(value))?).is_some() {
            return Err(format!("duplicate install option: {}", flag.to_string_lossy()).into());
        }
    }
    let prefix = prefix.map_or_else(|| absolute(Path::new("~/.local")), Ok)?;
    let source = if uninstall {
        None
    } else {
        Some(source.map_or_else(std::env::current_exe, Ok)?)
    };
    let _signals = SignalGuard::install(true)?;
    install(&prefix, source.as_deref(), rc, uninstall)
}

fn install(
    prefix: &Path,
    source: Option<&Path>,
    rc_path: Option<PathBuf>,
    uninstall: bool,
) -> Result<()> {
    let incoming = source
        .map(|path| -> Result<Contents> {
            let contents = Target::open(path.to_path_buf(), false, 0)?
                .read()?
                .ok_or("source executable is missing")?;
            if contents.bytes.is_empty() || contents.mode & 0o111 == 0 {
                return Err("source executable is empty or lacks execute permission".into());
            }
            Ok(Contents {
                mode: EXECUTABLE_MODE,
                ..contents
            })
        })
        .transpose()?;
    let binary = Target::open(prefix.join("bin/binit"), !uninstall, 0o777)?;
    let receipt_file = Target::open(prefix.join("share/binit/install.json"), !uninstall, 0o700)?;
    let parent_meta = receipt_file.parent.metadata()?;
    if parent_meta.uid() != uid() {
        return Err("installation receipt directory must be owned by the current user".into());
    }
    receipt_file.lock()?;
    let original_receipt = receipt_file.read()?;
    let previous = binary.read()?;
    let receipt: Option<Receipt> = original_receipt
        .as_ref()
        .map(|data| serde_json::from_slice(&data.bytes))
        .transpose()?;
    let block = match (&receipt, &previous) {
        (Some(receipt), Some(previous)) => {
            let block = receipt.block(&binary.path)?;
            if previous.mode != receipt.mode || hash(&previous.bytes) != receipt.sha256 {
                return Err(
                    format!("installed executable changed: {}", binary.path.display()).into(),
                );
            }
            block
        }
        (None, None) if !uninstall => Vec::new(),
        (None, None) => return Err("no installation receipt".into()),
        (None, Some(_)) => return Err("existing executable is not recorded".into()),
        (Some(_), None) => return Err("recorded executable is missing".into()),
    };
    let name = binary
        .path
        .to_str()
        .ok_or("installation path must be UTF-8")?;
    let command = if name
        .bytes()
        .all(|byte| byte.is_ascii_alphanumeric() || b"_@%+=:,./-".contains(&byte))
    {
        name.to_owned()
    } else {
        quote(name)
    };
    let startup = format!("eval \"$({command} init)\"");
    let old_rc = receipt
        .as_ref()
        .and_then(|receipt| receipt.rc_file.as_ref())
        .map(|p| absolute(p))
        .transpose()?;
    if let (Some(old), Some(new)) = (&old_rc, &rc_path)
        && old != new
    {
        return Err("installation already owns a different startup file".into());
    }
    let rc = rc_path
        .or(old_rc)
        .map(|path| {
            if path == binary.path || path == receipt_file.path || source == Some(path.as_path()) {
                return Err("startup file must differ from the executable and receipt".into());
            }
            let target = Target::open(path, !uninstall, 0o777)?;
            target.lock_unless_same_parent(&receipt_file)?;
            Ok::<_, Box<dyn std::error::Error>>(target)
        })
        .transpose()?;
    let mut changes = Vec::new();
    let mut receipt_block = None;
    if let Some(target) = &rc {
        let before = target.read()?;
        let old = before
            .as_ref()
            .map_or(&[][..], |data| data.bytes.as_slice());
        let (bytes, replacement) = startup_contents(old, &block, &startup, uninstall)?;
        receipt_block = Some(STANDARD.encode(replacement));
        if bytes != old {
            if let Some(data) = &before
                && !uninstall
            {
                target.backup(data)?;
            }
            let mode = before.as_ref().map_or(0o600, |data| data.mode);
            changes.push(Change {
                target,
                before,
                after: Some(Contents { bytes, mode }),
            });
        }
    }

    if !uninstall && parent_meta.mode() & 0o077 != 0 {
        receipt_file
            .parent
            .set_permissions(fs::Permissions::from_mode(parent_meta.mode() & 0o700))?;
    }
    if previous != incoming {
        if let Some(data) = &previous
            && !uninstall
        {
            binary.backup(data)?;
        }
        let updated = incoming
            .as_ref()
            .map(|data| -> Result<Contents> {
                let mut bytes = serde_json::to_vec_pretty(&Receipt {
                    schema: 1,
                    binary: binary.path.clone(),
                    sha256: hash(&data.bytes),
                    mode: data.mode,
                    rc_file: rc.as_ref().map(|target| target.path.clone()),
                    rc_block: receipt_block.clone(),
                })?;
                bytes.push(b'\n');
                Ok(Contents { bytes, mode: 0o600 })
            })
            .transpose()?;
        changes.push(Change {
            target: &binary,
            before: previous,
            after: incoming,
        });
        changes.push(Change {
            target: &receipt_file,
            before: original_receipt,
            after: updated,
        });
    } else if !changes.is_empty() {
        let mut receipt = receipt.ok_or("missing installation receipt")?;
        receipt.rc_file = rc.as_ref().map(|target| target.path.clone());
        receipt.rc_block = receipt_block;
        let mut bytes = serde_json::to_vec_pretty(&receipt)?;
        bytes.push(b'\n');
        changes.push(Change {
            target: &receipt_file,
            before: original_receipt,
            after: Some(Contents { bytes, mode: 0o600 }),
        });
    }
    transact(&changes, Target::replace, SignalGuard::check)?;
    println!(
        "{}",
        if uninstall {
            "Binit uninstalled"
        } else {
            &startup
        }
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn contents(bytes: &[u8]) -> Contents {
        Contents {
            bytes: bytes.to_vec(),
            mode: 0o600,
        }
    }

    #[test]
    fn edit_after_staging_is_preserved() {
        let root = crate::private_tempdir();
        let target = Target::open(root.path().join("rc"), false, 0).unwrap();
        let original = contents(b"original");
        target.replace(None, Some(&original)).unwrap();
        let staged = target.stage(&contents(b"replacement")).unwrap();
        fs::write(&target.path, b"user edit").unwrap();
        assert!(target.commit(Some(&original), Some(staged)).is_err());
        assert_eq!(fs::read(&target.path).unwrap(), b"user edit");
    }

    #[test]
    fn failed_receipt_write_restores_existing_files_and_removes_new_files() {
        let root = crate::private_tempdir();
        let rc = Target::open(root.path().join("rc"), false, 0).unwrap();
        let binary = Target::open(root.path().join("binary"), false, 0).unwrap();
        let receipt = Target::open(root.path().join("receipt"), false, 0).unwrap();
        rc.replace(None, Some(&contents(b"old rc"))).unwrap();
        let changes = [
            Change {
                target: &rc,
                before: rc.read().unwrap(),
                after: Some(contents(b"new rc")),
            },
            Change {
                target: &binary,
                before: None,
                after: Some(contents(b"new binary")),
            },
            Change {
                target: &receipt,
                before: None,
                after: Some(contents(b"receipt")),
            },
        ];
        let error = transact(
            &changes,
            |target, before, after| {
                if target.path == receipt.path {
                    return Err("fixture receipt write failure".into());
                }
                target.replace(before, after)
            },
            || Ok(()),
        )
        .unwrap_err();
        assert!(error.to_string().contains("fixture receipt write failure"));
        assert_eq!(rc.read().unwrap().unwrap().bytes, b"old rc");
        assert!(binary.read().unwrap().is_none());
        assert!(receipt.read().unwrap().is_none());
    }

    #[test]
    fn interrupted_commit_restores_files_already_written() {
        let root = crate::private_tempdir();
        let target = Target::open(root.path().join("installed"), false, 0).unwrap();
        let changes = [Change {
            target: &target,
            before: None,
            after: Some(contents(b"new")),
        }];
        let calls = std::cell::Cell::new(0);
        assert!(
            transact(&changes, Target::replace, || {
                calls.set(calls.get() + 1);
                if calls.get() == 2 {
                    return Err("fixture interruption".into());
                }
                Ok(())
            })
            .is_err()
        );
        assert!(target.read().unwrap().is_none());
    }
}
