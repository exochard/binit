use super::*;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const TIMEOUT: Duration = Duration::from_secs(120);
const OUTPUT_LIMIT: u64 = 4 * 1024 * 1024;

/// A parsed Git source: the clone URL and the repository name.
pub(super) struct Remote {
    pub(super) url: String,
    pub(super) name: String,
}

fn plain(text: &str) -> bool {
    text.bytes().all(|b| (0x21..=0x7e).contains(&b))
}

fn word(text: &str) -> bool {
    !text.is_empty()
        && text
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'))
}

fn repository_name(url: &str) -> String {
    let last = url
        .trim_end_matches('/')
        .rsplit(['/', ':'])
        .next()
        .unwrap_or("");
    last.strip_suffix(".git").unwrap_or(last).to_owned()
}

/// The fixed message for a rejected source that may carry a secret; it never
/// repeats the source.
pub(super) const CREDENTIALS: &str = "plugin line rejected: a source with credentials is not allowed; use a Git credential helper or an SSH key";

/// A user name allowed in `ssh://user@host/...` and `user@host:path`.
fn user_ok(user: &str) -> bool {
    let bytes = user.as_bytes();
    (1..=32).contains(&bytes.len())
        && (bytes[0].is_ascii_lowercase() || bytes[0] == b'_')
        && bytes[1..]
            .iter()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'_' | b'-'))
}

/// True when a rejected source may hold a secret: an `@` before its first
/// `/` (anywhere when it has no `/`), also after a `scheme:` and slashes.
fn may_hold_credentials(source: &str) -> bool {
    let before = |text: &str| text.split('/').next().unwrap_or("").contains('@');
    if before(source) {
        return true;
    }
    match source.split_once(':') {
        Some((scheme, rest))
            if scheme.starts_with(|c: char| c.is_ascii_alphabetic())
                && scheme
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'.' | b'-')) =>
        {
            before(rest.trim_start_matches('/'))
        }
        _ => false,
    }
}

/// Wraps an error of a plugin line with its source. The credentials message
/// stands alone.
pub(super) fn line_error(
    source: &str,
    error: Box<dyn std::error::Error>,
) -> Box<dyn std::error::Error> {
    if error.to_string() == CREDENTIALS {
        return error;
    }
    format!("plugin line {}: {error}", redact(source)).into()
}

/// Returns `None` for an absolute local path and the remote for a Git source.
pub(super) fn parse_source(source: &str) -> Result<Option<Remote>> {
    if source.starts_with('/') {
        return Ok(None);
    }
    let bad = |why: &str| -> Result<Option<Remote>> {
        if may_hold_credentials(source) {
            return Err(CREDENTIALS.into());
        }
        Err(format!(
            "unsupported plugin source {source}: {why}; use an absolute path, user/repo, https://, ssh://, file:// or user@host:path"
        )
        .into())
    };
    if source.starts_with('-') || source.contains("::") || !plain(source) {
        return bad("not a valid source");
    }
    if let Some((scheme, rest)) = source.split_once("://") {
        let authority = rest.split('/').next().unwrap_or("");
        let host = authority.rsplit('@').next().unwrap_or("");
        if let Some((userinfo, _)) = authority.rsplit_once('@')
            && (userinfo.contains(':') || scheme != "ssh" || !user_ok(userinfo))
        {
            return Err(CREDENTIALS.into());
        }
        if !["https", "ssh", "file"].contains(&scheme) {
            return bad("unsupported scheme");
        }
        let valid = if scheme == "file" {
            authority.is_empty() && rest.len() > 1
        } else {
            !host.is_empty() && !host.starts_with('-') && rest.len() > authority.len() + 1
        };
        if !valid {
            return bad("malformed URL");
        }
        return Ok(Some(Remote {
            url: source.to_owned(),
            name: repository_name(source),
        }));
    }
    if let Some((left, path)) = source.split_once(':')
        && let Some((user, host)) = left.split_once('@')
        && user_ok(user)
        && word(host)
        && !host.starts_with(['.', '-'])
        && !path.is_empty()
    {
        return Ok(Some(Remote {
            url: source.to_owned(),
            name: repository_name(source),
        }));
    }
    if let Some((owner, repo)) = source.split_once('/') {
        let valid = word(owner)
            && !owner.starts_with(['.', '-'])
            && word(repo)
            && !matches!(repo, "." | "..");
        if !valid {
            return bad("not a user/repo name");
        }
        let repo = repo.strip_suffix(".git").unwrap_or(repo);
        return Ok(Some(Remote {
            url: format!("https://github.com/{owner}/{repo}.git"),
            name: repo.to_owned(),
        }));
    }
    bad("not a Git source")
}

