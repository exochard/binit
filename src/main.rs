use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};

mod engine;
mod manager;
mod package;

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
const MAX_BYTES: u64 = 2 * 1024 * 1024;
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
    requires: Vec<String>,
    #[serde(default)]
    depends: Vec<String>,
    #[serde(default)]
    completions: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    commands: Vec<String>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Lock {
    schema: u32,
    plugins: Vec<Plugin>,
    #[serde(default)]
    catalog: Vec<manager::Installed>,
    files: BTreeMap<String, String>,
}

#[derive(Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct Project {
    #[serde(default)]
    label: String,
    #[serde(default)]
    env: BTreeMap<String, String>,
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
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_file() || meta.len() > MAX_BYTES {
        return Err(format!(
            "expected a regular file of at most 2 MiB: {}",
            path.display()
        )
        .into());
    }
    let mut bytes = Vec::new();
    OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?
        .take(MAX_BYTES + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_BYTES {
        return Err("file exceeds 2 MiB".into());
    }
    Ok(bytes)
}
fn private_dir(path: &Path) -> Result<()> {
    if !path.exists() {
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(path)?;
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

fn atomic_write(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path.parent().ok_or("missing parent")?;
    private_dir(parent)?;
    let temp = parent.join(format!(".write-{}-{}", std::process::id(), unique()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temp)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    fs::rename(temp, path)?;
    Ok(())
}
fn unique() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
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
fn validate_plugin(plugin: &Plugin) -> Result<()> {
    if plugin.schema != 1
        || !valid_name(&plugin.id)
        || !valid_relative(&plugin.entry)
        || !plugin.entry.ends_with(".bash")
        || !["startup", "prompt", "completion", "command"].contains(&plugin.load.as_str())
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
    let config: Config = toml::from_str(&text(&config_path)?)?;
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
    publish(catalog, packages, state)
}

fn publish(
    catalog: Vec<manager::Installed>,
    mut packages: BTreeMap<String, Vec<u8>>,
    state: &Path,
) -> Result<String> {
    if catalog.len() > 64 {
        return Err("at most 64 plugins are supported".into());
    }
    let mut plugins = BTreeMap::new();
    let mut completions = BTreeSet::new();
    let mut commands = BTreeSet::new();
    for item in &catalog {
        validate_plugin(&item.plugin)?;
        if !item.enabled {
            continue;
        }
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
    private_dir(state)?;
    let state = fs::canonicalize(state)?;
    let executable = std::env::current_exe()?;
    let mut init = format!(
        "# Generated initialization plan; changes require prepare.\n[[ $- == *i* && ${{BINIT_SAFE-0}} != 1 ]] || return 0\n[[ ${{__binit_active-0}} != 1 ]] || return 0\nBINIT_STATE_DIR={}\n__binit_manager={}\n__binit_generation_dir=${{BASH_SOURCE[0]%/*}}\nsource \"$__binit_generation_dir/runtime.bash\" || return\n",
        quote(state.to_str().ok_or("state path must be UTF-8")?),
        quote(executable.to_str().ok_or("binary path must be UTF-8")?)
    );
    for id in &order {
        let p = &plugins[id];
        init.push_str(&format!(
            "__binit_register {} \"$__binit_generation_dir\"/{} {} {} {} {} {}\n",
            quote(id),
            quote(&format!("plugins/{id}/{}", p.entry)),
            quote(&p.load),
            quote(&p.requires.join(" ")),
            quote(&p.depends.join(" ")),
            quote(&p.completions.join(" ")),
            quote(&p.commands.join(" "))
        ));
    }
    init.push_str("__binit_activate\n");
    packages.insert("init.bash".into(), init.into_bytes());
    packages.insert(
        "runtime.bash".into(),
        include_bytes!("../runtime/core.bash").to_vec(),
    );
    #[cfg(feature = "completion-menu")]
    packages.insert(
        "completion.so".into(),
        include_bytes!(concat!(env!("OUT_DIR"), "/completion.so")).to_vec(),
    );
    let lock = Lock {
        schema: 2,
        plugins: order.iter().map(|id| plugins[id].clone()).collect(),
        catalog,
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
        for (name, bytes) in packages {
            atomic_write(&staging.join(name), &bytes)?;
        }
        atomic_write(&staging.join("lock.toml"), &lock_bytes)?;
        fs::rename(staging, &destination)?;
    }
    verify_generation(&state, &generation)?;
    match pointer(&state, "current") {
        Ok(current) if current != generation => {
            atomic_write(&state.join("previous"), current.as_bytes())?;
        }
        _ => {}
    }
    atomic_write(&state.join("current"), generation.as_bytes())?;
    Ok(generation)
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
fn verify_generation(state: &Path, id: &str) -> Result<Lock> {
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
        if hash(&read_regular(&path)?) != *expected {
            return Err(format!("content changed: {file}").into());
        }
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
fn trust(state: &Path, directory: &Path) -> Result<()> {
    let (path, bytes, _) = read_project(directory)?;
    let path = path
        .to_str()
        .ok_or("project path must be UTF-8")?
        .to_owned();
    private_dir(state)?;
    private_dir(&state.join("trust"))?;
    let record = Trust {
        path: path.clone(),
        sha256: hash(&bytes),
    };
    atomic_write(
        &state
            .join("trust")
            .join(format!("{}.toml", hash(path.as_bytes()))),
        toml::to_string_pretty(&record)?.as_bytes(),
    )?;
    println!("Trusted {path} at {}", record.sha256);
    Ok(())
}
fn project(state: &Path, directory: &Path) -> Result<()> {
    let (path, bytes, project) = read_project(directory)?;
    let name = path.to_str().ok_or("project path must be UTF-8")?;
    private_dir(state)?;
    private_dir(&state.join("trust"))?;
    let record: Trust = toml::from_str(&text(
        &state
            .join("trust")
            .join(format!("{}.toml", hash(name.as_bytes()))),
    )?)?;
    if record.path != name || record.sha256 != hash(&bytes) {
        return Err("project content changed; review it and trust it again".into());
    }
    let mut out = std::io::stdout().lock();
    write!(out, ":label\0{}\0", project.label)?;
    for (key, value) in project.env {
        write!(out, "{key}\0{value}\0")?;
    }
    Ok(())
}

fn run() -> Result<()> {
    let raw_args = std::env::args_os().skip(1).collect::<Vec<_>>();
    if raw_args.first().is_some_and(|arg| arg == "run") {
        return engine::run(&raw_args[1..]);
    }
    let mut args = std::env::args().skip(1).collect::<Vec<_>>();
    if args.is_empty() || args.iter().any(|arg| arg == "--help" || arg == "-h") {
        println!(
            "Binit: Bash environment and plugin framework\n\nUsage: binit COMMAND [ARG] --state DIRECTORY\n       binit run [--engine BASH] [--] SCRIPT [ARG...]\n\n  run SCRIPT [ARG...]    Run a script with an explicitly selected Bash\n  add PATH [--disabled]   Install a local package or declaration\n  enable ID              Enable a plugin for the next shell\n  disable ID             Disable a plugin for the next shell\n  remove ID              Remove a plugin from the next shell\n  update ID [PATH]        Snapshot reviewed source changes\n  plugins                List installed and disabled plugins\n  inspect ID             Show source, dependencies and entry digest\n  shell                  Open Bash with the verified plan\n  apply                  Refresh the runtime using pinned plugins\n  prepare CONFIG   Snapshot local plugins and activate a loading plan\n  init             Verify the plan and print its Bash source command\n  status           Verify and list the active plugins\n  rollback         Activate the previous verified plan for new shells\n  trust DIRECTORY  Approve the current .binit-project.toml content\n  project DIRECTORY  Emit trusted project data (NUL-delimited)\n\nInside an initialized shell: binit profile, binit load ID, binit completion [on|off|status]\nRecovery: BINIT_SAFE=1 bash --rcfile PATH/TO/init.bash -i"
        );
        return Ok(());
    }
    let pos = args
        .iter()
        .position(|s| s == "--state")
        .ok_or("--state DIRECTORY is required")?;
    if pos + 1 >= args.len() {
        return Err("--state needs a directory".into());
    }
    let state = PathBuf::from(args.remove(pos + 1));
    args.remove(pos);
    if args.iter().any(|s| s == "--state") {
        return Err("--state may only be specified once".into());
    }
    let command = args.first().ok_or("missing command")?.as_str();
    match (command, args.len()) {
        ("add", 2) => manager::add(&state, Path::new(&args[1]), true)?,
        ("add", 3) if args[2] == "--disabled" => manager::add(&state, Path::new(&args[1]), false)?,
        ("enable" | "disable" | "remove", 2) => manager::change(&state, &args[1], command)?,
        ("update", 2) => manager::update(&state, &args[1], None)?,
        ("update", 3) => manager::update(&state, &args[1], Some(Path::new(&args[2])))?,
        ("plugins", 1) => manager::list(&state)?,
        ("inspect", 2) => manager::inspect(&state, &args[1])?,
        ("shell", 1) => manager::shell(&state)?,
        ("apply", 1) => manager::apply(&state)?,
        ("prepare", 2) => {
            let _guard = manager::MutationGuard::acquire(&state)?;
            println!("Prepared {}", prepare(Path::new(&args[1]), &state)?);
        }
        ("init", 1) => {
            let id = pointer(&state, "current")?;
            verify_generation(&state, &id)?;
            let path = fs::canonicalize(state.join("generations").join(id).join("init.bash"))?;
            println!(
                "source {}",
                quote(path.to_str().ok_or("state path must be UTF-8")?)
            );
        }
        ("status", 1) => {
            let id = pointer(&state, "current")?;
            let lock = verify_generation(&state, &id)?;
            println!("Generation {id}\n{:20} {:12} REQUIRES", "PLUGIN", "LOAD");
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
            println!("Activated {previous}; start a new shell to use it");
        }
        ("trust", 2) => trust(&state, Path::new(&args[1]))?,
        ("project", 2) => project(&state, Path::new(&args[1]))?,
        _ => return Err("unknown command or argument count; see binit --help".into()),
    }
    Ok(())
}
fn main() {
    if let Err(error) = run() {
        eprintln!("binit: {error}");
        std::process::exit(1);
    }
}
