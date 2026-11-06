use std::env;
use std::path::PathBuf;
use std::process::Command;

fn main() {
    for file in ["native/completion.c", "native/menu.c", "native/menu.h"] {
        println!("cargo:rerun-if-changed={file}");
    }
    for name in ["CC", "BINIT_BASH_INCLUDE"] {
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
    println!("cargo:rerun-if-changed=/usr/include/readline/readline.h");
    let output = PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("completion.so");
    let result = Command::new(env::var_os("CC").unwrap_or_else(|| "cc".into()))
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
        .arg(format!("-I{}", headers.display()))
        .arg(format!("-I{}", headers.join("include").display()))
        .arg(format!("-I{}", headers.join("builtins").display()))
        .args(["native/completion.c", "native/menu.c", "-ldl", "-o"])
        .arg(output)
        .output()
        .expect("could not run the C compiler; set CC or install a compiler");
    assert!(
        result.status.success(),
        "native completion build failed:\n{}",
        String::from_utf8_lossy(&result.stderr)
    );
}
