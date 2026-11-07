use super::*;

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Declaration {
    schema: u32,
    id: String,
    path: PathBuf,
    #[serde(default)]
    files: Vec<String>,
    load: String,
    #[serde(default)]
    requires: Vec<String>,
    #[serde(default)]
    depends: Vec<String>,
    #[serde(default)]
    completions: Vec<String>,
    #[serde(default)]
    commands: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    bin: Vec<String>,
}

/// Splits one `bin=` item into its package-relative path and command name.
fn bin_item(item: &str) -> Result<(String, String)> {
    let (path, name) = match item.rsplit_once(':') {
        Some(parts) => parts,
        None => (item, item.rsplit('/').next().unwrap_or(item)),
    };
    if !valid_declared_file(path) {
        return Err(format!("invalid bin path: {path}").into());
    }
    if !valid_command_name(name) {
        return Err(format!("invalid command name {name:?} for bin {path}; use PATH:NAME").into());
    }
    Ok((path.to_owned(), name.to_owned()))
}

fn limits(files: &BTreeMap<String, Vec<u8>>) -> Result<()> {
    if files.len() > MAX_FILES || files.values().map(Vec::len).sum::<usize>() > MAX_BYTES as usize {
        return Err("plugin package exceeds 128 files or 2 MiB".into());
    }
    Ok(())
}

fn declaration(path: &Path) -> Result<(Plugin, BTreeMap<String, Vec<u8>>)> {
    let bytes = read_regular(path)?;
    let declaration: Declaration = toml::from_str(std::str::from_utf8(&bytes)?)?;
    build(declaration, path.parent().unwrap(), bytes)
}

pub(super) fn valid_declared_file(name: &str) -> bool {
    valid_relative(name)
        && !name
            .split('/')
            .any(|part| part.is_empty() || part == "." || part == "..")
        && Path::new(name).components().count() <= 16
}

/// Copies one declared file into the package as `files/NAME` unless it is
/// already there, refusing symlinks and special files.
fn copy_declared(root: &Path, name: &str, files: &mut BTreeMap<String, Vec<u8>>) -> Result<String> {
    if !valid_declared_file(name) {
        return Err(format!("invalid declared file: {name}").into());
    }
    let mut file = root.to_owned();
    for part in Path::new(name).components() {
        file.push(part);
        if fs::symlink_metadata(&file)?.file_type().is_symlink() {
            return Err(format!("symlink in declared file: {name}").into());
        }
    }
    let selected = format!("files/{name}");
    if !files.contains_key(&selected) {
        files.insert(selected.clone(), read_regular(&file)?);
        limits(files)?;
    }
    Ok(selected)
}

// Both declaration files and plugin lines are validated and copied here.
fn build(
    declaration: Declaration,
    base: &Path,
    bytes: Vec<u8>,
) -> Result<(Plugin, BTreeMap<String, Vec<u8>>)> {
    let bin = declaration
        .bin
        .iter()
        .map(|item| bin_item(item))
        .collect::<Result<Vec<_>>>()?;
    let plugin = Plugin {
        schema: declaration.schema,
        id: declaration.id,
        entry: "entry.bash".into(),
        load: declaration.load,
        requires: declaration.requires,
        depends: declaration.depends,
        completions: declaration.completions,
        commands: declaration.commands,
        bin: bin
            .iter()
            .map(|(path, name)| format!("files/{path}:{name}"))
            .collect(),
    };
    validate_plugin(&plugin)?;
    if (declaration.files.is_empty() && bin.is_empty())
        || declaration.files.len() + bin.len() > MAX_FILES - 3
    {
        return Err("a declaration must select between 1 and 125 files".into());
    }
    let root = base.join(declaration.path);
    if !fs::symlink_metadata(&root)?.is_dir() {
        return Err("declared source must be a directory, not a symlink".into());
    }
    let root = fs::canonicalize(root)?;
    let mut files = BTreeMap::new();
    let mut entry = "# Generated from the pinned source declaration.\n".to_owned();
    let mut sourced = BTreeSet::new();
    for name in declaration.files {
        if !sourced.insert(name.clone()) {
            return Err(format!("duplicate declared file: {name}").into());
        }
        let selected = copy_declared(&root, &name, &mut files)?;
        entry.push_str(&format!(
            "source \"${{BASH_SOURCE[0]%/*}}\"/{} || return $?\n",
            quote(&selected)
        ));
    }
    for (path, _) in &bin {
        copy_declared(&root, path, &mut files)?;
    }
    files.insert("entry.bash".into(), entry.into_bytes());
    files.insert(
        "plugin.toml".into(),
        toml::to_string_pretty(&plugin)?.into_bytes(),
    );
    files.insert("declaration.toml".into(), bytes);
    limits(&files)?;
    Ok((plugin, files))
}

