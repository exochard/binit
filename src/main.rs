use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};

mod acquire;
mod candidate;
mod checks;
mod completion_worker;
mod diagnose;
mod engine;
mod extensions;
mod git;
mod help;
mod hooks;
mod init_stamp;
mod lazy_runtime;
mod manager;
mod package;
mod process;
mod publication;
mod releases;
mod remote;
mod trace;

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
const MAX_BYTES: u64 = 2 * 1024 * 1024;
/// Binit's own native module, which the binary embeds; a debug build with a grammar exceeds 2 MiB.
const MAX_MODULE_BYTES: u64 = 16 * 1024 * 1024;
const MAX_FILES: usize = 128;
const PROJECT_FILE: &str = ".binit-project.toml";

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Config {
    schema: u32,
    plugins: Vec<PluginPath>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct PluginPath {
    path: PathBuf,
    #[serde(default = "yes")]
    enabled: bool,
}
fn yes() -> bool {
    true
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Plugin {
    schema: u32,
    id: String,
    entry: String,
    load: String,
    #[serde(default)]
    delay_ms: u64,
    #[serde(default)]
    event: String,
    #[serde(default)]
    when: String,
    #[serde(default)]
    watch: Vec<String>,
    #[serde(default)]
    requires: Vec<String>,
    #[serde(default)]
    depends: Vec<String>,
    #[serde(default)]
    completions: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    commands: Vec<String>,
    /// `PATH:NAME` items: package files exposed as programs on PATH.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    bin: Vec<String>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Lock {
    schema: u32,
    plugins: Vec<Plugin>,
    #[serde(default)]
    catalog: Vec<manager::Installed>,
    /// Program name to the plugin that provides it.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    bin: BTreeMap<String, String>,
    files: BTreeMap<String, String>,
}

#[derive(Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct Project {
    #[serde(default)]
    label: String,
    #[serde(default)]
    env: BTreeMap<String, String>,
    #[serde(default)]
    path_prepend: Vec<String>,
    #[serde(default)]
    python_venv: Option<String>,
    #[serde(default)]
    node_bin: bool,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Trust {
    path: String,
    sha256: String,
}

fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn quote(text: &str) -> String {
    format!("'{}'", text.replace('\'', "'\\''"))
}
fn text(path: &Path) -> Result<String> {
    let bytes = read_regular(path)?;
    Ok(String::from_utf8(bytes)?)
}
fn read_regular(path: &Path) -> Result<Vec<u8>> {
    read_regular_limit(path, MAX_BYTES)
}
fn read_regular_limit(path: &Path, limit: u64) -> Result<Vec<u8>> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_file() || meta.len() > limit {
        return Err(format!(
            "expected a regular file of at most {} MiB: {}",
            limit / (1024 * 1024),
            path.display()
        )
        .into());
    }
    let mut bytes = Vec::new();
    OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?
        .take(limit + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 > limit {
        return Err("file exceeds size limit".into());
    }
    Ok(bytes)
}

fn snapshot_limit(file: &str, lock: &Lock) -> u64 {
    if matches!(file, "completion.so" | "completion-5.2.so") {
        return MAX_MODULE_BYTES;
    }
    let program = file
        .strip_prefix("bin/")
        .is_some_and(|name| lock.bin.contains_key(name))
        || lock.plugins.iter().any(|plugin| {
            file.strip_prefix(&format!("plugins/{}/", plugin.id))
                .is_some_and(|path| {
                    plugin
                        .bin
                        .iter()
                        .any(|item| split_bin(item).is_some_and(|(entry, _)| entry == path))
                })
        });
    if program {
        acquire::MAX_PROGRAM_BYTES
    } else {
        MAX_BYTES
    }
}
fn snapshot_file(root: &Path, file: &str, lock: &Lock) -> Result<Vec<u8>> {
    read_regular_limit(&root.join(file), snapshot_limit(file, lock))
}
fn snapshot_hash(root: &Path, file: &str, lock: &Lock) -> Result<String> {
    let limit = snapshot_limit(file, lock);
    let path = root.join(file);
    let meta = fs::symlink_metadata(&path)?;
    if !meta.is_file() || meta.len() > limit {
        return Err(format!(
            "expected a regular file of at most {} MiB: {}",
            limit / (1024 * 1024),
            path.display()
        )
        .into());
    }
    let mut input = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?
        .take(limit + 1);
    let mut digest = Sha256::new();
    let mut buffer = [0; 16384];
    let mut total = 0u64;
    loop {
        let count = match input.read(&mut buffer) {
            Ok(count) => count,
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error.into()),
        };
        if count == 0 {
            break;
        }
        total += count as u64;
        if total > limit {
            return Err("file exceeds size limit".into());
        }
        digest.update(&buffer[..count]);
    }
    Ok(format!("{:x}", digest.finalize()))
}
fn private_dir(path: &Path) -> Result<()> {
    if !path.exists() {
        // Only the state directory is private; its parents keep their usual mode.
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::DirBuilder::new().mode(0o700).create(path)?;
    }
    let meta = fs::symlink_metadata(path)?;
    // State contains executable startup files and must belong to this user.
    let current_uid = unsafe { libc::geteuid() };
    if !meta.is_dir() || meta.uid() != current_uid || meta.permissions().mode() & 0o077 != 0 {
        return Err(format!(
            "state directory must be a private directory (mode 700): {}",
            path.display()
        )
        .into());
    }
    Ok(())
}
use std::os::unix::fs::DirBuilderExt;

/// Tests that hand a temporary directory to code requiring a private state
/// directory must not depend on the caller's umask.
#[cfg(test)]
pub(crate) fn private_tempdir() -> tempfile::TempDir {
    use std::os::unix::fs::PermissionsExt;
    tempfile::Builder::new()
        .permissions(fs::Permissions::from_mode(0o700))
        .tempdir()
        .unwrap()
}

fn atomic_write(path: &Path, bytes: &[u8]) -> Result<()> {
    atomic_write_mode(path, bytes, 0o600)
}
fn atomic_write_mode(path: &Path, bytes: &[u8], mode: u32) -> Result<()> {
    write_atomic(path, bytes, mode, true)
}
fn write_atomic(path: &Path, bytes: &[u8], mode: u32, sync: bool) -> Result<()> {
    let parent = path.parent().ok_or("missing parent")?;
    private_dir(parent)?;
    let temp = parent.join(format!(".write-{}-{}", std::process::id(), unique()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(mode)
        .open(&temp)?;
    file.write_all(bytes)?;
    // The umask must not change the mode that verification expects.
    file.set_permissions(fs::Permissions::from_mode(mode))?;
    if sync {
        file.sync_all()?;
    }
    fs::rename(temp, path)?;
    Ok(())
}
fn unique() -> u128 {
    static SEQUENCE: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let sequence = SEQUENCE.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let clock = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    (clock << 64) | u128::from(sequence)
}
fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 64
        && name.as_bytes()[0].is_ascii_alphabetic()
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}
fn valid_relative(path: &str) -> bool {
    !path.is_empty()
        && Path::new(path)
            .components()
            .all(|c| matches!(c, Component::Normal(_)))
        && !path.contains(['\n', '\r', '\0'])
}
fn valid_command_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 64
        && name.as_bytes()[0].is_ascii_alphanumeric()
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'+' | b'-'))
}
fn split_bin(item: &str) -> Option<(&str, &str)> {
    item.rsplit_once(':')
        .filter(|(path, name)| package::valid_declared_file(path) && valid_command_name(name))
}
fn validate_plugin(plugin: &Plugin) -> Result<()> {
    if plugin.schema != 1
        || !valid_name(&plugin.id)
        || !valid_relative(&plugin.entry)
        || !plugin.entry.ends_with(".bash")
        || ![
            "startup",
            "prompt",
            "wait",
            "completion",
            "command",
            "timed",
            "event",
            "watch",
        ]
        .contains(&plugin.load.as_str())
        || plugin
            .requires
            .iter()
            .chain(&plugin.depends)
            .chain(&plugin.completions)
            .chain(&plugin.commands)
            .any(|s| !valid_name(s))
        || (plugin.load == "completion") == plugin.completions.is_empty()
        || (plugin.load == "command") == plugin.commands.is_empty()
        || plugin
            .commands
            .iter()
            .any(|s| s == "binit" || s.starts_with("__binit_"))
        || plugin.delay_ms > 86_400_000
        || (!plugin.event.is_empty() && !valid_name(&plugin.event))
        || (!plugin.when.is_empty()
            && (!valid_name(&plugin.when) || plugin.when.starts_with("__binit_")))
        || (plugin.load == "event" && plugin.event.is_empty())
        || (plugin.load == "watch" && plugin.watch.is_empty())
        || plugin.watch.len() > 128
        || plugin
            .watch
            .iter()
            .any(|path| !Path::new(path).is_absolute() || path.contains([',', '\n', '\r', '\0']))
        || plugin.bin.iter().any(|s| split_bin(s).is_none())
    {
        return Err(format!("invalid plugin manifest: {}", plugin.id).into());
    }
    Ok(())
}

