//! Splits the shell runtime so a shell start parses only what startup uses.
//!
//! Bash spends most of the runtime's load time building function bodies. Functions outside
//! `EAGER` go to `runtime-more.bash`, and `runtime.bash` keeps a one-line stub for each; the
//! first call of any stub sources the rest, which replaces every stub with its definition.

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

/// Returns the eager runtime, with the loader and stubs appended, and the deferred functions.
pub fn split(source: &str) -> Result<(String, String), String> {
    let mut eager = String::new();
    let mut deferred = String::new();
    let mut names = Vec::new();
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
        } else {
            deferred.push_str(&body);
            names.push(name.to_owned());
        }
    }
    eager.push_str(&pending);
    if names.is_empty() {
        return Err("runtime has no deferred functions".into());
    }
    eager.push_str(
        "\n# The functions below load from runtime-more.bash on first use; that file redefines each one.\n\
         __binit_runtime_more_file=$__binit_generation_dir/runtime-more.bash\n\
         [[ $__binit_runtime_more_file == /* ]] || __binit_runtime_more_file=$PWD/$__binit_runtime_more_file\n\
         __binit_runtime_more() {\n    [[ -z ${__binit_runtime_more_loaded-} ]] || return 1\n    __binit_runtime_more_loaded=1\n    \
         source \"$__binit_runtime_more_file\"\n}\n",
    );
    for name in &names {
        eager.push_str(&format!(
            "{name}() {{ __binit_runtime_more && {name} \"$@\"; }}\n"
        ));
    }
    Ok((eager, deferred))
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
        let (eager, deferred) = split(RUNTIME).unwrap();
        for line in RUNTIME.lines() {
            if let Some(name) = line.strip_suffix("() {") {
                let defined = |text: &str| text.lines().filter(|l| *l == line).count();
                assert_eq!(defined(&eager) + defined(&deferred), 1, "{name}");
                let stub = format!("{name}() {{ __binit_runtime_more && {name} \"$@\"; }}");
                assert_eq!(eager.contains(&stub), defined(&deferred) == 1, "{name}");
            }
        }
        // Deferred text holds only functions and their comments.
        let mut depth = 0;
        for line in deferred.lines() {
            if line.ends_with("() {") {
                depth += 1;
            } else if line == "}" {
                depth -= 1;
            } else if depth == 0 {
                assert!(line.starts_with('#') || line.is_empty(), "{line}");
            }
        }
        assert!(eager.contains("__binit_activate_plugins() {\n"));
        assert!(deferred.contains("__binit_unload() {\n"));
    }

    #[test]
    fn rejects_an_unterminated_function() {
        assert!(split("__binit_x() {\n    :\n").is_err());
        assert!(split("__binit_x() {\n    :\n__binit_y() {\n}\n").is_err());
    }
}
