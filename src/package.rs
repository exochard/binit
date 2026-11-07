use super::*;

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Declaration {
    schema: u32,
    id: String,
    path: PathBuf,
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

// Both declaration files and plugin lines are validated and copied here.
fn build(
    declaration: Declaration,
    base: &Path,
    bytes: Vec<u8>,
) -> Result<(Plugin, BTreeMap<String, Vec<u8>>)> {
    let plugin = Plugin {
        schema: declaration.schema,
        id: declaration.id,
        entry: "entry.bash".into(),
        load: declaration.load,
        requires: declaration.requires,
        depends: declaration.depends,
        completions: declaration.completions,
        commands: declaration.commands,
    };
    validate_plugin(&plugin)?;
    if declaration.files.is_empty() || declaration.files.len() > MAX_FILES - 3 {
        return Err("a declaration must select between 1 and 125 files".into());
    }
    let root = base.join(declaration.path);
    if !fs::symlink_metadata(&root)?.is_dir() {
        return Err("declared source must be a directory, not a symlink".into());
    }
    let root = fs::canonicalize(root)?;
    let mut files = BTreeMap::new();
    let mut entry = "# Generated from the pinned source declaration.\n".to_owned();
    for name in declaration.files {
        if !valid_relative(&name)
            || name
                .split('/')
                .any(|part| part.is_empty() || part == "." || part == "..")
            || Path::new(&name).components().count() > 16
        {
            return Err(format!("invalid declared file: {name}").into());
        }
        let mut file = root.clone();
        for part in Path::new(&name).components() {
            file.push(part);
            if fs::symlink_metadata(&file)?.file_type().is_symlink() {
                return Err(format!("symlink in declared file: {name}").into());
            }
        }
        let selected = format!("files/{name}");
        if files
            .insert(selected.clone(), read_regular(&file)?)
            .is_some()
        {
            return Err(format!("duplicate declared file: {name}").into());
        }
        limits(&files)?;
        entry.push_str(&format!(
            "source \"${{BASH_SOURCE[0]%/*}}\"/{} || return $?\n",
            quote(&selected)
        ));
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
    Ok((plugin, files))
}

const LINE_KEYS: [&str; 7] = [
    "id",
    "files",
    "load",
    "commands",
    "completions",
    "requires",
    "depends",
];

fn list(value: &str) -> Vec<String> {
    if value.is_empty() {
        Vec::new()
    } else {
        value.split(',').map(str::to_owned).collect()
    }
}

/// Builds a package from the arguments of one `binit plugin` line.
pub(super) fn line(argv: &[String]) -> Result<(Plugin, BTreeMap<String, Vec<u8>>)> {
    let source = argv.first().ok_or("empty plugin line")?;
    let path = Path::new(source);
    if !path.is_absolute() {
        return Err(format!("plugin source must be an absolute path: {source}").into());
    }
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
    let metadata = fs::symlink_metadata(path)?;
    if keys.is_empty() && fs::symlink_metadata(path.join("plugin.toml")).is_ok() {
        return read(path);
    }
    if !metadata.is_dir() {
        return Err(format!("plugin source must be a directory, not a symlink: {source}").into());
    }
    let id = match keys.get("id") {
        Some(id) => (*id).to_owned(),
        None => {
            let name = path
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("");
            if !valid_name(name) {
                return Err(format!("cannot derive a plugin id from {source}; add id=NAME").into());
            }
            name.to_owned()
        }
    };
    let files = match keys.get("files") {
        Some(files) => list(files),
        None => {
            let candidates = [
                format!("{id}.plugin.bash"),
                format!("{id}.bash"),
                "init.bash".to_owned(),
                format!("{id}.sh"),
                "init.sh".to_owned(),
            ];
            let found = candidates
                .iter()
                .find(|name| fs::symlink_metadata(path.join(name)).is_ok())
                .ok_or_else(|| {
                    format!(
                        "no files= given and none of the default files exist in {source}: {}",
                        candidates.join(", ")
                    )
                })?;
            vec![found.clone()]
        }
    };
    let value = |key: &str| list(keys.get(key).copied().unwrap_or(""));
    let declaration = Declaration {
        schema: 1,
        id,
        path: path.to_owned(),
        files,
        load: keys.get("load").copied().unwrap_or("startup").to_owned(),
        requires: value("requires"),
        depends: value("depends"),
        completions: value("completions"),
        commands: value("commands"),
    };
    let bytes = toml::to_string_pretty(&declaration)?.into_bytes();
    build(declaration, path, bytes)
}
