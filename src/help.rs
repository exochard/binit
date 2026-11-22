use super::Result;
use std::ffi::OsString;
use std::io::{self, IsTerminal};

const GUIDE: &str = "usage: binit COMMAND [ARG...]\n\nplugins\n  plugins         list plugins for the next shell\n  inspect ID      show source and load rules\n  sync            prepare a verified generation\n  update --check  preview plugin updates\n  rollback        select the previous generation\n\ncurrent shell\n  profile         show plugin state and load time\n  report ID       show recorded plugin effects\n  completions     list managed completions\n  editor          show enabled editor features\n\nall commands     binit --help\ncommand help     binit help COMMAND";

fn display_width(terminal: bool) -> usize {
    if terminal {
        let mut size = std::mem::MaybeUninit::<libc::winsize>::uninit();
        // TIOCGWINSZ initializes the supplied winsize on success.
        if unsafe { libc::ioctl(libc::STDOUT_FILENO, libc::TIOCGWINSZ, size.as_mut_ptr()) } == 0 {
            let columns = unsafe { size.assume_init() }.ws_col;
            if columns != 0 {
                return columns.into();
            }
        }
    }
    std::env::var("COLUMNS")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .filter(|width| *width > 0)
        .unwrap_or(80)
}

fn print_guide() {
    let terminal = io::stdout().is_terminal();
    let narrow = display_width(terminal) < 60;
    let style = terminal
        && std::env::var_os("NO_COLOR").is_none_or(|value| value.is_empty())
        && std::env::var_os("TERM").is_some_and(|value| value != "dumb");
    for line in GUIDE.lines() {
        if narrow
            && let Some((command, description)) =
                line.strip_prefix("  ").and_then(|row| row.split_once("  "))
        {
            println!("  {}\n    {}", command.trim_end(), description.trim_start());
        } else if style && matches!(line, "plugins" | "current shell") {
            println!("\x1b[1m{line}\x1b[0m");
        } else {
            println!("{line}");
        }
    }
}

pub(super) fn handle(args: &[OsString]) -> Result<bool> {
    if args.is_empty() {
        print_guide();
        return Ok(true);
    }
    if args.len() == 1 && (args[0] == "--help" || args[0] == "-h" || args[0] == "help") {
        print_guide();
        println!(
            "\nmanager commands\n  add enable disable remove update plugins inspect shell apply\n  diagnose candidate prepare init status rollback trust project releases\n  run extension declare sync\n\ncurrent-shell commands\n  plugin profile report unload completions load editor completion\n  on off emit dispatch\n\nstate     --state dir, $BINIT_STATE, $XDG_DATA_HOME/binit,\n          or $HOME/.local/share/binit\nrecovery  BINIT_SAFE=1 bash --rcfile path/to/init.bash -i"
        );
        return Ok(true);
    }
    let topic = if args.len() == 2 && args[0] == "help" {
        args[1].to_str()
    } else if args.len() == 2 && (args[1] == "--help" || args[1] == "-h") {
        args[0].to_str()
    } else {
        return Ok(false);
    };
    let text = match topic {
        Some("update") => {
            "usage: binit update [--check] [--jobs N] [ID...]\n       binit update ID [PATH]\n\n--check previews Git revisions and lists local sources. It leaves the generation unchanged.\n--jobs fetches 1 to 8 repositories at once (default 4).\nFor a catalog plugin, ID [PATH] snapshots its local source."
        }
        Some("plugin") => {
            "usage: binit plugin SOURCE [KEY=VALUE...]\n\nRequires an initialized shell. Load modes: startup, prompt, wait,\ncommand, completion, timed, event and watch. See docs/plugins.md."
        }
        Some("run") => {
            "usage: binit run [--engine BASH] [--] SCRIPT [ARG...]\n\n--engine defaults to /bin/bash"
        }
        Some("extension") => {
            "usage: binit extension ID [ARG...]\n\nRun a verified plugin manager extension."
        }
        Some("sync") => {
            "usage: binit sync [--jobs N]\n\nPrepare plugin declarations. Jobs range from 1 to 8 (default 4)."
        }
        Some("report" | "unload" | "load" | "inspect" | "enable" | "disable" | "remove") => {
            println!("usage: binit {} ID", topic.unwrap());
            if matches!(topic, Some("report" | "unload" | "load")) {
                println!("\nrequires an initialized shell");
            }
            return Ok(true);
        }
        Some("completions") => {
            "usage: binit completions [list [--plugin ID]]\n       binit completions enable|disable NAME\n       binit completions enable|disable --plugin ID\n       binit completions register NAME PROGRAM [ARG...]\n       binit completions unregister NAME\n\nrequires an initialized shell"
        }
        Some("completion") => {
            "usage: binit completion [on|off|status]\n\nRequires an initialized shell."
        }
        Some("editor") => "usage: binit editor [status]\n\nRequires an initialized shell.",
        Some(
            "profile" | "dispatch" | "plugins" | "shell" | "apply" | "init" | "status" | "rollback"
            | "declare",
        ) => {
            println!("usage: binit {}", topic.unwrap());
            if matches!(topic, Some("profile" | "dispatch")) {
                println!("\nrequires an initialized shell");
            }
            return Ok(true);
        }
        Some("on" | "off") => {
            "usage: binit on|off before-load|after-load ID FUNCTION\n\nRequires an initialized shell."
        }
        Some("emit") => "usage: binit emit EVENT\n\nRequires an initialized shell.",
        Some("add") => "usage: binit add PATH [--disabled]",
        Some("prepare") => "usage: binit prepare CONFIG",
        Some("trust") => "usage: binit trust DIRECTORY [--sha256 HEX]",
        Some("project") => "usage: binit project DIRECTORY",
        Some("diagnose") => "usage: binit diagnose PLAN [--json] [--no-trace]",
        Some("candidate") => {
            "usage: binit candidate prepare CONFIG | check PLAN | select [--base GEN --generation GEN --receipt SHA256] | status [--json]"
        }
        Some("releases") => {
            "usage: binit releases OWNER/REPO [--ver TAG|latest] [--asset GLOB] [--platform host|linux/x86_64|linux/arm64]"
        }
        _ => return Err("unknown help topic".into()),
    };
    println!("{text}");
    Ok(true)
}