/// The form of a URL used to compare sources and to key the mirror: no
/// trailing `/`, no `.git`, and a lower-case scheme and host for https and
/// ssh. The clone still uses the URL as written.
pub(super) fn normalize(url: &str) -> String {
    let trimmed = url.trim_end_matches('/');
    let trimmed = trimmed.strip_suffix(".git").unwrap_or(trimmed);
    let trimmed = trimmed.trim_end_matches('/');
    if let Some((scheme, rest)) = trimmed.split_once("://")
        && matches!(scheme.to_ascii_lowercase().as_str(), "https" | "ssh")
    {
        let end = rest.find('/').unwrap_or(rest.len());
        let (authority, path) = rest.split_at(end);
        let authority = match authority.rsplit_once('@') {
            Some((user, host)) => format!("{user}@{}", host.to_ascii_lowercase()),
            None => authority.to_ascii_lowercase(),
        };
        return format!("{}://{authority}{path}", scheme.to_ascii_lowercase());
    }
    trimmed.to_owned()
}

/// The source with a possible secret hidden, for messages.
pub(super) fn redact(source: &str) -> String {
    if matches!(parse_source(source), Err(error) if error.to_string() == CREDENTIALS) {
        return "***".to_owned();
    }
    source.to_owned()
}

pub(super) fn valid_commit(text: &str) -> bool {
    text.len() == 40
        && text
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

pub(super) fn valid_ver(ver: &str) -> bool {
    !ver.is_empty()
        && ver.len() <= 200
        && ver
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'/' | b'+' | b'-'))
        && !ver.starts_with(['-', '/', '.'])
        && !ver.ends_with(['/', '.'])
        && !ver.contains("..")
        && !ver.contains("//")
        && !ver.ends_with(".lock")
}

/// The `ver` value of a plugin line, validated.
pub(super) fn line_ver(argv: &[String]) -> Result<Option<String>> {
    let Some(ver) = argv[1..].iter().find_map(|a| a.strip_prefix("ver=")) else {
        return Ok(None);
    };
    if !valid_ver(ver) {
        return Err(format!("invalid ver: {ver}").into());
    }
    Ok(Some(ver.to_owned()))
}

pub(super) fn validate_origin(url: &str, ver: Option<&str>, commit: &str) -> Result<()> {
    if parse_source(url)?.is_none()
        || !valid_commit(commit)
        || ver.is_some_and(|ver| !valid_ver(ver))
    {
        return Err("invalid Git origin in lock".into());
    }
    Ok(())
}

struct Remove(PathBuf);
impl Drop for Remove {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[derive(Default)]
struct Scope<'a> {
    git_dir: Option<&'a Path>,
}

fn text_of(path: &Path) -> Result<&str> {
    path.to_str()
        .ok_or_else(|| "state path must be UTF-8".into())
}

fn tail(stderr: &str) -> String {
    let lines: Vec<&str> = stderr
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .collect();
    let start = lines.len().saturating_sub(4);
    lines[start..]
        .iter()
        .map(|line| line.chars().take(300).collect::<String>())
        .collect::<Vec<_>>()
        .join("; ")
}

/// Reads at most `limit` bytes and reports whether more was produced.
fn drain(
    mut source: impl Read + Send + 'static,
    limit: u64,
) -> std::thread::JoinHandle<(Vec<u8>, bool)> {
    std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let _ = (&mut source).take(limit).read_to_end(&mut bytes);
        let more = std::io::copy(&mut source, &mut std::io::sink()).is_ok_and(|n| n > 0);
        (bytes, more)
    })
}

