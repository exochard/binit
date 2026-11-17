//! Splits the shell runtime so a shell start parses only what startup uses.
//!
//! Bash spends most of the runtime's load time building function bodies. Functions outside
//! `EAGER` go to deferred files, and `runtime.bash` keeps a one-line stub for each; the first call
//! of a stub sources that function's file, which replaces every stub of the file with its
//! definition. Functions in a `GROUPS` entry get a file of their own, so their first use parses
//! only them; every other deferred function goes to `runtime-more.bash`.

/// Functions a startup, its first prompt and plugin loads call; everything else loads on use.
const EAGER: &[&str] = &[
    // The public command stays whole, so code that copies or wraps its definition sees it.
    "binit",
    "binit_on",
    "binit_segment",
    "__binit_declare_line",
    "__binit_load_direct",
    "__binit_activate_plugins",
    "__binit_activate_shell",
    "__binit_load_completion",
    "__binit_has_completion",
    "__binit_prompt_dispatch",
    "__binit_first_prompt_work",
    "__binit_wait_arm",
];

/// Deferred functions used together and often alone, each group with its own file.
const GROUPS: &[(&str, &[&str])] = &[("unload", &["__binit_unload", "__binit_provider_forget"])];

/// The file that holds every deferred function outside `GROUPS`.
const REST: &str = "more";