fn collect(root: &Path, relative: &Path, files: &mut BTreeMap<String, Vec<u8>>) -> Result<()> {
    if relative.components().count() > 16 {
        return Err("plugin directory nesting exceeds 16 levels".into());
    }
    let mut entries = fs::read_dir(root.join(relative))?.collect::<std::io::Result<Vec<_>>>()?;
    entries.sort_by_key(|entry| entry.file_name());
    for entry in entries {
        // A local checkout is a package source, not a snapshot of Git's database.
        if entry.file_name() == ".git" {
            continue;
        }
        let rel = relative.join(entry.file_name());
        let name = rel.to_str().ok_or("plugin paths must be UTF-8")?;
        if !valid_relative(name) {
            return Err("invalid package path".into());
        }
        let meta = fs::symlink_metadata(entry.path())?;
        if meta.is_dir() {
            collect(root, &rel, files)?;
        } else if meta.is_file() {
            files.insert(name.to_owned(), read_regular(&entry.path())?);
        } else {
            return Err(format!(
                "symlinks and special files are not supported: {}",
                entry.path().display()
            )
            .into());
        }
        if files.len() > MAX_FILES
            || files.values().map(Vec::len).sum::<usize>() > MAX_BYTES as usize
        {
            return Err("plugin package exceeds 128 files or 2 MiB".into());
        }
    }
    Ok(())
}