/// Runs one Git command without a shell, prompts, hooks or submodules and
/// returns its standard output. Git stays in the terminal's foreground
/// process group, so Ctrl-C reaches it and ssh can ask for a passphrase.
fn git_bytes(step: &str, scope: &Scope, args: &[&str], limit: u64) -> Result<Vec<u8>> {
    let mut command = Command::new("git");
    command.args([
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "protocol.ext.allow=never",
    ]);
    if let Some(dir) = scope.git_dir {
        command.arg("--git-dir").arg(dir);
    }
    command.args(args);
    for name in [
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
    ] {
        command.env_remove(name);
    }
    let mut child = command
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GIT_ASKPASS", "")
        .current_dir("/")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("git {step}: cannot run git: {error}"))?;
    let out = drain(child.stdout.take().ok_or("git output unavailable")?, limit);
    let err = drain(
        child.stderr.take().ok_or("git output unavailable")?,
        OUTPUT_LIMIT,
    );
    let started = Instant::now();
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break status;
        }
        if started.elapsed() > TIMEOUT {
            // A helper that inherited the pipes may outlive git, so the
            // reader threads are left to finish on their own.
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!("git {step} timed out after {} s", TIMEOUT.as_secs()).into());
        }
        std::thread::sleep(Duration::from_millis(20));
    };
    let (stdout, more) = out.join().unwrap_or_default();
    let (stderr, _) = err.join().unwrap_or_default();
    if !status.success() {
        return Err(format!(
            "git {step} failed ({status}): {}",
            tail(&String::from_utf8_lossy(&stderr))
        )
        .into());
    }
    if more {
        return Err(format!("git {step}: output exceeds {} MiB", limit >> 20).into());
    }
    Ok(stdout)
}

fn git(step: &str, scope: &Scope, args: &[&str]) -> Result<String> {
    let bytes = git_bytes(step, scope, args, OUTPUT_LIMIT)?;
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

fn mirror_path(state: &Path, remote: &Remote) -> PathBuf {
    let label: String = remote
        .name
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.') {
                c
            } else {
                '_'
            }
        })
        .take(64)
        .collect();
    let label = label.trim_start_matches('.');
    state.join("sources").join(format!(
        "{}-{}",
        &hash(normalize(&remote.url).as_bytes())[..16],
        if label.is_empty() { "repo" } else { label }
    ))
}

/// Returns the bare mirror of `remote`, cloning it first when it is missing.
/// The flag says whether this call created it.
pub(super) fn ensure(state: &Path, remote: &Remote) -> Result<(PathBuf, bool)> {
    let mirror = mirror_path(state, remote);
    if fs::symlink_metadata(&mirror).is_ok() {
        return Ok((mirror, false));
    }
    let sources = state.join("sources");
    private_dir(&sources)?;
    let staging = sources.join(format!(".staging-{}-{}", std::process::id(), unique()));
    let _cleanup = Remove(staging.clone());
    git(
        "init",
        &Scope::default(),
        &["init", "--bare", "--quiet", "--", text_of(&staging)?],
    )?;
    refresh(&staging, &remote.url)?;
    fs::rename(&staging, &mirror)?;
    Ok((mirror, true))
}

/// Updates the private refs of a mirror from its remote. Branches and tags
/// are required; the remote default branch is optional because a remote
/// whose HEAD points at a deleted branch still serves `ver` lines.
pub(super) fn refresh(mirror: &Path, url: &str) -> Result<()> {
    let scope = Scope {
        git_dir: Some(mirror),
    };
    git(
        "fetch",
        &scope,
        &[
            "fetch",
            "--quiet",
            "--force",
            "--prune",
            "--no-tags",
            "--no-recurse-submodules",
            "--",
            url,
            "+refs/heads/*:refs/binit/heads/*",
            "+refs/tags/*:refs/binit/tags/*",
        ],
    )?;
    let head = git(
        "fetch HEAD",
        &scope,
        &[
            "fetch",
            "--quiet",
            "--force",
            "--no-tags",
            "--no-recurse-submodules",
            "--",
            url,
            "+HEAD:refs/binit/HEAD",
        ],
    );
    if head.is_err() {
        let _ = git(
            "drop HEAD",
            &scope,
            &["update-ref", "-d", "refs/binit/HEAD"],
        );
    }
    Ok(())
}

