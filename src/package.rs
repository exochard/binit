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
    #[serde(default)]
    commands: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    bin: Vec<String>,
}

/// Splits one `bin=` item into its package-relative path and command name.
pub(super) fn bin_item(item: &str) -> Result<(String, String)> {
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

pub(super) fn downloaded_line(
    argv: &[String],
    root: &Path,
) -> Result<(Plugin, BTreeMap<String, Vec<u8>>)> {
    let keys = line_keys(argv)?;
    let snippet = argv[0].starts_with("snippet:");
    let value = |key: &str| list(keys.get(key).copied().unwrap_or(""));
    let programs = value("bin")
        .iter()
        .map(|item| bin_item(item))
        .collect::<Result<Vec<_>>>()?;
    let plugin = Plugin {
        schema: 1,
        id: keys["id"].to_owned(),
        entry: "entry.bash".into(),
        load: keys.get("load").copied().unwrap_or("startup").into(),
        requires: value("requires"),
        depends: value("depends"),
        commands: value("commands"),
        delay_ms: keys
            .get("delay_ms")
            .map(|v| v.parse::<u64>())
            .transpose()
            .map_err(|_| "delay_ms must be an integer")?
            .unwrap_or(0),
        event: keys.get("event").copied().unwrap_or("").to_owned(),
        when: keys.get("when").copied().unwrap_or("").to_owned(),
        watch: value("watch"),
        completions: value("completions"),
        bin: programs
            .iter()
            .map(|(path, name)| format!("files/{path}:{name}"))
            .collect(),
    };
    validate_plugin(&plugin)?;
    let sourced = if snippet {
        vec!["snippet.bash".to_owned()]
    } else {
        value("files")
    };
    if sourced.iter().collect::<BTreeSet<_>>().len() != sourced.len() {
        return Err("duplicate declared file in remote source".into());
    }
    let mut files = BTreeMap::new();
    let mut entry = String::from("# Generated from a checksum-pinned remote source.\n");
    let mut selected = BTreeSet::new();
    for (path, is_program) in sourced
        .iter()
        .map(|p| (p, false))
        .chain(programs.iter().map(|(p, _)| (p, true)))
    {
        if !valid_declared_file(path) {
            return Err("invalid remote file path".into());
        }
        let mut full = root.to_owned();
        for part in Path::new(path).components() {
            full.push(part);
            if fs::symlink_metadata(&full)?.file_type().is_symlink() {
                return Err("symlink in remote selection".into());
            }
        }
        let bytes = read_regular_limit(
            &full,
            if is_program {
                acquire::MAX_PROGRAM_BYTES
            } else {
                MAX_BYTES
            },
        )?;
        if selected.insert(path.clone()) {
            files.insert(format!("files/{path}"), bytes);
        }
        if !is_program {
            entry.push_str(&format!(
                "source \"${{BASH_SOURCE[0]%/*}}\"/{} || return $?\n",
                quote(&format!("files/{path}"))
            ));
        }
    }
    if selected.len() > MAX_FILES - 3
        || files.values().map(Vec::len).sum::<usize>() as u64 > acquire::MAX_EXPANDED_BYTES
    {
        return Err("remote package exceeds file or expanded size limits".into());
    }
    files.insert("entry.bash".into(), entry.into_bytes());
    files.insert(
        "plugin.toml".into(),
        toml::to_string_pretty(&plugin)?.into_bytes(),
    );
    Ok((plugin, files))
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

fn wildcard(pattern: &str, name: &str) -> bool {
    let mut previous = vec![false; name.len() + 1];
    previous[0] = true;
    for p in pattern.bytes() {
        let mut current = vec![false; name.len() + 1];
        current[0] = p == b'*' && previous[0];
        for (index, c) in name.bytes().enumerate() {
            current[index + 1] = if p == b'*' {
                previous[index + 1] || (c != b'/' && current[index])
            } else {
                previous[index] && (p == c || (p == b'?' && c != b'/'))
            };
        }
        previous = current;
    }
    previous[name.len()]
}

fn has_wildcard(name: &str) -> bool {
    name.contains(['*', '?'])
}

fn expand_patterns(patterns: &[String], candidates: &[String]) -> Result<Vec<String>> {
    let mut selected = Vec::new();
    let mut seen = BTreeSet::new();
    for pattern in patterns {
        if !valid_declared_file(pattern) || pattern.len() > 1024 {
            return Err(format!("invalid declared file: {pattern}").into());
        }
        if has_wildcard(pattern) && candidates.len() > 8192 {
            return Err("file pattern search exceeds 8192 entries".into());
        }
        if has_wildcard(pattern) && candidates.iter().any(|name| name.len() > 4096) {
            return Err("file pattern candidate exceeds 4096 bytes".into());
        }
        let matches: Vec<_> = if has_wildcard(pattern) {
            candidates
                .iter()
                .filter(|name| wildcard(pattern, name))
                .cloned()
                .collect()
        } else {
            vec![pattern.clone()]
        };
        if matches.is_empty() {
            return Err(format!("declared file pattern matches no files: {pattern}").into());
        }
        for name in matches {
            if !seen.insert(name.clone()) {
                return Err(format!("duplicate declared file: {name}").into());
            }
            selected.push(name);
            if selected.len() > MAX_FILES - 3 {
                return Err("a declaration must select between 1 and 125 files".into());
            }
        }
    }
    Ok(selected)
}

fn local_candidates(root: &Path, patterns: &[String]) -> Result<Vec<String>> {
    let mut paths = BTreeSet::new();
    let mut visited = 0usize;
    for pattern in patterns.iter().filter(|name| has_wildcard(name)) {
        if !valid_declared_file(pattern) || pattern.len() > 1024 {
            return Err(format!("invalid declared file: {pattern}").into());
        }
        let mut prefixes = vec![String::new()];
        let parts: Vec<_> = pattern.split('/').collect();
        for (index, part) in parts.iter().enumerate() {
            let mut next = Vec::new();
            for prefix in prefixes {
                let directory = root.join(&prefix);
                let entries = match fs::read_dir(directory) {
                    Ok(entries) => entries,
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                    Err(error) => return Err(error.into()),
                };
                for entry in entries {
                    visited += 1;
                    if visited > 8192 {
                        return Err("file pattern search exceeds 8192 entries".into());
                    }
                    let entry = entry?;
                    let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
                        continue;
                    };
                    if !wildcard(part, &name) {
                        continue;
                    }
                    let path = if prefix.is_empty() {
                        name
                    } else {
                        format!("{prefix}/{name}")
                    };
                    let kind = entry.file_type()?;
                    if kind.is_symlink() {
                        return Err(format!("symlink in declared file: {path}").into());
                    }
                    if index + 1 == parts.len() {
                        if kind.is_file() {
                            paths.insert(path);
                        }
                    } else if kind.is_dir() {
                        next.push(path);
                    }
                }
            }
            prefixes = next;
        }
    }
    Ok(paths.into_iter().collect())
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
        delay_ms: declaration.delay_ms,
        event: declaration.event,
        when: declaration.when,
        watch: declaration.watch,
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
    let candidates = local_candidates(&root, &declaration.files)?;
    for name in expand_patterns(&declaration.files, &candidates)? {
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

fn version_components(version: &str) -> Option<[(usize, &str); 3]> {
    let mut parts = version.split('.');
    let mut components = [(0, ""); 3];
    for component in &mut components {
        let part = parts.next()?;
        if part.is_empty()
            || (part.len() > 1 && part.starts_with('0'))
            || !part.bytes().all(|byte| byte.is_ascii_digit())
        {
            return None;
        }
        // Compare equal-length digit strings lexicographically.
        *component = (part.len(), part);
    }
    parts.next().is_none().then_some(components)
}

#[derive(Debug)]
pub(super) struct IncompatibleCore(String);

impl std::fmt::Display for IncompatibleCore {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for IncompatibleCore {}

fn check_minimum(id: &str, minimum: Option<&toml::Value>) -> Result<()> {
    let Some(minimum) = minimum else {
        return Ok(());
    };
    let (version, required) = minimum
        .as_str()
        .and_then(|version| version_components(version).map(|components| (version, components)))
        .ok_or_else(|| format!("plugin {id}: min_binit must be a MAJOR.MINOR.PATCH string"))?;
    let running = version_components(concat!(
        env!("CARGO_PKG_VERSION_MAJOR"),
        ".",
        env!("CARGO_PKG_VERSION_MINOR"),
        ".",
        env!("CARGO_PKG_VERSION_PATCH"),
    ))
    .expect("Cargo version has three numeric components");
    if required > running || (required == running && !env!("CARGO_PKG_VERSION_PRE").is_empty()) {
        return Err(IncompatibleCore(format!(
            "plugin {id} requires Binit {version} or newer (running {})",
            env!("CARGO_PKG_VERSION")
        ))
        .into());
    }
    Ok(())
}

pub(super) fn check_snapshot_minimum(id: &str, files: &BTreeMap<String, Vec<u8>>) -> Result<()> {
    // The package manifest is pinned by the existing lock file hashes.
    if let Some(bytes) = files.get(&format!("plugins/{id}/plugin.toml")) {
        let manifest = manifest_fields(bytes)?;
        check_minimum(id, manifest.get("min_binit"))?;
    }
    Ok(())
}

fn manifest_fields(bytes: &[u8]) -> Result<toml::Table> {
    Ok(toml::from_str(std::str::from_utf8(bytes)?)?)
}

pub(super) fn parse_manifest(bytes: &[u8]) -> Result<(Plugin, Option<toml::Value>)> {
    let mut manifest = manifest_fields(bytes)?;
    let minimum = manifest.remove("min_binit");
    Ok((toml::Value::Table(manifest).try_into()?, minimum))
}

pub(super) fn preserve_minimum(
    id: &str,
    files: &mut BTreeMap<String, Vec<u8>>,
    source: &[u8],
) -> Result<()> {
    // A raw file declaration can point into a tree with its own unrelated manifest.
    let Ok(manifest) = manifest_fields(source) else {
        return Ok(());
    };
    let Some(minimum) = manifest.get("min_binit") else {
        return Ok(());
    };
    if manifest.get("schema").and_then(toml::Value::as_integer) != Some(1) {
        return Ok(());
    }
    check_minimum(id, Some(minimum))?;
    files
        .get_mut("plugin.toml")
        .expect("generated package has a manifest")
        .extend(format!("min_binit = {minimum}\n").bytes());
    limits(files)
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
    let (plugin, minimum) = parse_manifest(files.get("plugin.toml").ok_or("missing plugin.toml")?)?;
    validate_plugin(&plugin)?;
    check_minimum(&plugin.id, minimum.as_ref())?;
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

const LINE_KEYS: [&str; 18] = [
    "id",
    "files",
    "bin",
    "load",
    "commands",
    "completions",
    "requires",
    "depends",
    "ver",
    "sha256",
    "asset",
    "format",
    "atclone",
    "atpull",
    "delay_ms",
    "event",
    "when",
    "watch",
];

fn list(value: &str) -> Vec<String> {
    if value.is_empty() {
        Vec::new()
    } else {
        value.split(',').map(str::to_owned).collect()
    }
}

pub(super) fn line_keys(argv: &[String]) -> Result<BTreeMap<&str, &str>> {
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
    if keys.keys().all(|key| ["atclone", "atpull"].contains(key))
        && fs::symlink_metadata(path.join("plugin.toml")).is_ok()
    {
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
    if remote::is_source(&argv[0]) {
        remote::origin(argv)?;
    } else if keys
        .keys()
        .any(|key| ["sha256", "asset", "format"].contains(key))
    {
        return Err("sha256=, asset= and format= require a release or snippet source".into());
    }
    if ["atclone", "atpull"]
        .iter()
        .any(|key| keys.get(key).is_some_and(|script| script.len() > 8192))
    {
        return Err("lifecycle hook exceeds 8192 bytes".into());
    }
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
        delay_ms: keys
            .get("delay_ms")
            .map(|v| v.parse::<u64>())
            .transpose()
            .map_err(|_| "delay_ms must be an integer")?
            .unwrap_or(0),
        event: keys.get("event").copied().unwrap_or("").to_owned(),
        when: keys.get("when").copied().unwrap_or("").to_owned(),
        watch: value("watch"),
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
    candidates: &[String],
) -> Result<Option<Vec<String>>> {
    let keys = line_keys(argv)?;
    if keys
        .keys()
        .all(|key| ["ver", "atclone", "atpull"].contains(key))
        && has("plugin.toml")
    {
        return Ok(None);
    }
    let id = line_id(&keys, name, &argv[0])?;
    let mut paths = match (keys.get("files"), keys.get("bin")) {
        (Some(files), _) => expand_patterns(&list(files), candidates)?,
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
    if keys
        .keys()
        .all(|key| ["ver", "atclone", "atpull"].contains(key))
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
        delay_ms: keys
            .get("delay_ms")
            .map(|v| v.parse::<u64>())
            .transpose()
            .map_err(|_| "delay_ms must be an integer")?
            .unwrap_or(0),
        event: keys.get("event").copied().unwrap_or("").to_owned(),
        when: keys.get("when").copied().unwrap_or("").to_owned(),
        watch: value("watch"),
        bin: value("bin"),
    };
    let bytes = toml::to_string_pretty(&declaration)?.into_bytes();
    let (plugin, mut files) = build(declaration, root, bytes)?;
    if let Ok(source) = read_regular(&root.join("plugin.toml")) {
        preserve_minimum(&plugin.id, &mut files, &source)?;
    }
    Ok((plugin, files))
}

#[cfg(test)]
mod version_tests {
    use super::version_components;

    #[test]
    fn minimum_versions_use_three_numeric_components() {
        assert!(version_components("0.10.0") > version_components("0.9.0"));
        assert!(
            version_components("18446744073709551616.0.0")
                > version_components("18446744073709551615.0.0")
        );
        for invalid in [
            "",
            "0.1",
            "0.1.0.0",
            "0.01.1",
            "v0.1.1",
            "0.1.1-dev",
            "0.+1.1",
        ] {
            assert_eq!(version_components(invalid), None, "{invalid}");
        }
    }
}