fn visit(
    id: &str,
    plugins: &BTreeMap<String, Plugin>,
    pending: &mut BTreeSet<String>,
    done: &mut BTreeSet<String>,
    order: &mut Vec<String>,
) -> Result<()> {
    if done.contains(id) {
        return Ok(());
    }
    let plugin = plugins
        .get(id)
        .ok_or_else(|| format!("missing dependency: {id}"))?;
    if !pending.insert(id.to_owned()) {
        return Err(format!("dependency cycle at {id}").into());
    }
    for dependency in &plugin.depends {
        visit(dependency, plugins, pending, done, order)?;
    }
    pending.remove(id);
    done.insert(id.to_owned());
    order.push(id.to_owned());
    Ok(())
}

fn prepare(config: &Path, state: &Path) -> Result<String> {
    let config_path = fs::canonicalize(config)?;
    let bytes = read_regular(&config_path)?;
    let generation = install_config(&config_path, &bytes, state)?;
    select_generation(state, &generation)?;
    Ok(generation)
}

fn install_config(config_path: &Path, bytes: &[u8], state: &Path) -> Result<String> {
    let config: Config = toml::from_str(std::str::from_utf8(bytes)?)?;
    if config.schema != 1 {
        return Err("unsupported config schema".into());
    }
    if config.plugins.len() > 64 {
        return Err("at most 64 plugins are supported".into());
    }
    let mut plugins = BTreeMap::new();
    let mut packages = BTreeMap::new();
    let mut catalog = Vec::new();
    for spec in config.plugins {
        let path = config_path.parent().unwrap().join(spec.path);
        let (item, files) = manager::package(&path, spec.enabled)?;
        let plugin = &item.plugin;
        if plugins.contains_key(&plugin.id) {
            return Err(format!("duplicate plugin: {}", plugin.id).into());
        }
        plugins.insert(plugin.id.clone(), plugin.clone());
        catalog.push(item);
        packages.extend(files);
    }
    install_generation(catalog, packages, state)
}

/// Byte-length-prefixed arguments; the runtime builds the same key from a line.
/// Registers every plugin of a generation with one compound assignment per runtime table, which
/// Bash runs far faster than one function call per plugin. The tables match what the runtime's
/// per-plugin defaults would hold.
fn registration(order: &[String], plugins: &BTreeMap<String, Plugin>) -> String {
    if order.is_empty() {
        return String::new();
    }
    let mut ids = String::from("__binit_ids+=(");
    let mut tables: Vec<String> = [
        "entry",
        "load_kind",
        "requires",
        "depends",
        "state",
        "load_us",
        "hook_us",
        "hook_calls",
        "commands",
        "completions",
        "reason",
        "delay_ms",
        "event",
        "when",
        "watch",
    ]
    .iter()
    .map(|name| format!("__binit_{name}+=("))
    .collect();
    let mut owners = String::new();
    for id in order {
        let p = &plugins[id];
        let key = quote(id);
        ids.push_str(&format!(" {key}"));
        let values = [
            format!(
                "\"$__binit_generation_dir\"/{}",
                quote(&format!("plugins/{id}/{}", p.entry))
            ),
            quote(&p.load),
            quote(&p.requires.join(" ")),
            quote(&p.depends.join(" ")),
            "pending".into(),
            "0".into(),
            "0".into(),
            "0".into(),
            quote(&p.commands.join(" ")),
            quote(&p.completions.join(" ")),
            "''".into(),
            p.delay_ms.to_string(),
            quote(&p.event),
            quote(&p.when),
            quote(&p.watch.join(",")),
        ];
        for (table, value) in tables.iter_mut().zip(values) {
            table.push_str(&format!(" [{key}]={value}"));
        }
        for command in &p.completions {
            owners.push_str(&format!(
                "__binit_completion_owner[{}]={key}\n",
                quote(command)
            ));
        }
        for command in &p.commands {
            owners.push_str(&format!(
                "__binit_command_owner[{}]={key}\n",
                quote(command)
            ));
        }
    }
    let mut text = ids + " )\n";
    for table in tables {
        text.push_str(&table);
        text.push_str(" )\n");
    }
    text + &owners
}
fn line_key(declaration: &[String]) -> String {
    declaration
        .iter()
        .map(|argument| format!("{}:{argument}", argument.len()))
        .collect()
}

fn publish(
    catalog: Vec<manager::Installed>,
    packages: BTreeMap<String, Vec<u8>>,
    state: &Path,
) -> Result<String> {
    let generation = install_generation(catalog, packages, state)?;
    select_generation(state, &generation)?;
    Ok(generation)
}

