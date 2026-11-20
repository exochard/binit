use std::env;
use std::path::PathBuf;
use std::process::Command;

fn main() {
    for file in [
        "native/completion.c",
        "native/menu.c",
        "native/menu.h",
        "native/effects.c",
        "native/effects.h",
        "native/layout.c",
        "native/worker.c",
        "native/worker.h",
        "native/layout.h",
        "native/highlight.c",
        "native/highlight.h",
        "scripts/fetch_syntax.py",
        "native/syntax-probe/tree-source.json",
    ] {
        println!("cargo:rerun-if-changed={file}");
    }
    for name in [
        "CC",
        "BINIT_BASH_INCLUDE",
        "BINIT_READLINE_INCLUDE",
        "BINIT_SYNTAX_SOURCE",
        "BINIT_SYNTAX_RUNTIME",
    ] {
        println!("cargo:rerun-if-env-changed={name}");
    }
    if env::var_os("CARGO_FEATURE_COMPLETION_MENU").is_none() {
        return;
    }
    assert_eq!(env::var("CARGO_CFG_TARGET_OS").as_deref(), Ok("linux"));
    let headers = env::var_os("BINIT_BASH_INCLUDE")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/usr/include/bash"));
    assert!(
        headers.join("config.h").is_file(),
        "Bash development headers are required for the completion menu. Set BINIT_BASH_INCLUDE or build with --no-default-features."
    );
    println!(
        "cargo:rerun-if-changed={}",
        headers.join("config.h").display()
    );
    println!(
        "cargo:rerun-if-changed={}",
        headers.join("version.h").display()
    );
    println!("cargo:rerun-if-changed=/usr/include/readline/readline.h");
    let readline = env::var_os("BINIT_READLINE_INCLUDE")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/usr/include"));
    println!(
        "cargo:rerun-if-changed={}",
        readline.join("readline/readline.h").display()
    );
    let output = PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("completion.so");
    let mut command = Command::new(env::var_os("CC").unwrap_or_else(|| "cc".into()));
    command
        .args([
            "-std=c17",
            "-O2",
            "-g",
            "-Wall",
            "-Wextra",
            "-Wpedantic",
            "-Wconversion",
            "-Wshadow",
            "-Werror",
            "-fPIC",
            "-fvisibility=hidden",
            "-fstack-protector-strong",
            "-D_FORTIFY_SOURCE=3",
            "-shared",
            "-Wl,-z,relro,-z,now,-z,noexecstack",
        ])
        // Bash's own headers keep pre-C23 declarations; as system headers their warnings do not fail -Werror.
        .arg("-isystem")
        .arg(&headers)
        .arg("-isystem")
        .arg(&readline)
        .arg("-isystem")
        .arg(headers.join("include"))
        .arg("-isystem")
        .arg(headers.join("builtins"))
        .args([
            "native/completion.c",
            "native/menu.c",
            "native/effects.c",
            "native/layout.c",
            "native/worker.c",
        ]);
    if let Some(source) = env::var_os("BINIT_SYNTAX_SOURCE") {
        let source = PathBuf::from(source);
        let runtime = PathBuf::from(
            env::var_os("BINIT_SYNTAX_RUNTIME")
                .expect("set BINIT_SYNTAX_RUNTIME to the prepared pinned runtime"),
        );
        let status = Command::new("python3")
            .args(["scripts/fetch_syntax.py", "--verify"])
            .arg(&source)
            .arg("--runtime-prefix")
            .arg(&runtime)
            .status()
            .expect("python3 is required to verify syntax source");
        assert!(status.success(), "pinned syntax source verification failed");
        for file in [
            "src/parser.c",
            "src/scanner.c",
            "src/tree_sitter/parser.h",
            "src/tree_sitter/array.h",
            "src/tree_sitter/alloc.h",
            "LICENSE",
        ] {
            println!("cargo:rerun-if-changed={}", source.join(file).display());
        }
        command
            .arg("-DBINIT_HIGHLIGHT=1")
            .arg("native/highlight.c")
            .arg(format!(
                "-I{}",
                runtime.join("source/lib/include").display()
            ));
        for file in [
            "source.tar.gz",
            "runtime.json",
            "source/lib/include/tree_sitter/api.h",
            "lib/libtree-sitter.a",
        ] {
            println!("cargo:rerun-if-changed={}", runtime.join(file).display());
        }
        for unit in ["parser", "scanner"] {
            let object =
                PathBuf::from(env::var_os("OUT_DIR").unwrap()).join(format!("syntax-{unit}.o"));
            let status = Command::new(env::var_os("CC").unwrap_or_else(|| "cc".into()))
                .args(["-std=c11", "-O2", "-fPIC", "-w", "-c"])
                .arg(format!("-I{}", source.join("src").display()))
                .arg(source.join(format!("src/{unit}.c")))
                .arg("-o")
                .arg(&object)
                .status()
                .expect("could not compile pinned syntax grammar");
            assert!(
                status.success(),
                "could not compile pinned syntax grammar: {unit}"
            );
            command.arg(object);
        }
        command.arg(runtime.join("lib/libtree-sitter.a"));
    }
    let result = command
        .args(["-ldl", "-o"])
        .arg(output)
        .output()
        .expect("could not run the C compiler; set CC or install a compiler");
    assert!(
        result.status.success(),
        "native completion build failed:\n{}",
        String::from_utf8_lossy(&result.stderr)
    );
}
