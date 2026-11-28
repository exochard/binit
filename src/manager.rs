use super::*;
use std::os::fd::AsRawFd;

#[derive(Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Installed {
    pub(super) plugin: Plugin,
    pub(super) enabled: bool,
    pub(super) source: Option<PathBuf>,
    /// The arguments of the `binit plugin` line that produced this entry.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub(super) declaration: Vec<String>,
    /// The Git source and the commit it is pinned to.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(super) origin: Option<Origin>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(super) asset: Option<remote::Origin>,
}

#[derive(Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Origin {
    pub(super) url: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(super) ver: Option<String>,
    pub(super) commit: String,
}

pub(super) struct MutationGuard(fs::File);
impl MutationGuard {
    pub(super) fn acquire(state: &Path) -> Result<Self> {
        private_dir(state)?;
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(state.join(".mutation.lock"))?;
        let metadata = file.metadata()?;
        let uid = unsafe { libc::geteuid() };
        if !metadata.is_file()
            || metadata.uid() != uid
            || metadata.permissions().mode() & 0o077 != 0
        {
            return Err("mutation lock must be a private regular file".into());
        }
        // The descriptor owns the lock; process exit releases it after failures.
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err("another Binit change is running; retry when it finishes".into());
        }
        Ok(Self(file))
    }
}
impl Drop for MutationGuard {
    fn drop(&mut self) {
        unsafe { libc::flock(self.0.as_raw_fd(), libc::LOCK_UN) };
    }
}

struct Plan {
    catalog: Vec<Installed>,
    packages: BTreeMap<String, Vec<u8>>,
}

impl Plan {
    fn replace(&mut self, position: usize, item: Installed, files: BTreeMap<String, Vec<u8>>) {
        let prefix = format!("plugins/{}/", self.catalog[position].plugin.id);
        self.packages.retain(|name, _| !name.starts_with(&prefix));
        self.packages.extend(files);
        self.catalog[position] = item;
    }
}

pub(super) fn catalog(lock: &Lock) -> Vec<Installed> {
    if lock.schema == 1 {
        lock.plugins
            .iter()
            .cloned()
            .map(|plugin| Installed {
                plugin,
                enabled: true,
                source: None,
                declaration: Vec::new(),
                origin: None,
                asset: None,
            })
            .collect()
    } else {
        lock.catalog.clone()
    }
}

fn current(state: &Path) -> Result<Plan> {
    if !state.join("current").exists() {
        return Ok(Plan {
            catalog: Vec::new(),
            packages: BTreeMap::new(),
        });
    }
    let id = pointer(state, "current")?;
    let lock = verify_generation(state, &id)?;
    let root = state.join("generations").join(id);
    let catalog = catalog(&lock);
    let mut packages = BTreeMap::new();
    for name in lock
        .files
        .keys()
        .filter(|name| name.starts_with("plugins/"))
    {
        packages.insert(name.clone(), snapshot_file(&root, name, &lock)?);
    }
    Ok(Plan { catalog, packages })
}

pub(super) fn package(
    path: &Path,
    enabled: bool,
) -> Result<(Installed, BTreeMap<String, Vec<u8>>)> {
    let (plugin, files) = package::read(path)?;
    installed(
        plugin,
        files,
        Some(fs::canonicalize(path)?),
        enabled,
        Vec::new(),
        None,
    )
}

fn installed(
    plugin: Plugin,
    files: BTreeMap<String, Vec<u8>>,
    source: Option<PathBuf>,
    enabled: bool,
    declaration: Vec<String>,
    origin: Option<Origin>,
) -> Result<(Installed, BTreeMap<String, Vec<u8>>)> {
    let files = files
        .into_iter()
        .map(|(name, bytes)| (format!("plugins/{}/{name}", plugin.id), bytes))
        .collect();
    Ok((
        Installed {
            plugin,
            enabled,
            source,
            declaration,
            origin,
            asset: None,
        },
        files,
    ))
}