fn rev(mirror: &Path, name: &str) -> Option<String> {
    git(
        "resolve",
        &Scope {
            git_dir: Some(mirror),
        },
        &[
            "rev-parse",
            "--verify",
            "--quiet",
            &format!("{name}^{{commit}}"),
        ],
    )
    .ok()
    .map(|out| out.trim().to_owned())
    .filter(|commit| valid_commit(commit))
}

/// Whether a branch or tag of the remote contains `commit`.
fn on_ref(mirror: &Path, commit: &str) -> bool {
    git(
        "check reachability",
        &Scope {
            git_dir: Some(mirror),
        },
        &[
            "for-each-ref",
            "--count=1",
            "--format=%(refname)",
            "--contains",
            commit,
            "refs/binit/heads/",
            "refs/binit/tags/",
        ],
    )
    .is_ok_and(|out| !out.trim().is_empty())
}

/// Resolves `ver` (or the remote default branch) to a full commit.
/// `fresh` says the mirror was refreshed during this run.
pub(super) fn pin(mirror: &Path, url: &str, ver: Option<&str>, fresh: bool) -> Result<String> {
    let Some(ver) = ver else {
        return rev(mirror, "refs/binit/HEAD")
            .ok_or_else(|| "the remote has no default branch; set ver=".into());
    };
    if valid_commit(ver) {
        // A server may hand out commits of other repositories (forks), so the
        // commit must be reachable from a branch or tag of this remote.
        let found = || rev(mirror, ver).filter(|commit| on_ref(mirror, commit));
        if let Some(commit) = found() {
            return Ok(commit);
        }
        if !fresh {
            refresh(mirror, url)?;
        }
        return found().ok_or_else(|| "commit is not on any branch or tag of the remote".into());
    }
    for name in [
        format!("refs/binit/tags/{ver}"),
        format!("refs/binit/heads/{ver}"),
    ] {
        if let Some(commit) = rev(mirror, &name) {
            return Ok(commit);
        }
    }
    Err(format!("ver {ver} is not a tag or branch of the remote").into())
}

/// Commit count and up to 20 `git log --oneline` lines for `old..new`.
pub(super) fn changes(mirror: &Path, old: &str, new: &str) -> (Option<u64>, Vec<String>) {
    let scope = Scope {
        git_dir: Some(mirror),
    };
    let range = format!("{old}..{new}");
    let count = git("count", &scope, &["rev-list", "--count", &range, "--"])
        .ok()
        .and_then(|out| out.trim().parse().ok());
    let log = git(
        "log",
        &scope,
        &[
            "log",
            "--oneline",
            "--no-decorate",
            "-n",
            "20",
            &range,
            "--",
        ],
    )
    .unwrap_or_default();
    (count, log.lines().map(str::to_owned).collect())
}

/// One blob of a commit as listed by `git ls-tree`.
struct Entry {
    object: String,
    size: u64,
    symlink: bool,
    executable: bool,
}

/// The files of a commit, read without checking anything out.
pub(super) struct Listing {
    entries: BTreeMap<String, Entry>,
    non_utf8: bool,
}

impl Listing {
    /// Whether the commit has a file or symlink at `path`.
    pub(super) fn has(&self, path: &str) -> bool {
        self.entries.contains_key(path)
    }
}

const LIST_LIMIT: u64 = 64 * 1024 * 1024;