fn install_generation(
    catalog: Vec<manager::Installed>,
    mut packages: BTreeMap<String, Vec<u8>>,
    state: &Path,
) -> Result<String> {
    if catalog.len() > 64 {
        return Err("at most 64 plugins are supported".into());
    }
    let mut plugins = BTreeMap::new();
    let mut ids = BTreeSet::new();
    let mut completions = BTreeSet::new();
    let mut commands = BTreeSet::new();
    for item in &catalog {
        validate_plugin(&item.plugin)?;
        if !ids.insert(item.plugin.id.as_str()) {
            return Err(format!("duplicate plugin id: {}", item.plugin.id).into());
        }
        if !item.enabled {
            continue;
        }
        package::check_snapshot_minimum(&item.plugin.id, &packages)?;
        for command in &item.plugin.completions {
            if !completions.insert(command.clone()) {
                return Err(format!("duplicate completion: {command}").into());
            }
        }
        for command in &item.plugin.commands {
            if !commands.insert(command.clone()) {
                return Err(format!("duplicate command trigger: {command}").into());
            }
        }
        plugins.insert(item.plugin.id.clone(), item.plugin.clone());
    }
    let mut order = Vec::new();
    let mut pending = BTreeSet::new();
    let mut done = BTreeSet::new();
    for id in plugins.keys() {
        visit(id, &plugins, &mut pending, &mut done, &mut order)?;
    }
    // Programs of enabled plugins are copied to bin/NAME in the generation.
    let mut bin = BTreeMap::new();
    for item in catalog.iter().filter(|item| item.enabled) {
        let id = &item.plugin.id;
        for entry in &item.plugin.bin {
            let (path, name) = split_bin(entry).ok_or("invalid bin entry")?;
            let bytes = packages
                .get(&format!("plugins/{id}/{path}"))
                .ok_or_else(|| format!("plugin {id}: bin file is missing: {path}"))?
                .clone();
            if let Some(other) = bin.insert(name.to_owned(), id.clone()) {
                return Err(format!(
                    "duplicate command {name}: provided by plugins {other} and {id}"
                )
                .into());
            }
            packages.insert(format!("bin/{name}"), bytes);
        }
    }
    private_dir(state)?;
    let state = fs::canonicalize(state)?;
    let executable = std::env::current_exe()?;
    let mut init = format!(
        "# Generated initialization plan; changes require prepare.\n[[ $- == *i* && ${{BINIT_SAFE-0}} != 1 ]] || return 0\n[[ ${{__binit_active-0}} != 1 ]] || return 0\nBINIT_STATE_DIR={}\nif [[ -n ${{__binit_check_state-}} ]]; then BINIT_STATE_DIR=$__binit_check_state; fi\n__binit_manager={}\n__binit_generation_dir=${{BASH_SOURCE[0]%/*}}\n[[ ${{BASH_SOURCE[0]}} == */* ]] || __binit_generation_dir=.\n[[ $__binit_generation_dir == /* ]] || __binit_generation_dir=$PWD/$__binit_generation_dir\nsource \"$__binit_generation_dir/runtime.bash\" || return\n",
        quote(state.to_str().ok_or("state path must be UTF-8")?),
        quote(executable.to_str().ok_or("binary path must be UTF-8")?)
    );
    init.push_str(if cfg!(binit_dual_native) {
        "__binit_native_layout=dual\n"
    } else {
        "__binit_native_layout=single\n"
    });
    if !bin.is_empty() {
        let directory = state.join("generations");
        if directory
            .to_str()
            .ok_or("state path must be UTF-8")?
            .contains(':')
        {
            return Err("the state path must not contain ':' when plugins provide commands".into());
        }
        init.push_str(
            "case :$PATH: in *:\"$__binit_generation_dir/bin\":*) ;; *) PATH=$__binit_generation_dir/bin${PATH:+:$PATH} ;; esac\n__binit_has_bin=1\n",
        );
    }
    init.push_str(&registration(&order, &plugins));
    let mut activate = Vec::new();
    for id in &order {
        let declaration = catalog
            .iter()
            .find(|item| item.plugin.id == *id)
            .map(|item| item.declaration.as_slice())
            .unwrap_or_default();
        if declaration.is_empty() {
            activate.push(quote(id));
        } else {
            init.push_str(&format!(
                "__binit_declare_line {} {}\n",
                quote(&line_key(declaration)),
                quote(id)
            ));
        }
    }
    // Startup plugins may initialize editor capabilities before prompt hooks run.
    init.push_str("if [[ ${BINIT_COMPLETION_MENU-1} == 1 ]]; then __binit_load_completion >/dev/null 2>&1 || :; fi\n");
    if !activate.is_empty() {
        init.push_str(&format!(
            "__binit_activate_plugins {}\n",
            activate.join(" ")
        ));
    }
    init.push_str("__binit_activate_shell\n");
    packages.insert("init.bash".into(), init.into_bytes());
    let (runtime, deferred) = lazy_runtime::split(lazy_runtime::SOURCE)?;
    let runtime = runtime + include_str!("../runtime/completion-module.bash");
    packages.insert("runtime.bash".into(), runtime.into_bytes());
    for (name, text) in deferred {
        packages.insert(name, text.into_bytes());
    }
    #[cfg(feature = "completion-menu")]
    packages.insert(
        "completion.so".into(),
        include_bytes!(concat!(env!("OUT_DIR"), "/completion.so")).to_vec(),
    );
    #[cfg(binit_dual_native)]
    packages.insert(
        "completion-5.2.so".into(),
        include_bytes!(concat!(env!("OUT_DIR"), "/completion-5.2.so")).to_vec(),
    );
    let lock = Lock {
        schema: 2,
        plugins: order.iter().map(|id| plugins[id].clone()).collect(),
        catalog,
        bin,
        files: packages
            .iter()
            .map(|(name, bytes)| (name.clone(), hash(bytes)))
            .collect(),
    };
    let lock_bytes = toml::to_string_pretty(&lock)?.into_bytes();
    let generation = hash(&lock_bytes);
    private_dir(&state.join("generations"))?;
    let destination = state.join("generations").join(&generation);
    if !destination.exists() {
        let staging =
            state
                .join("generations")
                .join(format!(".staging-{}-{}", std::process::id(), unique()));
        private_dir(&staging)?;
        publication::write_staging(&staging, packages, &lock_bytes)?;
        fs::rename(staging, &destination)?;
    }
    verify_generation(&state, &generation)?;
    Ok(generation)
}

