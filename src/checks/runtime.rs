use super::*;

const MAX_RUNTIME_FILE: u64 = 512 * 1024 * 1024;
const MAX_RUNTIME_TOTAL: u64 = 1024 * 1024 * 1024;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub(super) struct RuntimeIdentity {
    pub bash_invocation: PathBuf,
    pub bash: FileIdentity,
    pub manager: FileIdentity,
    pub bash_version: String,
    pub files: Vec<FileIdentity>,
    pub path: String,
    pub native: bool,
}

pub(super) fn file_identity(path: &Path) -> Result<FileIdentity> {
    let path = fs::canonicalize(path)?;
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(&path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.len() > MAX_RUNTIME_FILE {
        return Err("runtime identity needs bounded regular files".into());
    }
    let mut digest = Sha256::new();
    let mut buffer = [0; 16384];
    let mut size = 0u64;
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        size += count as u64;
        if size > MAX_RUNTIME_FILE {
            return Err("runtime file exceeds its identity limit".into());
        }
        digest.update(&buffer[..count]);
    }
    Ok(FileIdentity {
        path,
        sha256: format!("{:x}", digest.finalize()),
    })
}

pub(super) fn manager_identity() -> Result<FileIdentity> {
    file_identity(&std::env::current_exe()?)
}

pub(super) fn bash_invocation() -> Result<PathBuf> {
    let path = std::env::var_os("BINIT_BASH")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/bin/bash"));
    if !path.is_absolute() {
        return Err("BINIT_BASH must be an absolute executable path".into());
    }
    Ok(path)
}

pub(super) fn command(bash: &Path, home: &Path, work: &Path, path: &str) -> std::process::Command {
    let mut command = std::process::Command::new(bash);
    command
        .env_clear()
        .env("PATH", path)
        .env("HOME", home)
        .env("XDG_CONFIG_HOME", home.join("config"))
        .env("XDG_DATA_HOME", home.join("data"))
        .env("XDG_CACHE_HOME", home.join("cache"))
        .env("LC_ALL", "C.UTF-8")
        .env("TERM", "xterm-256color")
        .env("INPUTRC", "/dev/null")
        .env("HISTFILE", "/dev/null")
        .env("BINIT_BASH", bash)
        .env("GIT_CEILING_DIRECTORIES", work)
        .current_dir(work);
    command
}

pub(super) fn probe(
    subject: &Subject,
    state: &Path,
    native: bool,
    directory: &Path,
) -> Result<RuntimeIdentity> {
    let manager = manager_identity()?;
    if manager != subject.manager {
        return Err("Binit executable changed; prepare the candidate again".into());
    }
    let bash_invocation = bash_invocation()?;
    let bash = file_identity(&bash_invocation)?;
    let path = std::env::var("PATH").unwrap_or_else(|_| "/usr/bin:/bin".to_owned());
    let home = directory.join("identity-home");
    private_dir(&home)?;
    let mut command = command(&bash_invocation, &home, directory, &path);
    let module = if native {
        state
            .join("generations")
            .join(&subject.generation)
            .join("completion.so")
    } else {
        PathBuf::new()
    };
    command
        .args([
            "--noprofile",
            "--norc",
            "-c",
            include_str!("../../runtime/check-identity.bash"),
            "binit-check-identity",
        ])
        .arg(&module);
    let output = crate::process::run(&mut command, Duration::from_secs(5))?;
    if !output.status.success()
        || output.timed_out
        || output.interrupted != 0
        || !output.stdout.complete
        || output.stdout.truncated
        || !output.stderr.complete
    {
        return Err("cannot bind the selected Bash/native runtime; check its compatibility".into());
    }
    let text = std::str::from_utf8(&output.stdout.bytes)?;
    let mut lines = text.lines();
    let version = lines
        .next()
        .and_then(|line| line.strip_prefix("BINIT-IDENTITY\t"))
        .ok_or("invalid runtime identity response")?
        .to_owned();
    let mut files = BTreeMap::new();
    let mut total = 0u64;
    for line in lines {
        let mut rest = line;
        let mut inode = 0u64;
        for field in 0..5 {
            let end = rest.find(char::is_whitespace).unwrap_or(rest.len());
            let word = &rest[..end];
            if field == 3 {
                let (major, minor) = word.split_once(':').ok_or("invalid runtime mapping")?;
                u32::from_str_radix(major, 16)?;
                u32::from_str_radix(minor, 16)?;
            } else if field == 4 {
                inode = word.parse()?;
            }
            rest = rest[end..].trim_start_matches(char::is_whitespace);
        }
        if !rest.starts_with('/') {
            continue;
        }
        // Proc escaping is ambiguous for unusual filenames; never bind a guess.
        if rest.contains('\\') || rest.ends_with(" (deleted)") {
            return Err("runtime mapping changed or uses an unsupported escaped filename".into());
        }
        let mapped = PathBuf::from(rest);
        let metadata = fs::metadata(&mapped)?;
        // Btrfs mapping devices can differ from stat's anonymous subvolume
        // device. Bind the mapped path, inode and contents instead.
        if metadata.ino() != inode {
            return Err("runtime file changed during identity capture".into());
        }
        if !files.contains_key(&mapped) {
            if files.len() >= 64 {
                return Err("runtime identity exceeds 64 mapped files".into());
            }
            total = total
                .checked_add(metadata.len())
                .ok_or("runtime size overflow")?;
            if total > MAX_RUNTIME_TOTAL {
                return Err("runtime identity exceeds 1 GiB of files".into());
            }
            files.insert(mapped.clone(), file_identity(&mapped)?);
        }
    }
    if files.is_empty()
        || file_identity(&bash_invocation)? != bash
        || manager_identity()? != manager
    {
        return Err("runtime identity changed during capture".into());
    }
    Ok(RuntimeIdentity {
        bash_invocation,
        bash,
        manager,
        bash_version: version,
        files: files.into_values().collect(),
        path,
        native,
    })
}