fn valid_object(text: &str) -> bool {
    [40, 64].contains(&text.len())
        && text
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// Lists the files of `commit`. Submodules are skipped.
pub(super) fn list(mirror: &Path, commit: &str) -> Result<Listing> {
    if !valid_commit(commit) {
        return Err("invalid commit".into());
    }
    let out = git_bytes(
        "list files",
        &Scope {
            git_dir: Some(mirror),
        },
        &["ls-tree", "-r", "-z", "--full-tree", "-l", commit],
        LIST_LIMIT,
    )?;
    let mut listing = Listing {
        entries: BTreeMap::new(),
        non_utf8: false,
    };
    for record in out.split(|b| *b == 0).filter(|record| !record.is_empty()) {
        let malformed = || -> Box<dyn std::error::Error> { "malformed git ls-tree output".into() };
        let tab = record
            .iter()
            .position(|b| *b == b'\t')
            .ok_or_else(malformed)?;
        let meta = std::str::from_utf8(&record[..tab]).map_err(|_| malformed())?;
        let mut fields = meta.split_whitespace();
        let (Some(mode), Some(kind), Some(object), Some(size), None) = (
            fields.next(),
            fields.next(),
            fields.next(),
            fields.next(),
            fields.next(),
        ) else {
            return Err(malformed());
        };
        if mode == "160000" {
            continue;
        }
        if kind != "blob"
            || !["100644", "100755", "120000"].contains(&mode)
            || !valid_object(object)
        {
            return Err(format!("unsupported entry in repository: mode {mode} {kind}").into());
        }
        let Ok(path) = std::str::from_utf8(&record[tab + 1..]) else {
            listing.non_utf8 = true;
            continue;
        };
        listing.entries.insert(
            path.to_owned(),
            Entry {
                object: object.to_owned(),
                size: size.parse().map_err(|_| malformed())?,
                symlink: mode == "120000",
                executable: mode == "100755",
            },
        );
    }
    Ok(listing)
}

fn safe_path(path: &str) -> bool {
    !path.is_empty()
        && path
            .split('/')
            .all(|part| !part.is_empty() && part != "." && part != "..")
}

/// A temporary copy of some files of one commit, removed on drop. Blobs are
/// written byte for byte, so no attributes, filters or hooks of the user or
/// the repository can change them.
pub(super) struct Checkout {
    _cleanup: Remove,
    tree: PathBuf,
}
impl Checkout {
    /// Writes the files named in `paths`, or every file when `paths` is
    /// `None`. A symlink among them is an error; limits are checked first.
    pub(super) fn create(
        state: &Path,
        mirror: &Path,
        listing: &Listing,
        paths: Option<&[String]>,
    ) -> Result<Self> {
        let mut chosen = BTreeSet::new();
        match paths {
            None => {
                if listing.non_utf8 {
                    return Err("plugin paths must be UTF-8".into());
                }
                chosen.extend(listing.entries.keys().map(String::as_str));
            }
            Some(paths) => {
                for path in paths {
                    if !safe_path(path) {
                        continue;
                    }
                    let mut prefix = String::new();
                    for part in path.split('/') {
                        if !prefix.is_empty() {
                            prefix.push('/');
                        }
                        prefix.push_str(part);
                        if prefix != *path
                            && listing.entries.get(&prefix).is_some_and(|e| e.symlink)
                        {
                            return Err(format!("symlink in repository: {prefix}").into());
                        }
                    }
                    if listing.has(path) {
                        chosen.insert(path.as_str());
                    }
                }
            }
        }
        let mut total = 0u64;
        for path in &chosen {
            if !safe_path(path) {
                return Err(format!("unsafe path in repository: {path}").into());
            }
            let entry = &listing.entries[*path];
            if entry.symlink {
                return Err(format!("symlink in repository: {path}").into());
            }
            total += entry.size;
        }
        if chosen.len() > MAX_FILES || total > MAX_BYTES {
            return Err("plugin package exceeds 128 files or 2 MiB".into());
        }
        let tmp = state.join("tmp");
        private_dir(&tmp)?;
        let dir = tmp.join(format!("checkout-{}-{}", std::process::id(), unique()));
        private_dir(&dir)?;
        let cleanup = Remove(dir.clone());
        let tree = dir.join("tree");
        fs::create_dir(&tree)?;
        let scope = Scope {
            git_dir: Some(mirror),
        };
        for path in chosen {
            let entry = &listing.entries[path];
            let bytes = git_bytes(
                "read file",
                &scope,
                &["cat-file", "blob", &entry.object],
                MAX_BYTES,
            )?;
            if bytes.len() as u64 != entry.size {
                return Err(format!("git read file: size mismatch for {path}").into());
            }
            let target = tree.join(path);
            if let Some(parent) = target.parent() {
                fs::DirBuilder::new()
                    .recursive(true)
                    .mode(0o700)
                    .create(parent)?;
            }
            OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(if entry.executable { 0o700 } else { 0o600 })
                .open(target)?
                .write_all(&bytes)?;
        }
        Ok(Self {
            _cleanup: cleanup,
            tree,
        })
    }
    pub(super) fn root(&self) -> &Path {
        &self.tree
    }
}

/// Removes staging and checkout directories that an interrupted run left in
/// the state directory. Call it only while holding the mutation guard.
pub(super) fn clean_leftovers(state: &Path) {
    for (name, prefix) in [("sources", ".staging-"), ("tmp", "checkout-")] {
        let dir = state.join(name);
        if !fs::symlink_metadata(&dir).is_ok_and(|meta| meta.is_dir()) {
            continue;
        }
        let Ok(entries) = fs::read_dir(&dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let file = entry.file_name();
            let Some(rest) = file.to_str().and_then(|file| file.strip_prefix(prefix)) else {
                continue;
            };
            let mut parts = rest.split('-');
            let digits = |part: Option<&str>| {
                part.is_some_and(|part| {
                    !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit())
                })
            };
            if digits(parts.next())
                && digits(parts.next())
                && parts.next().is_none()
                && entry.file_type().is_ok_and(|kind| kind.is_dir())
            {
                let _ = fs::remove_dir_all(entry.path());
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn url(source: &str) -> String {
        parse_source(source).unwrap().unwrap().url
    }

    #[test]
    fn user_repo_expands_to_github() {
        assert_eq!(url("user/repo"), "https://github.com/user/repo.git");
        assert_eq!(url("user/repo.git"), "https://github.com/user/repo.git");
        assert_eq!(
            parse_source("user/repo.js").unwrap().unwrap().name,
            "repo.js"
        );
        assert_eq!(parse_source("user/repo").unwrap().unwrap().name, "repo");
    }

    #[test]
    fn url_forms_are_kept_and_named() {
        let remote = parse_source("https://gitlab.com/a/b.git").unwrap().unwrap();
        assert_eq!(
            (remote.url.as_str(), remote.name.as_str()),
            ("https://gitlab.com/a/b.git", "b")
        );
        assert_eq!(
            parse_source("git@github.com:user/repo.git")
                .unwrap()
                .unwrap()
                .name,
            "repo"
        );
        assert_eq!(url("ssh://git@host/x/y"), "ssh://git@host/x/y");
        assert_eq!(url("file:///tmp/x/y.git"), "file:///tmp/x/y.git");
        assert!(parse_source("/abs/dir").unwrap().is_none());
    }

    // The integration tests feed this table to the shell check as well;
    // keep one `("source", accepted),` entry per line, without quotes or
    // escapes inside a source.
    // SOURCE-TABLE-BEGIN
    const SOURCES: &[(&str, bool)] = &[
        ("/abs/dir", true),
        ("/", true),
        ("user/repo", true),
        ("user/repo.git", true),
        ("user/repo.js", true),
        ("a_b/c-d", true),
        ("https://gitlab.com/a/b.git", true),
        ("https://user@host/a/b", false),
        ("https://ghp_token@github.com/u/r", false),
        ("http://alice@host/a/b", false),
        ("http://alice:pw@host/a/b", false),
        ("file://user@/x", false),
        ("ssh://git@host/x/y", true),
        ("ssh://host/x", true),
        ("file:///tmp/x/y.git", true),
        ("git@github.com:user/repo.git", true),
        ("git@github.com:o/r", true),
        ("ssh://a_b-1@h/x", true),
        ("ghp_AbC123@github.com:o/r", false),
        ("ssh://ghp_X@h/x", false),
        ("ssh://Git@h/x", false),
        ("Git@host:path", false),
        ("ssh://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa@h/x", false),
        ("oauth2:glpat-SECRET@gitlab.com:o/r.git", false),
        ("x:SECRET@host:path", false),
        ("https:/u:SECRET@h/x", false),
        ("u@host:path", true),
        ("https://user:secret@host/a/b", false),
        ("https://:token@host/a/b", false),
        ("ssh://u:p@host/x", false),
        ("-oProxyCommand=x/y", false),
        ("--upload-pack=x", false),
        ("ext::sh -c id", false),
        ("fd::3", false),
        ("a::b", false),
        ("http://example.com/a/b", false),
        ("git://example.com/a/b", false),
        ("ftp://example.com/a", false),
        ("https://-evil/a", false),
        ("https://u@-evil/a", false),
        ("ssh://-oProxyCommand=x/y", false),
        ("ssh://u@-host/x", false),
        ("https://host", false),
        ("https://host/", false),
        ("https:///x", false),
        ("file://host/x", false),
        ("file:///", false),
        ("user/repo name", false),
        ("../x", false),
        ("./x", false),
        ("a/b/c", false),
        ("user/..", false),
        ("user/.", false),
        (".user/repo", false),
        ("-user/repo", false),
        ("~/x", false),
        ("relative", false),
        ("-u@host:path", false),
        ("u@-host:path", false),
        ("u@.host:path", false),
        ("u@host:", false),
        ("u@ho st:path", false),
        ("a@b@host:path", false),
        ("", false),
    ];
    // SOURCE-TABLE-END

    #[test]
    fn source_table_is_decided_by_parse_source() {
        for (source, accepted) in SOURCES {
            assert_eq!(
                parse_source(source).is_ok(),
                *accepted,
                "{source:?} should be {}",
                if *accepted { "accepted" } else { "rejected" }
            );
        }
        for source in [
            "user/repo\n",
            "user/repo\tx",
            "https://host/a\u{7}",
            "https://host/a\u{a0}b",
            "https://host/a\u{2028}b",
            "user/re\u{a0}po",
            "u@host:pa\u{a0}th",
        ] {
            assert!(parse_source(source).is_err(), "{source:?} was accepted");
        }
    }

    #[test]
    fn redact_hides_rejected_credentials_only() {
        for source in [
            "https://u:SECRET@h/a/b",
            "https://SECRET@h/a/b",
            "oauth2:glpat-SECRET@gitlab.com:o/r.git",
            "x:SECRET@host:path",
            "https:/u:SECRET@h/x",
            "ssh://ghp_SECRET@h/x",
        ] {
            assert_eq!(redact(source), "***", "{source}");
            let error = parse_source(source).err().unwrap().to_string();
            assert_eq!(error, CREDENTIALS, "{source}");
        }
        assert_eq!(redact("ssh://git@h/a"), "ssh://git@h/a");
        assert_eq!(redact("user/repo"), "user/repo");
    }

    #[test]
    fn normalize_equates_spellings_of_one_repository() {
        let same = [
            "user/repo",
            "https://github.com/user/repo",
            "https://github.com/user/repo.git",
            "https://github.com/user/repo/",
            "https://GitHub.com/user/repo.git/",
        ];
        let want = normalize(&url(same[0]));
        for source in same {
            assert_eq!(normalize(&url(source)), want, "{source}");
        }
        assert_ne!(
            normalize("https://github.com/user/Repo"),
            normalize("https://github.com/user/repo")
        );
        assert_eq!(
            normalize("ssh://git@Host.example/A/b.git"),
            "ssh://git@host.example/A/b"
        );
        assert_eq!(normalize("file:///tmp/X/y.git/"), "file:///tmp/X/y");
    }

    #[test]
    fn credentials_in_urls_are_not_echoed() {
        let error = parse_source("https://user:hunter2@host/a/b").err().unwrap();
        assert!(!error.to_string().contains("hunter2"), "{error}");
        assert!(error.to_string().contains("credential helper"), "{error}");
    }

    #[test]
    fn unsupported_schemes_do_not_echo_credentials() {
        let error = parse_source("http://alice:s3cr3t@host/a/b").err().unwrap();
        assert!(!error.to_string().contains("s3cr3t"), "{error}");
        let error = parse_source("ftp://tok@host/a b").err().unwrap();
        assert!(!error.to_string().contains("tok"), "{error}");
    }

    #[test]
    fn versions_and_commits_are_checked() {
        for ver in ["v1.2.0", "main", "release/1.x", "feature+x"] {
            assert!(valid_ver(ver), "{ver}");
        }
        for ver in ["", "-x", "a..b", "/a", "a/", "a b", "x.lock", "a//b", "a;b"] {
            assert!(!valid_ver(ver), "{ver}");
        }
        assert!(valid_commit(&"a".repeat(40)));
        assert!(!valid_commit(&"A".repeat(40)));
        assert!(!valid_commit(&"a".repeat(39)));
    }
}
