use super::*;

#[derive(Deserialize)]
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
    let root = path.parent().unwrap().join(declaration.path);
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
