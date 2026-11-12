use super::*;
use std::os::unix::process::CommandExt;

pub(super) fn command(raw: &[std::ffi::OsString]) -> Result<()> {
    let mut args = raw.to_vec();
    let mut state = None;
    let boundary = args.iter().position(|s| s == "--").unwrap_or(args.len());
    if let Some(position) = args[..boundary].iter().position(|s| s == "--state") {
        if position + 1 >= boundary {
            return Err("--state needs a directory".into());
        }
        state = Some(PathBuf::from(args.remove(position + 1)));
        args.remove(position);
        let boundary = args.iter().position(|s| s == "--").unwrap_or(args.len());
        if args[..boundary].iter().any(|s| s == "--state") {
            return Err("--state may only be specified once".into());
        }
    }
    if let Some(boundary) = args.iter().position(|s| s == "--") {
        args.remove(boundary);
    }
    let state = state.map_or_else(default_state, Ok)?;
    run(&state, &args)
}

fn run(state: &Path, args: &[std::ffi::OsString]) -> Result<()> {
    let id = args
        .first()
        .and_then(|s| s.to_str())
        .ok_or("extension needs a UTF-8 plugin ID")?;
    if !valid_name(id) {
        return Err("invalid extension ID".into());
    }
    let generation = pointer(state, "current")?;
    let lock = verify_generation(state, &generation)?;
    if !lock.plugins.iter().any(|plugin| plugin.id == id) {
        return Err(format!("extension plugin is not enabled: {id}").into());
    }
    let relative = format!("plugins/{id}/manager.bash");
    if !lock.files.contains_key(&relative) {
        return Err(format!("plugin {id} has no manager.bash extension").into());
    }
    let bash = std::env::var_os("BINIT_BASH").unwrap_or_else(|| "/bin/bash".into());
    if !Path::new(&bash).is_absolute() {
        return Err("BINIT_BASH must be an absolute executable path".into());
    }
    let script = fs::canonicalize(state.join("generations").join(generation).join(relative))?;
    let state = fs::canonicalize(state)?;
    let error = std::process::Command::new(bash)
        .args(["--noprofile", "--norc", "--"])
        .arg(script)
        .args(&args[1..])
        .env_remove("BASH_ENV")
        .env_remove("ENV")
        .env("BINIT_PLUGIN_ID", id)
        .env("BINIT_EXTENSION_STATE", state)
        .exec();
    Err(error.into())
}