// The caller holds the mutation lock and has verified this generation.
fn select_generation(state: &Path, generation: &str) -> Result<()> {
    let state = fs::canonicalize(state)?;
    match pointer(&state, "current") {
        Ok(current) if current != generation => {
            atomic_write(&state.join("previous"), current.as_bytes())?;
        }
        _ => {}
    }
    atomic_write(&state.join("current"), generation.as_bytes())?;
    Ok(())
}

fn valid_digest(digest: &str) -> bool {
    digest.len() == 64
        && digest
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}
fn pointer(state: &Path, name: &str) -> Result<String> {
    let value = text(&state.join(name))?;
    if !valid_digest(&value) {
        return Err(format!("invalid {name} generation pointer").into());
    }
    Ok(value)
}
/// Full verification. A failure also drops the generation's init stamp, so `binit init` cannot keep
/// skipping checks that any other command has seen fail.
fn verify_generation(state: &Path, id: &str) -> Result<Lock> {
    let result = verify_generation_checks(state, id);
    if result.is_err() && valid_digest(id) {
        init_stamp::forget(state, id);
    }
    result
}

fn verify_generation_checks(state: &Path, id: &str) -> Result<Lock> {
    if !valid_digest(id) {
        return Err("invalid generation".into());
    }
    private_dir(state)?;
    private_dir(&state.join("generations"))?;
    let root = state.join("generations").join(id);
    private_dir(&root)?;
    let bytes = read_regular(&root.join("lock.toml"))?;
    if hash(&bytes) != id {
        return Err("lock content does not match generation".into());
    }
    let lock: Lock = toml::from_str(std::str::from_utf8(&bytes)?)?;
    if ![1, 2].contains(&lock.schema) {
        return Err("unsupported lock schema".into());
    }
    for origin in lock.catalog.iter().filter_map(|item| item.origin.as_ref()) {
        git::validate_origin(&origin.url, origin.ver.as_deref(), &origin.commit)?;
    }
    for item in lock.catalog.iter().filter(|item| item.asset.is_some()) {
        if remote::origin(&item.declaration)?
            != *item.asset.as_ref().ok_or("remote origin missing")?
        {
            return Err("remote origin does not match its declaration".into());
        }
    }
    for (file, expected) in &lock.files {
        if !valid_relative(file) {
            return Err("invalid locked path".into());
        }
        let mut path = root.clone();
        for component in Path::new(file).components() {
            path.push(component);
            if fs::symlink_metadata(&path)?.file_type().is_symlink() {
                return Err("symlink in generation".into());
            }
        }
        let meta = fs::symlink_metadata(&path)?;
        let program = file.strip_prefix("bin/");
        let mode = meta.permissions().mode() & 0o7777;
        let mode_ok = if program.is_some() {
            mode == 0o700
        } else {
            mode & 0o177 == 0
        };
        if meta.uid() != unsafe { libc::geteuid() } || !mode_ok {
            return Err(format!("unexpected owner or mode: {file}").into());
        }
        if snapshot_hash(&root, file, &lock)? != *expected {
            return Err(format!("content changed: {file}").into());
        }
        if let Some(name) = program {
            let owner = lock.bin.get(name).ok_or("program is not in the lock")?;
            if !valid_command_name(name) || !lock.plugins.iter().any(|plugin| plugin.id == *owner) {
                return Err(format!("invalid locked program: {name}").into());
            }
        }
    }
    let programs = lock
        .files
        .keys()
        .filter(|file| file.starts_with("bin/"))
        .count();
    if programs != lock.bin.len() {
        return Err("locked programs do not match the lock".into());
    }
    // bin/ holds exactly the locked programs and nothing else.
    match fs::symlink_metadata(root.join("bin")) {
        Ok(_) => {
            private_dir(&root.join("bin"))?;
            for entry in fs::read_dir(root.join("bin"))? {
                let name = entry?.file_name();
                let locked = name
                    .to_str()
                    .is_some_and(|name| lock.files.contains_key(&format!("bin/{name}")));
                if !locked {
                    return Err(
                        format!("unexpected file in bin: {}", name.to_string_lossy()).into(),
                    );
                }
            }
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound && programs == 0 => {}
        Err(error) => return Err(error.into()),
    }
    Ok(lock)
}

fn validate_project(project: &Project) -> Result<()> {
    if project.label.len() > 80
        || project.label.chars().any(char::is_control)
        || project.env.len() > 64
    {
        return Err("invalid project label or too many variables".into());
    }
    if project.path_prepend.len() > 16
        || (project.python_venv.is_some() && project.env.contains_key("VIRTUAL_ENV"))
    {
        return Err("too many path entries or VIRTUAL_ENV conflicts with python_venv".into());
    }
    for name in project
        .path_prepend
        .iter()
        .chain(project.python_venv.iter())
    {
        if name.is_empty()
            || name.len() > 4096
            || name.contains(':')
            || name.chars().any(char::is_control)
            || Path::new(name)
                .components()
                .any(|part| !matches!(part, Component::Normal(_) | Component::CurDir))
        {
            return Err("project helper paths must stay relative to the profile directory".into());
        }
    }
    for (key, value) in &project.env {
        let safe_key = !key.is_empty()
            && key.len() <= 64
            && key
                .bytes()
                .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_')
            && !key.as_bytes()[0].is_ascii_digit()
            && ![
                "PATH",
                "POSIXLY_CORRECT",
                "IFS",
                "ENV",
                "CDPATH",
                "SHELLOPTS",
                "BASHOPTS",
                "GLOBIGNORE",
                "PROMPT_COMMAND",
                "HOME",
                "SHELL",
                "PWD",
                "OLDPWD",
                "SHLVL",
                "RANDOM",
                "SECONDS",
                "LINENO",
                "FUNCNAME",
                "DIRSTACK",
                "PIPESTATUS",
                "EUID",
                "UID",
                "GROUPS",
                "PPID",
                "OPTIND",
                "OPTARG",
                "REPLY",
                "PS0",
                "PS1",
                "PS2",
                "PS3",
                "PS4",
            ]
            .contains(&key.as_str())
            && ![
                "BASH", "BINIT_", "HIST", "EPOCH", "COMP_", "LC_", "LD_", "DYLD_",
            ]
            .iter()
            .any(|prefix| key.starts_with(prefix))
            && key != "LANG";
        if !safe_key || value.contains('\0') || value.len() > 8192 {
            return Err(format!("unsupported project variable: {key}").into());
        }
    }
    Ok(())
}
fn read_project(directory: &Path) -> Result<(PathBuf, Vec<u8>, Project)> {
    let directory = fs::canonicalize(directory)?;
    let path = directory.join(PROJECT_FILE);
    let bytes = read_regular(&path)?;
    let project: Project = toml::from_str(std::str::from_utf8(&bytes)?)?;
    validate_project(&project)?;
    Ok((path, bytes, project))
}
/// Approves a project file and prints what was approved. With `reviewed`, approves only content
/// whose digest matches it, so a change between review and approval is refused.
fn trust(state: &Path, directory: &Path, reviewed: Option<&str>) -> Result<()> {
    let (path, bytes, project) = read_project(directory)?;
    let digest = hash(&bytes);
    if reviewed.is_some_and(|reviewed| reviewed != digest) {
        return Err(format!("trust: content changed since review: sha256 {digest}").into());
    }
    let path = path
        .to_str()
        .ok_or("project path must be UTF-8")?
        .to_owned();
    private_dir(state)?;
    private_dir(&state.join("trust"))?;
    let record = Trust {
        path: path.clone(),
        sha256: digest,
    };
    atomic_write(
        &state
            .join("trust")
            .join(format!("{}.toml", hash(path.as_bytes()))),
        toml::to_string_pretty(&record)?.as_bytes(),
    )?;
    println!(
        "trusted {} at {}",
        releases::plain_text(&path),
        record.sha256
    );
    println!("label  {}", releases::plain_text(&project.label));
    for (key, value) in &project.env {
        println!("env    {key}={}", value.escape_default());
    }
    for directory in &project.path_prepend {
        println!("path   {}", directory.escape_default());
    }
    if let Some(venv) = &project.python_venv {
        println!("venv   {}", venv.escape_default());
    }
    if project.node_bin {
        println!("node   node_modules/.bin");
    }
    Ok(())
}

fn project_helper_directory(root: &Path, path: &Path) -> Result<String> {
    let path = fs::canonicalize(root.join(path))?;
    if !path.is_dir() || !path.starts_with(root) {
        return Err("project helper directory leaves the trusted profile directory".into());
    }
    let name = path.to_str().ok_or("project helper path must be UTF-8")?;
    if name.contains(':') || name.chars().any(char::is_control) {
        return Err("project helper path contains a colon or control character".into());
    }
    Ok(name.to_owned())
}

fn project(state: &Path, directory: &Path, framed: bool) -> Result<()> {
    let directory = fs::canonicalize(directory)?;
    let root = directory.as_path();
    let mut out = std::io::stdout().lock();
    if framed {
        let file = root.join(PROJECT_FILE);
        let name = file.to_str().ok_or("project path must be UTF-8")?;
        write!(out, ":profile\0{name}\0")?;
    }
    let (path, bytes, project) = read_project(root)?;
    let name = path.to_str().ok_or("project path must be UTF-8")?;
    private_dir(state)?;
    private_dir(&state.join("trust"))?;
    let record: Trust = toml::from_str(&text(
        &state
            .join("trust")
            .join(format!("{}.toml", hash(name.as_bytes()))),
    )?)?;
    if record.path != name || record.sha256 != hash(&bytes) {
        return Err("project content changed since it was trusted".into());
    }
    if !framed
        && (!project.path_prepend.is_empty() || project.python_venv.is_some() || project.node_bin)
    {
        return Err("project helpers require --framed".into());
    }
    let mut directories = Vec::new();
    for name in &project.path_prepend {
        directories.push(project_helper_directory(root, Path::new(name))?);
    }
    let venv = project
        .python_venv
        .as_ref()
        .map(|name| {
            let root_path = Path::new(name);
            let environment = project_helper_directory(root, root_path)?;
            directories.push(project_helper_directory(root, &root_path.join("bin"))?);
            Ok::<_, Box<dyn std::error::Error>>(environment)
        })
        .transpose()?;
    let node = Path::new("node_modules/.bin");
    if project.node_bin && root.join(node).try_exists()? {
        directories.push(project_helper_directory(root, node)?);
    }
    write!(out, ":label\0{}\0", project.label)?;
    for (key, value) in project.env {
        write!(out, "{key}\0{value}\0")?;
    }
    if let Some(venv) = venv {
        write!(out, "VIRTUAL_ENV\0{venv}\0")?;
    }
    for name in directories {
        write!(out, ":path\0{name}\0")?;
    }
    if framed {
        out.write_all(b":end\0ok\0")?;
    }
    Ok(())
}

fn default_state() -> Result<PathBuf> {
    let variable = |name| std::env::var_os(name).filter(|value| !value.is_empty());
    if let Some(state) = variable("BINIT_STATE") {
        let state = PathBuf::from(state);
        if !state.is_absolute() {
            return Err("BINIT_STATE must be an absolute path".into());
        }
        return Ok(state);
    }
    if let Some(data) = variable("XDG_DATA_HOME").map(PathBuf::from)
        && data.is_absolute()
    {
        return Ok(data.join("binit"));
    }
    match variable("HOME").map(PathBuf::from) {
        Some(home) if home.is_absolute() => Ok(home.join(".local/share/binit")),
        _ => Err("HOME must be an absolute path; set BINIT_STATE or pass --state".into()),
    }
}

fn run() -> Result<()> {
    let raw_args = std::env::args_os().skip(1).collect::<Vec<_>>();
    if help::handle(&raw_args)? {
        return Ok(());
    }
    if raw_args.len() == 1 && raw_args[0] == "__complete" {
        return help::complete();
    }
    if raw_args
        .first()
        .is_some_and(|arg| arg == "__completion-worker")
    {
        return completion_worker::run(&raw_args[1..]);
    }
    if raw_args.first().is_some_and(|arg| arg == "__prompt-worker") {
        return process::run_prompt_worker(&raw_args[1..]);
    }
    if raw_args.len() == 1 && raw_args[0] == "__highlight-worker" {
        return process::run_highlight_worker();
    }
    if raw_args.len() == 1 && raw_args[0] == "__highlight-paths" {
        return process::resolve_highlight_paths();
    }
    if raw_args.first().is_some_and(|arg| arg == "run") {
        return engine::run(&raw_args[1..]);
    }
    if raw_args.first().is_some_and(|arg| arg == "extension") {
        return extensions::command(&raw_args[1..]);
    }
    let mut args = std::env::args().skip(1).collect::<Vec<_>>();
    if let [flag] = args.as_slice()
        && (flag == "--version" || flag == "-V")
    {
        println!("binit {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    if args.first().is_some_and(|s| s == "releases") {
        return releases::command(&args[1..]);
    }
    let mut jobs = 4usize;
    if let Some(position) = args.iter().position(|s| s == "--jobs") {
        if !args.first().is_some_and(|s| s == "sync" || s == "update") {
            return Err("--jobs is supported only for sync and update".into());
        }
        jobs = args
            .get(position + 1)
            .ok_or("--jobs needs a count")?
            .parse()
            .map_err(|_| "--jobs must be between 1 and 8")?;
        if !(1..=8).contains(&jobs) {
            return Err("--jobs must be between 1 and 8".into());
        }
        args.drain(position..=position + 1);
        if args.iter().any(|s| s == "--jobs") {
            return Err("--jobs may only be specified once".into());
        }
    }
    // The shell's `binit sync` sends its plugin lines on standard input.
    let mut from_shell = false;
    if let Some(position) = args.iter().position(|s| s == "--stdin") {
        if !args.first().is_some_and(|s| s == "sync") {
            return Err("--stdin is supported only for sync".into());
        }
        args.remove(position);
        from_shell = true;
    }
    let state = match args.iter().position(|s| s == "--state") {
        Some(pos) => {
            if pos + 1 >= args.len() {
                return Err("--state needs a directory".into());
            }
            let state = PathBuf::from(args.remove(pos + 1));
            args.remove(pos);
            state
        }
        None => default_state()?,
    };
    if args.iter().any(|s| s == "--state") {
        return Err("--state may only be specified once".into());
    }
    let command = args.first().ok_or("missing command")?.clone();
    let command = command.as_str();
    match (command, args.len()) {
        ("candidate", _) => candidate::command(&state, &args[1..])?,
        ("diagnose", _) => diagnose::command(&state, &args[1..])?,
        ("add", 2) => manager::add(&state, Path::new(&args[1]), true)?,
        ("add", 3) if args[2] == "--disabled" => manager::add(&state, Path::new(&args[1]), false)?,
        ("enable" | "disable" | "remove", 2) => manager::change(&state, &args[1], command)?,
        ("update", _) => manager::update_command_with_jobs(&state, &args[1..], jobs)?,
        ("plugins", 1) => manager::list(&state)?,
        ("inspect", 2) => manager::inspect(&state, &args[1])?,
        ("shell", 1) => manager::shell(&state)?,
        ("apply", 1) => manager::apply(&state)?,
        ("prepare", 2) => {
            let _guard = manager::MutationGuard::acquire(&state)?;
            println!("prepared {}", prepare(Path::new(&args[1]), &state)?);
        }
        ("sync", 1) => manager::sync_with_jobs(&state, jobs, from_shell)?,
        ("declare", 1) => manager::declare(&state)?,
        ("init", 1) => {
            if matches!(
                fs::symlink_metadata(state.join("current")),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound
            ) {
                // First use: publish an empty plan so the source line exists.
                let _guard = manager::MutationGuard::acquire(&state)?;
                if !state.join("current").exists() {
                    publish(Vec::new(), BTreeMap::new(), &state)?;
                }
            }
            let id = pointer(&state, "current")?;
            // A shell start re-hashes the generation only when its files' metadata has moved.
            private_dir(&state)?;
            private_dir(&state.join("generations"))?;
            if !valid_digest(&id) || !init_stamp::unchanged(&state, &id) {
                let started = init_stamp::now();
                let lock = verify_generation(&state, &id)?;
                // The stamp only saves time; a state directory it cannot be written to still starts.
                let _ = init_stamp::record(&state, &id, lock.files.keys(), started);
            }
            let path = fs::canonicalize(state.join("generations").join(id).join("init.bash"))?;
            println!(
                "source {}",
                quote(path.to_str().ok_or("state path must be UTF-8")?)
            );
        }
        ("status", 1) => {
            let id = pointer(&state, "current")?;
            let lock = verify_generation(&state, &id)?;
            println!("generation  {id}");
            println!("{:20} {:12} REQUIRES", "PLUGIN", "LOAD");
            for plugin in lock.plugins {
                println!(
                    "{:20} {:12} {}",
                    plugin.id,
                    plugin.load,
                    plugin.requires.join(", ")
                );
            }
        }
        ("rollback", 1) => {
            let _guard = manager::MutationGuard::acquire(&state)?;
            let previous = pointer(&state, "previous")?;
            verify_generation(&state, &previous)?;
            let current = pointer(&state, "current")?;
            atomic_write(&state.join("previous"), current.as_bytes())?;
            atomic_write(&state.join("current"), previous.as_bytes())?;
            println!("activated {previous}; new shells use it");
        }
        ("trust", 2) => trust(&state, Path::new(&args[1]), None)?,
        ("trust", 4) if args[2] == "--sha256" => {
            trust(&state, Path::new(&args[1]), Some(&args[3]))?
        }
        ("project", 2) => project(&state, Path::new(&args[1]), false)?,
        ("project", 3) if args[2] == "--framed" => project(&state, Path::new(&args[1]), true)?,
        _ => return Err("unknown command or argument count; see binit --help".into()),
    }
    Ok(())
}
/// Errors can carry plugin manifests, hook output and Git stderr, so they reach the terminal as plain
/// text. Each line is filtered, and continuation lines are indented so that text from a hook cannot
/// pass for a line of Binit's own.
fn plain_error(error: &str) -> String {
    error
        .split('\n')
        .map(releases::plain_text)
        .collect::<Vec<_>>()
        .join("\n  ")
}
fn main() {
    if let Err(error) = run() {
        eprintln!("binit: {}", plain_error(&error.to_string()));
        std::process::exit(1);
    }
}

#[cfg(test)]
mod plain_errors {
    use super::plain_error;

    #[test]
    fn continuation_lines_cannot_pass_for_binit_output() {
        assert_eq!(
            plain_error("demo: atclone failed: step\nbinit: trusted /tmp/x\r\u{1b}]0;t\u{7}"),
            "demo: atclone failed: step\n  binit: trusted /tmp/x??]0;t?"
        );
    }
}

#[cfg(test)]
mod size_limits {
    use super::*;

    #[test]
    fn the_embedded_module_has_its_own_limit_and_plugins_keep_theirs() {
        let lock = Lock {
            schema: 1,
            plugins: Vec::new(),
            catalog: Vec::new(),
            bin: BTreeMap::new(),
            files: BTreeMap::new(),
        };
        assert_eq!(snapshot_limit("completion.so", &lock), MAX_MODULE_BYTES);
        assert_eq!(snapshot_limit("completion-5.2.so", &lock), MAX_MODULE_BYTES);
        assert_eq!(snapshot_limit("plugins/x/completion.so", &lock), MAX_BYTES);
    }
}
