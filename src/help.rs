use super::{Result, terminal_ui};
use std::ffi::OsString;
use std::io::{self, IsTerminal, Read};
use std::os::unix::ffi::OsStrExt;

const MANAGER_COMMANDS: &[&[&str]] = &[
    &[
        "add", "enable", "disable", "remove", "update", "plugins", "inspect", "shell", "apply",
    ],
    &[
        "diagnose",
        "candidate",
        "prepare",
        "init",
        "status",
        "rollback",
        "trust",
        "project",
        "releases",
    ],
    &[
        "run",
        "extension",
        "declare",
        "sync",
        "install",
        "uninstall",
    ],
];
const SHELL_COMMANDS: &[&[&str]] = &[
    &[
        "plugin",
        "profile",
        "logs",
        "report",
        "unload",
        "completions",
        "load",
        "editor",
        "completion",
    ],
    &["on", "off", "emit", "dispatch"],
];
const CANDIDATE_ACTIONS: &[&str] = &[
    "prepare", "restore", "check", "diff", "history", "select", "status",
];
const COMPLETION_ACTIONS: &[&str] = &["list", "enable", "disable", "register", "unregister"];

fn commands() -> impl Iterator<Item = &'static str> {
    MANAGER_COMMANDS
        .iter()
        .chain(SHELL_COMMANDS)
        .flat_map(|line| line.iter().copied())
}

// Results are one word per line. Markers ask Bash for data it already owns.
pub(super) fn complete() -> Result<()> {
    use super::{default_state, manager, pointer, verify_generation_checks};
    use std::path::PathBuf;

    const LIMIT: u64 = 64 * 1024;
    let mut input = Vec::new();
    io::stdin().take(LIMIT + 1).read_to_end(&mut input)?;
    let Some(input) = input.strip_suffix(&[0]) else {
        return Err("incomplete completion request".into());
    };
    if input.len() as u64 >= LIMIT {
        return Err("completion request exceeds size limit".into());
    }
    let mut words = Vec::new();
    let mut state = None;
    let mut take_state = false;
    for word in input
        .split(|byte| *byte == 0)
        .map(std::ffi::OsStr::from_bytes)
    {
        if take_state {
            state = Some(PathBuf::from(word));
            take_state = false;
        } else if word == "--state" {
            take_state = true;
        } else {
            words.push(word.to_str().unwrap_or_default());
        }
    }
    let choices: &[&str] = if take_state {
        &["@directories"]
    } else {
        match words.as_slice() {
            [] => {
                for name in commands().chain(["help", "--help", "--version", "--state"]) {
                    println!("{name}");
                }
                return Ok(());
            }
            ["help"] => {
                for name in commands() {
                    println!("{name}");
                }
                return Ok(());
            }
            ["candidate"] => CANDIDATE_ACTIONS,
            ["candidate", "prepare" | "check"] | ["prepare" | "diagnose" | "add"] => &["@files"],
            ["candidate", "status" | "history"] | ["logs"] | ["profile", "--timeline"] => {
                &["--json"]
            }
            ["candidate", "diff"] => &["--files"],
            ["profile"] => &["--timeline"],
            ["plugins"] => &["--session"],
            ["plugins", "--session"] => &["--json"],
            ["candidate", "restore", _] => &["--from", "--pick"],
            ["candidate", "select", ..] => &["--base", "--generation", "--receipt"],
            ["completions"] => COMPLETION_ACTIONS,
            ["completions", "enable" | "disable"] => &["--plugin", "@completions"],
            ["completions", "list"] => &["--plugin"],
            ["completions", "list" | "enable" | "disable", "--plugin"] => &["@live"],
            ["completions", "unregister"] => &["@completions"],
            ["completion"] => &["on", "off", "status"],
            ["editor"] => &["status"],
            ["on" | "off"] => &["before-load", "after-load"],
            ["on" | "off", "before-load" | "after-load"] | ["report" | "load" | "unload"] => {
                &["@live"]
            }
            ["sync"] | ["sync", "--stage"] | ["sync", "--jobs", _] => &["--jobs", "--stage"],
            ["update" | "sync", .., "--jobs"] => &["1", "2", "3", "4", "5", "6", "7", "8"],
            ["run"] => &["--engine", "--", "@files"],
            ["run", "--engine"] | ["run", "--"] => &["@files"],
            ["install" | "uninstall", .., "--prefix"] => &["@directories"],
            ["install", .., "--rc-file" | "--binary"] => &["@files"],
            ["install", ..] => &["--prefix", "--rc-file", "--binary"],
            ["uninstall", ..] => &["--prefix"],
            ["add", _] => &["--disabled"],
            ["trust" | "project"] => &["@directories"],
            ["trust", _] => &["--sha256"],
            ["diagnose", _, ..] => &["--json", "--no-trace"],
            ["releases", _, ..] => &["--ver", "--asset", "--platform"],
            ["plugin"] => &["@directories"],
            ["plugin", _, .., "load", "="] => &[
                "startup",
                "prompt",
                "wait",
                "command",
                "completion",
                "timed",
                "event",
                "watch",
            ],
            ["plugin", _, ..] => &[
                "id=",
                "files=",
                "bin=",
                "load=",
                "commands=",
                "completions=",
                "requires=",
                "depends=",
                "delay_ms=",
                "event=",
                "when=",
                "watch=",
            ],
            ["candidate", "restore"]
            | ["inspect" | "enable" | "disable" | "remove"]
            | ["update", ..] => {
                if words[0] == "update" {
                    println!("--check\n--stage\n--jobs");
                }
                let state = state.map_or_else(default_state, Ok)?;
                if !state.join("current").exists() {
                    return Ok(());
                }
                let generation = pointer(&state, "current")?;
                let lock = verify_generation_checks(&state, &generation)?;
                for entry in manager::catalog(&lock) {
                    if words[0] != "update" || !words[1..].contains(&entry.plugin.id.as_str()) {
                        println!("{}", entry.plugin.id);
                    }
                }
                return Ok(());
            }
            _ => &[],
        }
    };
    for option in choices {
        if !words.contains(option) {
            println!("{option}");
        }
    }
    Ok(())
}