pub(super) fn read(path: &Path) -> Result<(Plugin, BTreeMap<String, Vec<u8>>)> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.is_file() {
        return declaration(path);
    }
    if !metadata.is_dir() {
        return Err("plugin source must be a directory or declaration file, not a symlink".into());
    }
    let mut files = BTreeMap::new();
    collect(path, Path::new(""), &mut files)?;
    let plugin: Plugin = toml::from_str(std::str::from_utf8(
        files.get("plugin.toml").ok_or("missing plugin.toml")?,
    )?)?;
    validate_plugin(&plugin)?;
    if !files.contains_key(&plugin.entry) {
        return Err(format!("missing entry for {}", plugin.id).into());
    }
    for item in &plugin.bin {
        let path = item.rsplit_once(':').map_or("", |parts| parts.0);
        if !files.contains_key(path) {
            return Err(format!("bin file is missing from {}: {path}", plugin.id).into());
        }
    }
    Ok((plugin, files))
}

const LINE_KEYS: [&str; 9] = [
    "id",
    "files",
    "bin",
    "load",
    "commands",
    "completions",
    "requires",
    "depends",
    "ver",
];

fn list(value: &str) -> Vec<String> {
    if value.is_empty() {
        Vec::new()
    } else {
        value.split(',').map(str::to_owned).collect()
    }
}

fn line_keys(argv: &[String]) -> Result<BTreeMap<&str, &str>> {
    let mut keys = BTreeMap::new();
    for argument in &argv[1..] {
        let (key, value) = argument
            .split_once('=')
            .ok_or_else(|| format!("expected key=value, found: {argument}"))?;
        if !LINE_KEYS.contains(&key) {
            return Err(format!("unknown plugin key: {key}").into());
        }
        if keys.insert(key, value).is_some() {
            return Err(format!("duplicate plugin key: {key}").into());
        }
    }
    Ok(keys)
}

/// Builds a package from the arguments of one `binit plugin` line whose
/// source is an absolute local directory.
pub(super) fn line(argv: &[String]) -> Result<(Plugin, BTreeMap<String, Vec<u8>>)> {
    let source = argv.first().ok_or("empty plugin line")?;
    let path = Path::new(source);
    if !path.is_absolute() {
        return Err(format!("plugin source must be an absolute path: {source}").into());
    }
    let keys = line_keys(argv)?;
    if keys.contains_key("ver") {
        return Err("ver= applies to Git sources only".into());
    }
    let metadata = fs::symlink_metadata(path)?;
    if keys.is_empty() && fs::symlink_metadata(path.join("plugin.toml")).is_ok() {
        return read(path);
    }
    if !metadata.is_dir() {
        return Err(format!("plugin source must be a directory, not a symlink: {source}").into());
    }
    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("");
    line_in(&keys, path, path, name, source)
}

/// Checks every key of a plugin line without reading any source, so a bad
/// line fails before Binit contacts a remote.
pub(super) fn check_keys(argv: &[String]) -> Result<()> {
    let keys = line_keys(argv)?;
    if !keys.keys().all(|key| *key == "ver")
        && !keys.contains_key("id")
        && let Some(remote) = git::parse_source(&argv[0])?
        && !valid_name(&remote.name)
    {
        return Err(format!(
            "cannot derive a plugin id from {}; add id=NAME",
            git::redact(&argv[0])
        )
        .into());
    }
    let value = |key: &str| list(keys.get(key).copied().unwrap_or(""));
    let plugin = Plugin {
        schema: 1,
        id: keys.get("id").copied().unwrap_or("plugin").to_owned(),
        entry: "entry.bash".into(),
        load: keys.get("load").copied().unwrap_or("startup").to_owned(),
        requires: value("requires"),
        depends: value("depends"),
        completions: value("completions"),
        commands: value("commands"),
        bin: Vec::new(),
    };
    validate_plugin(&plugin)?;
    let files = value("files");
    let bin = value("bin");
    if (keys.contains_key("files") && files.is_empty())
        || (keys.contains_key("bin") && bin.is_empty())
        || files.len() + bin.len() > MAX_FILES - 3
    {
        return Err("a declaration must select between 1 and 125 files".into());
    }
    if let Some(name) = files.iter().find(|name| !valid_declared_file(name)) {
        return Err(format!("invalid declared file: {name}").into());
    }
    for item in &bin {
        bin_item(item)?;
    }
    Ok(())
}

