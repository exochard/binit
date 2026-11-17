use super::Result;
use std::ffi::{OsStr, OsString};
use std::os::unix::process::CommandExt;
use std::process::Command;

pub(super) fn run(args: &[OsString]) -> Result<()> {
    let mut arguments = args.iter();
    let mut script = arguments
        .next()
        .ok_or("run needs a script; see binit run --help")?;
    if script == "--help" || script == "-h" {
        println!(
            "usage: binit run [--engine BASH] [--] SCRIPT [ARG...]\n\n--engine defaults to /bin/bash"
        );
        return Ok(());
    }
    let mut engine = OsStr::new("/bin/bash");
    if script == "--engine" {
        engine = arguments.next().ok_or("--engine needs a Bash executable")?;
        if engine.is_empty() {
            return Err("--engine needs a Bash executable".into());
        }
        script = arguments.next().ok_or("run needs a script")?;
    }
    if script == "--" {
        script = arguments.next().ok_or("run needs a script")?;
    } else if script.as_encoded_bytes().starts_with(b"-") && script != "-" {
        return Err("unknown run option; use -- before a script beginning with '-'".into());
    }
    let error = Command::new(engine)
        .args(["--noprofile", "--norc", "--"])
        .arg(script)
        .args(arguments)
        .exec();
    Err(format!("cannot run {}: {error}", engine.to_string_lossy()).into())
}