const GUIDE: &str = concat!(
    "usage: binit COMMAND [ARG...]\n",
    "\n",
    "plugins\n",
    "  plugins --session       compare with next shell\n",
    "  inspect ID              show source and load rules\n",
    "  sync --stage            stage current declarations\n",
    "  update --check          preview plugin updates\n",
    "  update --stage          stage plugin updates\n",
    "\n",
    "candidate\n",
    "  candidate diff          review staged changes\n",
    "  candidate check PLAN    run candidate checks\n",
    "  candidate select        use checked generation\n",
    "  rollback                use the previous generation\n",
    "\n",
    "current shell\n",
    "  profile                 show plugin state and load time\n",
    "  logs                    show recorded lifecycle events\n",
    "  report ID               show recorded plugin effects\n",
    "  completions             list managed completions\n",
    "  editor                  show enabled editor features\n",
    "\n",
    "all commands     binit --help\n",
    "command help     binit help COMMAND",
);

fn print_guide() {
    let terminal = io::stdout().is_terminal();
    let width = terminal_ui::width(terminal, libc::STDOUT_FILENO);
    let style = terminal_ui::color(terminal);
    for line in GUIDE.lines() {
        if width < 60
            && let Some((command, description)) =
                line.strip_prefix("  ").and_then(|row| row.split_once("  "))
        {
            let command = command.trim_end();
            let description = description.trim_start();
            if command.len() + description.len() + 4 <= width {
                println!("  {command}  {description}");
            } else {
                println!("  {command}\n    {description}");
            }
        } else if style && matches!(line, "plugins" | "candidate" | "current shell") {
            println!("{}", terminal_ui::styled(line, "1", style));
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
        for (title, lines) in [
            ("manager commands", MANAGER_COMMANDS),
            ("current-shell commands", SHELL_COMMANDS),
        ] {
            println!("\n{title}");
            for line in lines {
                println!("  {}", line.join(" "));
            }
        }
        println!(
            "\nstate     --state dir, $BINIT_STATE, $XDG_DATA_HOME/binit,\n          or $HOME/.local/share/binit\nrecovery  BINIT_SAFE=1 bash --rcfile path/to/init.bash -i"
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
        Some("install") => {
            "usage: binit install [--prefix PATH] [--rc-file PATH] [--binary PATH]\n\nInstall this executable to ~/.local/bin. Pass --rc-file to add a managed startup block."
        }
        Some("uninstall") => {
            "usage: binit uninstall [--prefix PATH]\n\nRemove the verified executable and startup block recorded by the installer. Plugin state is kept."
        }
        Some("update") => {
            "usage: binit update [--check | --stage] [--jobs N] [ID...]\n       binit update [--stage] ID [PATH]\n\n--check previews Git revisions and lists local sources.\n--stage prepares a candidate for binit candidate diff, check and select.\n--jobs fetches 1 to 8 repositories at once (default 4).\nFor a catalog plugin, ID [PATH] snapshots its local source."
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
            "usage: binit sync [--stage] [--jobs N]\n\nUse --stage to prepare a candidate for review. Jobs range from 1 to 8 (default 4)."
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
        Some("logs") => {
            "usage: binit logs [--json]\n\nSet BINIT_LOGS=1 before initializing Binit to record lifecycle events."
        }
        Some("profile") => {
            "usage: binit profile [--timeline [--json]]\n\nRequires an initialized shell."
        }
        Some("plugins") => {
            "usage: binit plugins [--session [--json]]\n\n--session also shows live state. Requires an initialized shell."
        }
        Some("dispatch" | "shell" | "apply" | "init" | "status" | "rollback" | "declare") => {
            println!("usage: binit {}", topic.unwrap());
            if matches!(topic, Some("dispatch")) {
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
            "usage: binit candidate prepare CONFIG | restore ID [--from GENERATION | --pick] | check PLAN | diff [--files] | history [--json] | select [--base GEN --generation GEN --receipt SHA256] | status [--json]"
        }
        Some("releases") => {
            "usage: binit releases OWNER/REPO [--ver TAG|latest] [--asset GLOB] [--platform host|linux/x86_64|linux/arm64]"
        }
        _ => return Err("unknown help topic".into()),
    };
    println!("{text}");
    Ok(true)
}