/// Appended to the eager runtime, before the stubs. A deferred file holds only function
/// definitions, so sourcing it again is safe: a definition installed before a failure is
/// already the final one, and clearing the flag lets the next stub call retry the rest.
/// A shell that needed the rest is already using the runtime at length, so loading the rest
/// also loads every group; a group that fails there loads again on its own first use.
const LOADER: &str = r#"
# The functions below load from runtime-<group>.bash on first use; each file redefines its own.
__binit_runtime_dir=$__binit_generation_dir
[[ $__binit_runtime_dir == /* ]] || __binit_runtime_dir=$PWD/$__binit_runtime_dir
declare -gA __binit_runtime_loaded=()
__binit_runtime_load() {
    [[ -z ${__binit_runtime_loaded[$1]-} ]] || return 1
    __binit_runtime_loaded[$1]=loading
    if ! source "$__binit_runtime_dir/runtime-$1.bash"; then
        unset '__binit_runtime_loaded[$1]'
        return 1
    fi
    __binit_runtime_loaded[$1]=1
    [[ $1 == @REST@ ]] || return 0
    local __binit_runtime_group
    for __binit_runtime_group in @GROUPS@; do
        [[ -n ${__binit_runtime_loaded[$__binit_runtime_group]-} ]] ||
            __binit_runtime_load "$__binit_runtime_group" || :
    done
    return 0
}
"#;

/// Returns the eager runtime, with the loader and stubs appended, and each deferred file as
/// `(name, text)`, named `runtime-<group>.bash`.
pub fn split(source: &str) -> Result<(String, Vec<(String, String)>), String> {
    let mut eager = String::new();
    let mut files: Vec<(&str, String, Vec<String>)> = GROUPS
        .iter()
        .map(|(group, _)| (*group, String::new(), Vec::new()))
        .collect();
    files.push((REST, String::new(), Vec::new()));
    let mut lines = source.split_inclusive('\n').peekable();
    // Comment lines directly above a function travel with it.
    let mut pending = String::new();
    while let Some(line) = lines.next() {
        if line.starts_with('#') {
            pending.push_str(line);
            continue;
        }
        let Some(name) = function_start(line) else {
            eager.push_str(&pending);
            pending.clear();
            eager.push_str(line);
            continue;
        };
        let mut body = std::mem::take(&mut pending);
        body.push_str(line);
        loop {
            let next = lines
                .next()
                .ok_or_else(|| format!("runtime function {name} has no closing brace"))?;
            body.push_str(next);
            if next.trim_end_matches('\n') == "}" {
                break;
            }
            if function_start(next).is_some() {
                return Err(format!("runtime function {name} has no closing brace"));
            }
        }
        if EAGER.contains(&name) {
            eager.push_str(&body);
            continue;
        }
        let group = GROUPS
            .iter()
            .find(|(_, names)| names.contains(&name))
            .map_or(REST, |(group, _)| group);
        let file = files.iter_mut().find(|(name, ..)| *name == group).unwrap();
        file.1.push_str(&body);
        file.2.push(name.to_owned());
    }
    eager.push_str(&pending);
    if let Some((group, ..)) = files.iter().find(|(_, _, names)| names.is_empty()) {
        return Err(format!("runtime has no deferred functions for {group}"));
    }
    let groups = GROUPS
        .iter()
        .map(|(group, _)| *group)
        .collect::<Vec<_>>()
        .join(" ");
    eager.push_str(&LOADER.replace("@REST@", REST).replace("@GROUPS@", &groups));
    for (group, _, names) in &files {
        for name in names {
            eager.push_str(&format!(
                "{name}() {{ __binit_runtime_load {group} && {name} \"$@\"; }}\n"
            ));
        }
    }
    Ok((
        eager,
        files
            .into_iter()
            .map(|(group, text, _)| (format!("runtime-{group}.bash"), text))
            .collect(),
    ))
}

/// The name of a multi-line function definition that starts at column zero.
fn function_start(line: &str) -> Option<&str> {
    let name = line.trim_end_matches('\n').strip_suffix("() {")?;
    let mut chars = name.chars();
    let first = chars.next()?;
    ((first.is_ascii_alphabetic() || first == '_')
        && chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
        && name.len() < 64)
        .then_some(name)
}

#[cfg(test)]
mod tests {
    use super::split;

    const RUNTIME: &str = include_str!("../runtime/core.bash");

    #[test]
    fn keeps_every_function_once_and_stubs_the_deferred_ones() {
        let (eager, files) = split(RUNTIME).unwrap();
        let names: Vec<_> = files.iter().map(|(name, _)| name.as_str()).collect();
        assert_eq!(names, ["runtime-unload.bash", "runtime-more.bash"]);
        for line in RUNTIME.lines() {
            if let Some(name) = line.strip_suffix("() {") {
                let defined = |text: &str| text.lines().filter(|l| *l == line).count();
                let deferred: Vec<_> = files
                    .iter()
                    .filter(|(_, text)| defined(text) == 1)
                    .collect();
                assert_eq!(defined(&eager) + deferred.len(), 1, "{name}");
                for (file, _) in &deferred {
                    let group = &file["runtime-".len()..file.len() - ".bash".len()];
                    let stub =
                        format!("{name}() {{ __binit_runtime_load {group} && {name} \"$@\"; }}");
                    assert!(eager.contains(&stub), "{name}");
                }
                if deferred.is_empty() {
                    assert!(
                        !eager.contains(&format!("{name}() {{ __binit_runtime_load")),
                        "{name}"
                    );
                }
            }
        }
        // Deferred text holds only functions and their comments.
        for (_, text) in &files {
            let mut depth = 0;
            for line in text.lines() {
                if line.ends_with("() {") {
                    depth += 1;
                } else if line == "}" {
                    depth -= 1;
                } else if depth == 0 {
                    assert!(line.starts_with('#') || line.is_empty(), "{line}");
                }
            }
        }
        assert!(eager.contains("__binit_activate_plugins() {\n"));
        assert!(files[0].1.contains("__binit_unload() {\n"));
    }

    #[test]
    fn a_failed_load_retries_and_spares_the_caller() {
        let source = "binit() {\n    :\n}\n__binit_unload() {\n    echo unload\n}\n\
                      __binit_provider_forget() {\n    :\n}\n__binit_other() {\n    echo other\n}\n";
        let (eager, files) = split(source).unwrap();
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join("runtime.bash"), eager).unwrap();
        for (name, text) in files {
            std::fs::write(root.path().join(name), text).unwrap();
        }
        // Moving a file away makes its source fail, also for root, which ignores modes.
        let script = r#"
            __binit_generation_dir=$1
            source "$1/runtime.bash"
            set -o functrace
            trap 'group=custom' DEBUG
            mv "$1/runtime-unload.bash" "$1/away"
            __binit_unload 2>/dev/null && echo 'loaded a missing file'
            __binit_other || echo 'the caller failed with its group'
            mv "$1/away" "$1/runtime-unload.bash"
            __binit_unload
            trap - DEBUG
            echo "group=$group loaded=${__binit_runtime_loaded[more]},${__binit_runtime_loaded[unload]}"
        "#;
        // A caller's DEBUG trap that assigns a common name must not steer the group loop.
        let traced = r#"
            __binit_generation_dir=$1
            source "$1/runtime.bash"
            set -o functrace
            trap 'group=custom' DEBUG
            __binit_other
            trap - DEBUG
            echo "group=$group loaded=${__binit_runtime_loaded[more]},${__binit_runtime_loaded[unload]}"
        "#;
        for (script, expected) in [
            (script, "other\nunload\ngroup=custom loaded=1,1\n"),
            (traced, "other\ngroup=custom loaded=1,1\n"),
        ] {
            let output = std::process::Command::new("bash")
                .args(["--norc", "--noprofile", "-c", script, "bash"])
                .arg(root.path())
                .output()
                .unwrap();
            assert_eq!(
                String::from_utf8_lossy(&output.stdout),
                expected,
                "{}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
    }

    #[test]
    fn rejects_an_unterminated_function() {
        assert!(split("__binit_x() {\n    :\n").is_err());
        assert!(split("__binit_x() {\n    :\n__binit_y() {\n}\n").is_err());
    }
}