/// The files of a Git tree that a line needs, or `None` for the whole tree
/// of a package that carries its own `plugin.toml`. `has` tells whether the
/// tree holds a path.
pub(super) fn git_paths(
    argv: &[String],
    name: &str,
    has: &dyn Fn(&str) -> bool,
) -> Result<Option<Vec<String>>> {
    let keys = line_keys(argv)?;
    if keys.keys().all(|key| *key == "ver") && has("plugin.toml") {
        return Ok(None);
    }
    let id = line_id(&keys, name, &argv[0])?;
    let mut paths = match (keys.get("files"), keys.get("bin")) {
        (Some(files), _) => list(files),
        (None, Some(_)) => Vec::new(),
        (None, None) => vec![default_file(&id, &argv[0], has)?],
    };
    for item in list(keys.get("bin").copied().unwrap_or("")) {
        let path = bin_item(&item)?.0;
        if !paths.contains(&path) {
            paths.push(path);
        }
    }
    Ok(Some(paths))
}

/// Builds a package from a Git line using the files copied from the tree
/// into `root`. `name` is the repository name used for the default plugin ID.
pub(super) fn git_line(
    argv: &[String],
    root: &Path,
    name: &str,
) -> Result<(Plugin, BTreeMap<String, Vec<u8>>)> {
    let keys = line_keys(argv)?;
    if keys.keys().all(|key| *key == "ver")
        && fs::symlink_metadata(root.join("plugin.toml")).is_ok()
    {
        return read(root);
    }
    // The stored declaration names the tree relative to its own root, so it
    // does not depend on the temporary checkout location.
    line_in(&keys, root, Path::new("."), name, &argv[0])
}

fn line_id(keys: &BTreeMap<&str, &str>, name: &str, source: &str) -> Result<String> {
    match keys.get("id") {
        Some(id) => Ok((*id).to_owned()),
        None => {
            if !valid_name(name) {
                return Err(format!(
                    "cannot derive a plugin id from {}; add id=NAME",
                    git::redact(source)
                )
                .into());
            }
            Ok(name.to_owned())
        }
    }
}

fn default_file(id: &str, source: &str, exists: &dyn Fn(&str) -> bool) -> Result<String> {
    let candidates = [
        format!("{id}.plugin.bash"),
        format!("{id}.bash"),
        "init.bash".to_owned(),
        format!("{id}.sh"),
        "init.sh".to_owned(),
    ];
    candidates
        .iter()
        .find(|name| exists(name))
        .cloned()
        .ok_or_else(|| {
            format!(
                "no files= given and none of the default files exist in {source}: {}",
                candidates.join(", ")
            )
            .into()
        })
}

fn line_in(
    keys: &BTreeMap<&str, &str>,
    root: &Path,
    declared: &Path,
    name: &str,
    source: &str,
) -> Result<(Plugin, BTreeMap<String, Vec<u8>>)> {
    let id = line_id(keys, name, source)?;
    let files = match (keys.get("files"), keys.get("bin")) {
        (Some(files), _) => list(files),
        (None, Some(_)) => Vec::new(),
        (None, None) => vec![default_file(&id, source, &|name| {
            fs::symlink_metadata(root.join(name)).is_ok()
        })?],
    };
    let value = |key: &str| list(keys.get(key).copied().unwrap_or(""));
    let declaration = Declaration {
        schema: 1,
        id,
        path: declared.to_owned(),
        files,
        load: keys.get("load").copied().unwrap_or("startup").to_owned(),
        requires: value("requires"),
        depends: value("depends"),
        completions: value("completions"),
        commands: value("commands"),
        bin: value("bin"),
    };
    let bytes = toml::to_string_pretty(&declaration)?.into_bytes();
    build(declaration, root, bytes)
}