struct Stage(PathBuf);
impl Stage {
    fn new(state: &Path, files: BTreeMap<String, Vec<u8>>) -> Result<Self> {
        let parent = state.join("tmp");
        private_dir(&parent)?;
        let stage = Self(parent.join(format!("checkout-{}-{}", std::process::id(), unique())));
        fs::DirBuilder::new().mode(0o700).create(&stage.0)?;
        for (path, bytes) in files {
            if !package::valid_declared_file(&path) {
                return Err("unsafe staging path".into());
            }
            let path = stage.0.join(path);
            fs::create_dir_all(path.parent().ok_or("staging parent missing")?)?;
            OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(path)?
                .write_all(&bytes)?;
        }
        Ok(stage)
    }
}
impl Drop for Stage {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn hook(argv: &[String], directory: &Path, event: Option<&str>) -> Result<()> {
    if let Some(event) = event {
        let keys = package::line_keys(argv)?;
        if let Some(script) = keys.get(event).filter(|script| !script.is_empty()) {
            let id = if let Some(id) = keys.get("id") {
                id.to_string()
            } else if directory.join("plugin.toml").is_file() {
                package::parse_manifest(&read_regular(&directory.join("plugin.toml"))?)?
                    .0
                    .id
            } else if let Some(source) = git::parse_source(&argv[0])? {
                source.name
            } else {
                Path::new(&argv[0])
                    .file_name()
                    .and_then(|name| name.to_str())
                    .unwrap_or("plugin")
                    .to_owned()
            };
            hooks::run(script, directory, &id, event)?;
        }
    }
    Ok(())
}

fn line_package(
    state: &Path,
    argv: &[String],
    event: Option<&str>,
) -> Result<(Installed, BTreeMap<String, Vec<u8>>)> {
    let keys = package::line_keys(argv)?;
    let staging =
        if event.is_some_and(|event| keys.get(event).is_some_and(|script| !script.is_empty())) {
            let root = Path::new(&argv[0]);
            if !root.is_absolute() || !fs::symlink_metadata(root)?.is_dir() {
                return Err("hook source must be an absolute directory, not a symlink".into());
            }
            let mut files = BTreeMap::new();
            collect(root, Path::new(""), &mut files)?;
            let executable = files
                .keys()
                .filter(|path| {
                    fs::metadata(root.join(path))
                        .is_ok_and(|meta| meta.permissions().mode() & 0o111 != 0)
                })
                .cloned()
                .collect::<Vec<_>>();
            let stage = Stage::new(state, files)?;
            for path in executable {
                fs::set_permissions(stage.0.join(path), fs::Permissions::from_mode(0o700))?;
            }
            Some(stage)
        } else {
            None
        };
    let (plugin, files) = if let Some(stage) = &staging {
        hook(argv, &stage.0, event)?;
        package::git_line(
            argv,
            &stage.0,
            Path::new(&argv[0])
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("plugin"),
        )?
    } else {
        package::line(argv)?
    };
    let source = Some(fs::canonicalize(&argv[0])?);
    installed(plugin, files, source, true, argv.to_vec(), None)
}

/// Pins a Git line to `commit` and builds its package from a checkout.
fn git_package(
    state: &Path,
    argv: &[String],
    remote: &git::Remote,
    mirror: &Path,
    commit: &str,
    event: Option<&str>,
) -> Result<(Installed, BTreeMap<String, Vec<u8>>)> {
    let ver = git::line_ver(argv)?;
    let listing = git::list(mirror, commit)?;
    let keys = package::line_keys(argv)?;
    let paths =
        if event.is_some_and(|event| keys.get(event).is_some_and(|script| !script.is_empty())) {
            None
        } else {
            package::git_paths(
                argv,
                &remote.name,
                &|path| listing.has(path),
                &listing.paths(),
            )?
        };
    let checkout = git::Checkout::create(state, mirror, &listing, paths.as_deref())?;
    hook(argv, checkout.root(), event)?;
    let (plugin, mut files) = package::git_line(argv, checkout.root(), &remote.name)?;
    if paths.is_some()
        && !checkout.root().join("plugin.toml").exists()
        && let Some(manifest) = listing.manifest(mirror)?
    {
        package::preserve_minimum(&plugin.id, &mut files, &manifest)?;
    }
    let origin = Origin {
        url: remote.url.clone(),
        ver,
        commit: commit.to_owned(),
    };
    installed(plugin, files, None, true, argv.to_vec(), Some(origin))
}

fn remote_package(
    state: &Path,
    argv: &[String],
    event: Option<&str>,
) -> Result<(Installed, BTreeMap<String, Vec<u8>>)> {
    package::check_keys(argv)?;
    let origin = remote::origin(argv)?;
    let stage = Stage::new(state, remote::materialize(state, argv, &origin)?)?;
    for item in package::line_keys(argv)?
        .get("bin")
        .into_iter()
        .flat_map(|value| value.split(','))
    {
        let (path, _) = package::bin_item(item)?;
        let path = stage.0.join(path);
        if fs::symlink_metadata(&path).is_ok_and(|meta| meta.is_file()) {
            fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
        }
    }
    hook(argv, &stage.0, event)?;
    let (plugin, files) = package::downloaded_line(argv, &stage.0)?;
    let (mut item, files) = installed(plugin, files, None, true, argv.to_vec(), None)?;
    item.asset = Some(origin);
    Ok((item, files))
}

fn require_catalog(entry: &Installed) -> Result<()> {
    if entry.declaration.is_empty() {
        return Ok(());
    }
    Err(format!(
        "{} is managed by a binit plugin line; edit the line and run binit sync",
        entry.plugin.id
    )
    .into())
}

/// One line per change: what happened, in which generation, and that new shells use it.
fn announce(change: &str, generation: &str) {
    println!("{change} in {}; new shells use it", &generation[..12]);
}

pub(super) fn add(state: &Path, path: &Path, enabled: bool) -> Result<()> {
    let _guard = MutationGuard::acquire(state)?;
    let mut plan = current(state)?;
    let (item, files) = package(path, enabled)?;
    let id = item.plugin.id.clone();
    if plan.catalog.iter().any(|entry| entry.plugin.id == id) {
        return Err(format!("plugin already installed: {id}; use binit update {id}").into());
    }
    plan.catalog.push(item);
    plan.packages.extend(files);
    let generation = publish(plan.catalog, plan.packages, state)?;
    announce(
        &format!("added {id}{}", if enabled { "" } else { " (disabled)" }),
        &generation,
    );
    Ok(())
}

pub(super) fn change(state: &Path, id: &str, action: &str) -> Result<()> {
    let _guard = MutationGuard::acquire(state)?;
    let mut plan = current(state)?;
    let position = plan
        .catalog
        .iter()
        .position(|entry| entry.plugin.id == id)
        .ok_or_else(|| format!("plugin not installed: {id}"))?;
    require_catalog(&plan.catalog[position])?;
    match action {
        "enable" => plan.catalog[position].enabled = true,
        "disable" => plan.catalog[position].enabled = false,
        "remove" => {
            plan.catalog.remove(position);
            let prefix = format!("plugins/{id}/");
            plan.packages.retain(|name, _| !name.starts_with(&prefix));
        }
        _ => return Err("unknown plugin action".into()),
    }
    let generation = publish(plan.catalog, plan.packages, state)?;
    let verb = match action {
        "enable" => "enabled",
        "disable" => "disabled",
        _ => "removed",
    };
    announce(&format!("{verb} {id}"), &generation);
    Ok(())
}

type Mirrors = BTreeMap<String, (PathBuf, bool)>;

/// Network work is grouped by mirror. Threads finish before packages and hooks
/// are prepared, and publishing still happens once after all preparations.
fn prefetch_mirrors(state: &Path, requests: Vec<(String, bool)>, jobs: usize) -> Result<Mirrors> {
    use std::sync::Mutex;
    use std::sync::atomic::{AtomicUsize, Ordering};

    if !(1..=8).contains(&jobs) {
        return Err("jobs must be between 1 and 8".into());
    }
    let mut tasks: Vec<(String, String, bool)> = Vec::new();
    let mut indices = BTreeMap::new();
    for (source, refresh) in requests {
        let remote = git::parse_source(&source)
            .map_err(|error| git::line_error(&source, error))?
            .ok_or("Git origin on a local line")?;
        let key = git::normalize(&remote.url);
        if let Some(&index) = indices.get(&key) {
            let task: &mut (String, String, bool) = &mut tasks[index];
            task.2 |= refresh;
        } else {
            indices.insert(key.clone(), tasks.len());
            tasks.push((key, source, refresh));
        }
    }
    if tasks.is_empty() {
        return Ok(BTreeMap::new());
    }
    // Creating the shared directory once avoids races in private_dir's
    // create-then-verify path; each worker owns a distinct mirror staging dir.
    private_dir(&state.join("sources"))?;
    let next = AtomicUsize::new(0);
    // Foreground SSH can prompt on the terminal, so allow only one such
    // acquisition at a time while other transports continue in parallel.
    let ssh = Mutex::new(());
    let results = Mutex::new(
        std::iter::repeat_with(|| None)
            .take(tasks.len())
            .collect::<Vec<Option<std::result::Result<(PathBuf, bool), String>>>>(),
    );
    std::thread::scope(|scope| {
        for _ in 0..jobs.min(tasks.len()) {
            scope.spawn(|| {
                loop {
                    let index = next.fetch_add(1, Ordering::Relaxed);
                    let Some((_, source, refresh)) = tasks.get(index) else {
                        break;
                    };
                    let result = (|| -> Result<(PathBuf, bool)> {
                        let remote =
                            git::parse_source(source)?.ok_or("Git origin on a local line")?;
                        let _ssh_guard = if git::is_ssh_url(&remote.url) {
                            Some(ssh.lock().map_err(|_| "SSH acquisition lock poisoned")?)
                        } else {
                            None
                        };
                        let (mirror, created) = git::ensure(state, &remote)?;
                        if *refresh && !created {
                            git::refresh(&mirror, &remote.url)?;
                        }
                        Ok((mirror, created || *refresh))
                    })()
                    .map_err(|error| git::line_error(source, error).to_string());
                    results.lock().expect("mirror result lock")[index] = Some(result);
                }
            });
        }
    });
    let results = results
        .into_inner()
        .map_err(|_| "mirror result lock poisoned")?;
    let mut mirrors = BTreeMap::new();
    for ((key, _, _), result) in tasks.into_iter().zip(results) {
        let prepared = result
            .ok_or("missing mirror result")?
            .map_err(|error| -> Box<dyn std::error::Error> { error.into() })?;
        mirrors.insert(key, prepared);
    }
    Ok(mirrors)
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum UpdateMode {
    Apply,
    Check,
    Stage,
}

pub(super) fn update_command_with_jobs(state: &Path, args: &[String], jobs: usize) -> Result<()> {
    if !(1..=8).contains(&jobs) {
        return Err("jobs must be between 1 and 8".into());
    }
    let mut mode = UpdateMode::Apply;
    let mut targets = Vec::new();
    for arg in args {
        let requested = match arg.as_str() {
            "--check" => UpdateMode::Check,
            "--stage" => UpdateMode::Stage,
            _ => {
                targets.push(arg.as_str());
                continue;
            }
        };
        if mode != UpdateMode::Apply {
            return Err("specify either --check or --stage once".into());
        }
        mode = requested;
    }
    let _guard = MutationGuard::acquire(state)?;
    let plan = current(state)?;
    if mode != UpdateMode::Check && (1..=2).contains(&targets.len()) {
        let catalog = plan
            .catalog
            .iter()
            .any(|entry| entry.plugin.id == targets[0] && entry.declaration.is_empty());
        let installed = |id: &str| plan.catalog.iter().any(|entry| entry.plugin.id == id);
        // Treat the second argument as a replacement path only for a catalog
        // plugin and only if it does not name an installed plugin.
        if catalog && (targets.len() == 1 || !installed(targets[1])) {
            return update_catalog(state, plan, targets[0], targets.get(1).map(Path::new), mode);
        }
    }
    update_lines(state, plan, &targets, mode, jobs)
}

fn update_lines(
    state: &Path,
    mut plan: Plan,
    ids: &[&str],
    mode: UpdateMode,
    jobs: usize,
) -> Result<()> {
    let check = mode == UpdateMode::Check;
    git::clean_leftovers(state);
    let mut targets = Vec::new();
    if ids.is_empty() {
        for (position, entry) in plan.catalog.iter().enumerate() {
            if !entry.declaration.is_empty() {
                targets.push(position);
            }
        }
        if targets.is_empty() {
            println!("no declared plugins to update");
            return Ok(());
        }
    }
    for id in ids {
        let position = plan
            .catalog
            .iter()
            .position(|entry| entry.plugin.id == *id)
            .ok_or_else(|| format!("plugin not installed: {id}"))?;
        let entry = &plan.catalog[position];
        if entry.declaration.is_empty() {
            return Err(format!("{id} is a catalog plugin; use binit update {id} [PATH]").into());
        }
        if !targets.contains(&position) {
            targets.push(position);
        }
    }
    let mut requests = Vec::new();
    for &position in &targets {
        let entry = &plan.catalog[position];
        if let Some(origin) = &entry.origin
            && !origin.ver.as_deref().is_some_and(git::valid_commit)
        {
            package::check_keys(&entry.declaration)?;
            requests.push((entry.declaration[0].clone(), true));
        }
    }
    let progress = (!requests.is_empty())
        .then(|| progress::Progress::start("Checking plugin sources"))
        .flatten();
    let mirrors = prefetch_mirrors(state, requests, jobs);
    if let Some(progress) = progress {
        progress.finish();
    }
    let mirrors = mirrors?;
    let mut changes = Vec::new();
    // Results print after the generation is prepared, so a failure shows no
    // moves.
    let mut report = Vec::new();
    let mut say = |line: String| {
        if check {
            println!("{line}");
        } else {
            report.push(line);
        }
    };
    for position in targets {
        let entry = plan.catalog[position].clone();
        let id = entry.plugin.id.clone();
        if entry.asset.is_some() {
            say(format!(
                "{id}  pinned by sha256; edit the source line and run binit sync"
            ));
            continue;
        }
        let wrap = |error: Box<dyn std::error::Error>| -> Box<dyn std::error::Error> {
            git::line_error(&entry.declaration[0], error)
        };
        let unchanged_id = |item: &Installed| -> Result<()> {
            if item.plugin.id == id {
                return Ok(());
            }
            Err(wrap(
                format!(
                    "the plugin id would change from {id} to {}; edit the line (for example add id={id} and files=) and run binit sync",
                    item.plugin.id
                )
                .into(),
            ))
        };
        let Some(origin) = &entry.origin else {
            if check || ids.is_empty() {
                let keys = package::line_keys(&entry.declaration).map_err(wrap)?;
                let has_hook = |event| keys.get(event).is_some_and(|script| !script.is_empty());
                if has_hook("atclone") && !has_hook("atpull") {
                    say(format!(
                        "{id}  kept prepared snapshot (atclone without atpull)"
                    ));
                    continue;
                }
                if check {
                    say(format!("{id}  local source (refreshed by binit update)"));
                    continue;
                }
            }
            let (item, files) =
                line_package(state, &entry.declaration, Some("atpull")).map_err(wrap)?;
            unchanged_id(&item)?;
            say(format!("{id}  re-snapshotted"));
            changes.push((position, item, files));
            continue;
        };
        if origin.ver.as_deref().is_some_and(git::valid_commit) {
            say(format!(
                "{id}  pinned to commit; to track a branch again, remove ver= or set ver=BRANCH, then run binit sync"
            ));
            continue;
        }
        package::check_keys(&entry.declaration).map_err(wrap)?;
        let remote = git::parse_source(&entry.declaration[0])
            .map_err(wrap)?
            .ok_or("Git origin on a local line")?;
        let (mirror, _) = mirrors
            .get(&git::normalize(&remote.url))
            .ok_or("missing prepared mirror")?;
        let commit = git::pin(mirror, &remote.url, origin.ver.as_deref(), true).map_err(wrap)?;
        if commit == origin.commit {
            say(format!("{id}  up to date"));
            continue;
        }
        let moved = format!("{id}  {} -> {}", &origin.commit[..12], &commit[..12]);
        if check {
            let (count, log) = git::changes(mirror, &origin.commit, &commit);
            match count {
                Some(1) => println!("{moved}  (1 commit)"),
                Some(count) => println!("{moved}  ({count} commits)"),
                None => println!("{moved}"),
            }
            for line in log {
                println!("{line}");
            }
        } else {
            let (item, files) = git_package(
                state,
                &entry.declaration,
                &remote,
                mirror,
                &commit,
                Some("atpull"),
            )
            .map_err(wrap)?;
            unchanged_id(&item)?;
            say(moved);
            changes.push((position, item, files));
        }
    }
    if check {
        return Ok(());
    }
    if changes.is_empty() {
        for line in report {
            println!("{line}");
        }
        return Ok(());
    }
    for (position, item, files) in changes {
        plan.replace(position, item, files);
    }
    let generation = finish_update(state, plan, mode)?;
    for line in report {
        println!("{line}");
    }
    if let Some(generation) = generation {
        println!("applied {}; new shells use it", &generation[..12]);
    }
    Ok(())
}

fn finish_update(state: &Path, plan: Plan, mode: UpdateMode) -> Result<Option<String>> {
    if mode == UpdateMode::Apply {
        return Ok(Some(publish(plan.catalog, plan.packages, state)?));
    }
    let base = pointer(state, "current")?;
    stage_plan(state, plan, Some(base))?;
    Ok(None)
}

fn stage_plan(state: &Path, plan: Plan, base: Option<String>) -> Result<()> {
    let generation = install_generation(plan.catalog, plan.packages, state)?;
    if base.as_deref() == Some(generation.as_str()) {
        println!("no generation changes to stage");
    } else {
        candidate::save_prepared(state, generation, base, None)?;
    }
    Ok(())
}

pub(super) fn stage_restore(state: &Path, id: &str, from: Option<&str>) -> Result<()> {
    let _guard = MutationGuard::acquire(state)?;
    let mut plan = current(state)?;
    let position = plan
        .catalog
        .iter()
        .position(|entry| entry.plugin.id == id)
        .ok_or_else(|| format!("plugin not installed: {id}"))?;
    let base = pointer(state, "current")?;
    let source = match from {
        Some(generation) => generation.to_owned(),
        None => pointer(state, "previous").map_err(|error| {
            format!("cannot read previous generation ({error}); pass --from GENERATION")
        })?,
    };
    let lock = verify_generation(state, &source)?;
    let mut item = catalog(&lock)
        .into_iter()
        .find(|entry| entry.plugin.id == id)
        .ok_or_else(|| format!("plugin {id} is absent from generation {source}"))?;
    let current = &plan.catalog[position];
    if item.declaration != current.declaration {
        return Err(format!(
            "plugin {id} has a different saved declaration in generation {source}"
        )
        .into());
    }
    item.enabled = current.enabled;
    let prefix = format!("plugins/{id}/");
    let root = state.join("generations").join(&source);
    let mut files = BTreeMap::new();
    for (name, sha256) in lock
        .files
        .range(prefix.clone()..)
        .take_while(|(name, _)| name.starts_with(&prefix))
    {
        let bytes = snapshot_file(&root, name, &lock)?;
        if hash(&bytes) != *sha256 {
            return Err(format!("content changed: {name}").into());
        }
        files.insert(name.clone(), bytes);
    }
    if item == *current
        && plan
            .packages
            .range(prefix.clone()..)
            .take_while(|(name, _)| name.starts_with(&prefix))
            .eq(&files)
    {
        println!("no plugin changes to stage");
        return Ok(());
    }
    plan.replace(position, item, files);
    stage_plan(state, plan, Some(base))?;
    println!("snapshot   {id} from {source}");
    Ok(())
}

fn update_catalog(
    state: &Path,
    mut plan: Plan,
    id: &str,
    path: Option<&Path>,
    mode: UpdateMode,
) -> Result<()> {
    let position = plan
        .catalog
        .iter()
        .position(|entry| entry.plugin.id == id)
        .ok_or_else(|| format!("plugin not installed: {id}"))?;
    let old = &plan.catalog[position];
    require_catalog(old)?;
    let source = path
        .or(old.source.as_deref())
        .ok_or("legacy plugin has no source path; use binit update ID PATH")?;
    let (item, files) = package(source, old.enabled)?;
    if item.plugin.id != id {
        return Err(format!("update changes plugin ID from {id} to {}", item.plugin.id).into());
    }
    plan.replace(position, item, files);
    if let Some(generation) = finish_update(state, plan, mode)? {
        announce(&format!("updated {id}"), &generation);
    }
    Ok(())
}

pub(super) fn list(state: &Path) -> Result<()> {
    let plan = current(state)?;
    if plan.catalog.is_empty() {
        println!("no plugins");
        return Ok(());
    }
    println!("{:<20} {:<12} LOAD", "PLUGIN", "NEXT_SHELL");
    for entry in plan.catalog {
        println!(
            "{:<20} {:<12} {}",
            entry.plugin.id,
            if entry.enabled { "enabled" } else { "disabled" },
            entry.plugin.load
        );
    }
    Ok(())
}

pub(super) fn plugin_session(state: &Path, json: bool) -> Result<()> {
    use std::io::IsTerminal;

    let mut input = Vec::new();
    std::io::stdin()
        .take(DECLARED_MAX + 1)
        .read_to_end(&mut input)?;
    if input.len() as u64 > DECLARED_MAX {
        return Err("plugin session exceeds 256 KiB".into());
    }
    let session = plugin_session::Session::parse(&input)?;
    let selected = candidate::selected_id(state)?;
    let installed = match selected.as_deref() {
        Some(id) => catalog(&verify_generation(state, id)?),
        None => Vec::new(),
    };
    let stdout = std::io::stdout();
    let rows = installed.iter().map(|entry| {
        (
            entry.plugin.id.as_str(),
            entry.enabled,
            entry.plugin.load.as_str(),
        )
    });
    if json {
        return session.write_json(&mut stdout.lock(), selected.as_deref(), rows);
    }
    let terminal = stdout.is_terminal();
    let color = crate::terminal_ui::color(terminal);
    session.write(
        &mut stdout.lock(),
        selected.as_deref(),
        rows,
        crate::terminal_ui::width(terminal, libc::STDOUT_FILENO),
        color,
    )
}

pub(super) fn inspect(state: &Path, id: &str) -> Result<()> {
    let generation = pointer(state, "current")?;
    let lock = verify_generation(state, &generation)?;
    let catalog = catalog(&lock);
    let entry = catalog
        .iter()
        .find(|entry| entry.plugin.id == id)
        .ok_or_else(|| format!("plugin not installed: {id}"))?;
    let file = format!("plugins/{id}/{}", entry.plugin.entry);
    let digest = lock
        .files
        .get(&file)
        .ok_or("plugin entry is missing from the lock")?;
    println!(
        "plugin        {id}\nnext shell    {}\nload          {}\ncommands      {}\nrequires      {}\ndepends       {}\nsource        {}\nentry sha256  {digest}",
        if entry.enabled { "enabled" } else { "disabled" },
        entry.plugin.load,
        entry.plugin.commands.join(", "),
        entry.plugin.requires.join(", "),
        entry.plugin.depends.join(", "),
        match (&entry.origin, entry.source.as_deref()) {
            (Some(origin), _) => origin.url.clone(),
            (None, Some(path)) => crate::releases::plain_text(&path.to_string_lossy()),
            (None, None) => "legacy snapshot".to_owned(),
        }
    );
    if !entry.plugin.bin.is_empty() {
        let programs = entry
            .plugin
            .bin
            .iter()
            .filter_map(|item| item.rsplit_once(':'))
            .map(|(path, name)| {
                crate::releases::plain_text(&format!(
                    "{name} ({})",
                    path.strip_prefix("files/").unwrap_or(path)
                ))
            })
            .collect::<Vec<_>>();
        println!(
            "programs      {}{}",
            programs.join(", "),
            if entry.enabled { "" } else { " (disabled)" }
        );
    }
    if let Some(origin) = &entry.origin {
        println!(
            "ver           {}\ncommit        {}",
            crate::releases::plain_text(origin.ver.as_deref().unwrap_or("default branch")),
            origin.commit
        );
    }
    if let Some(asset) = &entry.asset {
        println!(
            "remote        {}\nsha256        {}\nformat        {}",
            asset.url, asset.sha256, asset.format
        );
    }
    Ok(())
}

pub(super) fn shell(state: &Path) -> Result<()> {
    use std::os::unix::process::CommandExt;
    let bash = match std::env::var_os("BINIT_BASH") {
        Some(value) if Path::new(&value).is_absolute() => value,
        Some(_) => return Err("BINIT_BASH must be an absolute executable path".into()),
        None => "bash".into(),
    };
    if std::env::var_os("BINIT_SAFE").is_some_and(|value| value == "1") {
        return Err(std::process::Command::new(&bash)
            .args(["--noprofile", "--norc", "-i"])
            .exec()
            .into());
    }
    let id = pointer(state, "current")?;
    let lock = verify_generation(state, &id)?;
    let init = fs::canonicalize(state.join("generations").join(id).join("init.bash"))?;
    let declarations: Vec<_> = lock
        .catalog
        .iter()
        .filter(|entry| entry.enabled && !entry.declaration.is_empty())
        .map(|entry| entry.declaration.as_slice())
        .collect();
    let mut command = std::process::Command::new(&bash);
    command.args(["--noprofile", "--rcfile"]);
    if declarations.is_empty() {
        return Err(command.arg(init).arg("-i").exec().into());
    }
    let startup = isolated_shell_startup(&init, &declarations)?;
    Err(command
        .arg(format!("/proc/self/fd/{}", startup.as_raw_fd()))
        .arg("-i")
        .exec()
        .into())
}

fn isolated_shell_startup(init: &Path, declarations: &[&[String]]) -> Result<fs::File> {
    use std::io::{Seek, SeekFrom};
    use std::os::fd::FromRawFd;

    let descriptor = unsafe {
        libc::memfd_create(
            c"binit-shell".as_ptr(),
            libc::MFD_CLOEXEC | libc::MFD_ALLOW_SEALING,
        )
    };
    if descriptor < 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let mut file = unsafe { fs::File::from_raw_fd(descriptor) };
    let init = quote(init.to_str().ok_or("initialization path must be UTF-8")?);
    // Bash opens the rc path before sourcing it. Close the inherited descriptor
    // before any plugin or user command can keep it open in a child.
    writeln!(file, "exec {descriptor}<&-\nsource {init} || return")?;
    for line in declarations {
        write!(file, "__binit_plugin_line")?;
        for arg in *line {
            write!(file, " {}", quote(arg))?;
        }
        writeln!(file)?;
    }
    file.seek(SeekFrom::Start(0))?;
    let seals = libc::F_SEAL_SEAL | libc::F_SEAL_SHRINK | libc::F_SEAL_GROW | libc::F_SEAL_WRITE;
    if unsafe { libc::fcntl(descriptor, libc::F_ADD_SEALS, seals) } < 0
        || unsafe { libc::fcntl(descriptor, libc::F_SETFD, 0) } < 0
    {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(file)
}

pub(super) fn apply(state: &Path) -> Result<()> {
    let _guard = MutationGuard::acquire(state)?;
    let plan = current(state)?;
    let generation = publish(plan.catalog, plan.packages, state)?;
    println!("applied {generation}; new shells use it");
    Ok(())
}

const DECLARED_MAX: u64 = 256 * 1024;
const DECLARED_LINES: usize = 64;
const DECLARED_ARGUMENTS: usize = 16;

/// Parses records of an argument count followed by that many arguments, each
/// terminated by NUL.
fn parse_declared(bytes: &[u8]) -> Result<Vec<Vec<String>>> {
    let text = std::str::from_utf8(bytes).map_err(|_| "declared plugin lines must be UTF-8")?;
    if text.is_empty() {
        return Ok(Vec::new());
    }
    let body = text
        .strip_suffix('\0')
        .ok_or("declared plugin lines are truncated")?;
    let mut fields = body.split('\0');
    let mut lines = Vec::new();
    while let Some(count) = fields.next() {
        if count.is_empty() || count.len() > 2 || !count.bytes().all(|b| b.is_ascii_digit()) {
            return Err("declared plugin lines are malformed".into());
        }
        let count: usize = count.parse()?;
        if count == 0 || count > DECLARED_ARGUMENTS || lines.len() == DECLARED_LINES {
            return Err("declared plugin lines exceed 64 lines or 16 arguments".into());
        }
        let mut line = Vec::with_capacity(count);
        for _ in 0..count {
            line.push(
                fields
                    .next()
                    .ok_or("declared plugin lines are truncated")?
                    .to_owned(),
            );
        }
        lines.push(line);
    }
    Ok(lines)
}

fn read_declared(state: &Path) -> Result<Vec<Vec<String>>> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(state.join("declared"))
        .map_err(|error| -> Box<dyn std::error::Error> {
            if error.kind() == std::io::ErrorKind::NotFound {
                "no declared plugin lines; run binit sync inside a shell that sourced binit init"
                    .into()
            } else {
                error.into()
            }
        })?;
    let metadata = file.metadata()?;
    if !metadata.is_file()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.permissions().mode() & 0o077 != 0
    {
        return Err("declared list must be a private regular file owned by you".into());
    }
    if metadata.len() > DECLARED_MAX {
        return Err("declared list exceeds 256 KiB".into());
    }
    let mut bytes = Vec::new();
    file.take(DECLARED_MAX + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > DECLARED_MAX {
        return Err("declared list exceeds 256 KiB".into());
    }
    parse_declared(&bytes)
}

/// Reads the list a shell sends on standard input and checks it, so neither
/// `declare` nor `sync` stores a record this binary would refuse.
fn declared_input() -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    std::io::stdin()
        .take(DECLARED_MAX + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 > DECLARED_MAX {
        return Err("declared list exceeds 256 KiB".into());
    }
    parse_declared(&bytes)?;
    Ok(bytes)
}

pub(super) fn declare(state: &Path) -> Result<()> {
    atomic_write(&state.join("declared"), &declared_input()?)
}

fn kept_git_pin(
    old: &[Installed],
    argv: &[String],
    remote: &git::Remote,
    ver: &Option<String>,
) -> Option<String> {
    let wanted = argv[1..]
        .iter()
        .find_map(|arg| arg.strip_prefix("id="))
        .unwrap_or(&remote.name);
    old.iter().find_map(|entry| {
        let origin = entry.origin.as_ref()?;
        (entry.plugin.id == wanted
            && git::normalize(&origin.url) == git::normalize(&remote.url)
            && &origin.ver == ver)
            .then(|| origin.commit.clone())
    })
}

/// With `from_shell`, the invoking shell sends its lines on standard input and
/// they are recorded under the mutation lock, so another shell writing its own
/// list meanwhile cannot change what this sync publishes.
pub(super) fn sync_with_jobs(
    state: &Path,
    jobs: usize,
    from_shell: bool,
    stage: bool,
) -> Result<()> {
    let input = if from_shell {
        Some(declared_input()?)
    } else {
        None
    };
    let _guard = MutationGuard::acquire(state)?;
    let base = if stage {
        candidate::selected_id(state)?
    } else {
        None
    };
    git::clean_leftovers(state);
    let lines = match input {
        Some(bytes) => {
            if !stage {
                atomic_write(&state.join("declared"), &bytes)?;
            }
            parse_declared(&bytes)?
        }
        None => read_declared(state)?,
    };
    let mut plan = current(state)?;
    let previous = plan.packages.clone();
    let old: Vec<Installed> = plan
        .catalog
        .iter()
        .filter(|e| !e.declaration.is_empty())
        .cloned()
        .collect();
    let mut requests = Vec::new();
    for argv in &lines {
        package::check_keys(argv)?;
        if remote::is_source(&argv[0])
            || old
                .iter()
                .any(|entry| entry.origin.is_some() && entry.declaration == *argv)
        {
            continue;
        }
        let Some(remote) =
            git::parse_source(&argv[0]).map_err(|error| git::line_error(&argv[0], error))?
        else {
            continue;
        };
        let ver = git::line_ver(argv).map_err(|error| git::line_error(&argv[0], error))?;
        let kept = kept_git_pin(&old, argv, &remote, &ver);
        let refresh = kept.is_none() && !ver.as_deref().is_some_and(git::valid_commit);
        requests.push((argv[0].clone(), refresh));
    }
    let progress = (!requests.is_empty())
        .then(|| progress::Progress::start("Preparing plugin sources"))
        .flatten();
    let mirrors = prefetch_mirrors(state, requests, jobs);
    if let Some(progress) = progress {
        progress.finish();
    }
    let mirrors = mirrors?;
    for entry in &old {
        let prefix = format!("plugins/{}/", entry.plugin.id);
        plan.packages.retain(|name, _| !name.starts_with(&prefix));
    }
    plan.catalog.retain(|entry| entry.declaration.is_empty());
    for argv in &lines {
        package::check_keys(argv)?;
        let keys = package::line_keys(argv)?;
        let same_source = old.iter().any(|entry| {
            entry.declaration.first() == argv.first()
                && package::line_keys(&entry.declaration).is_ok_and(|previous| {
                    ["id", "ver", "sha256", "asset", "format"]
                        .iter()
                        .all(|key| previous.get(key) == keys.get(key))
                })
        });
        let event = if same_source { None } else { Some("atclone") };
        if remote::is_source(&argv[0])
            || (argv.iter().any(|arg| arg.starts_with("atclone="))
                && old.iter().any(|entry| entry.declaration == *argv))
        {
            if let Some(entry) = old.iter().find(|entry| entry.declaration == *argv) {
                if plan
                    .catalog
                    .iter()
                    .any(|item| item.plugin.id == entry.plugin.id)
                {
                    return Err(format!("plugin id is already in use: {}", entry.plugin.id).into());
                }
                let prefix = format!("plugins/{}/", entry.plugin.id);
                plan.packages.extend(
                    previous
                        .iter()
                        .filter(|(name, _)| name.starts_with(&prefix))
                        .map(|(name, bytes)| (name.clone(), bytes.clone())),
                );
                plan.catalog.push(entry.clone());
            } else {
                let (entry, files) = if remote::is_source(&argv[0]) {
                    remote_package(state, argv, event)?
                } else {
                    line_package(state, argv, event)?
                };
                if plan
                    .catalog
                    .iter()
                    .any(|item| item.plugin.id == entry.plugin.id)
                {
                    return Err(format!("plugin id is already in use: {}", entry.plugin.id).into());
                }
                plan.catalog.push(entry);
                plan.packages.extend(files);
            }
            continue;
        }
        let wrap = |error: Box<dyn std::error::Error>| -> Box<dyn std::error::Error> {
            git::line_error(&argv[0], error)
        };
        let (item, files) = match git::parse_source(&argv[0]).map_err(wrap)? {
            None => line_package(state, argv, event).map_err(wrap)?,
            // A line that is already synced keeps its pin and its snapshot.
            Some(remote) => match old
                .iter()
                .find(|entry| entry.origin.is_some() && entry.declaration == *argv)
            {
                Some(entry) => {
                    let prefix = format!("plugins/{}/", entry.plugin.id);
                    let files = previous
                        .iter()
                        .filter(|(name, _)| name.starts_with(&prefix))
                        .map(|(name, bytes)| (name.clone(), bytes.clone()))
                        .collect();
                    (entry.clone(), files)
                }
                None => {
                    package::check_keys(argv).map_err(wrap)?;
                    let ver = git::line_ver(argv).map_err(wrap)?;
                    // A line keeps its commit while its id, source and ver stay
                    // the same. Any other change is a new choice and resolves
                    // the current commit.
                    let kept = kept_git_pin(&old, argv, &remote, &ver);
                    let (mirror, fresh) = mirrors
                        .get(&git::normalize(&remote.url))
                        .ok_or("missing prepared mirror")?;
                    let commit = match kept {
                        Some(commit) => commit,
                        None => {
                            git::pin(mirror, &remote.url, ver.as_deref(), *fresh).map_err(wrap)?
                        }
                    };
                    git_package(state, argv, &remote, mirror, &commit, event).map_err(wrap)?
                }
            },
        };
        let id = &item.plugin.id;
        if plan.catalog.iter().any(|entry| entry.plugin.id == *id) {
            return Err(format!(
                "plugin line {}: id {id} is already in use; add id=NAME",
                git::redact(&argv[0])
            )
            .into());
        }
        plan.catalog.push(item);
        plan.packages.extend(files);
    }
    if stage {
        return stage_plan(state, plan, base);
    }
    let previous = pointer(state, "current").ok();
    let generation = publish(plan.catalog, plan.packages, state)?;
    // Silent when new shells get the generation they already had.
    if previous.as_deref() == Some(generation.as_str()) {
        return Ok(());
    }
    println!(
        "synced {} plugin line{} into {}; new shells use it",
        lines.len(),
        if lines.len() == 1 { "" } else { "s" },
        &generation[..12]
    );
    Ok(())
}
