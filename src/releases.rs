use super::*;

const MAX_METADATA: u64 = 2 * 1024 * 1024;
const MAX_ASSETS: usize = 1024;

#[derive(Deserialize)]
struct Release {
    tag_name: String,
    assets: Vec<Asset>,
}

#[derive(Deserialize)]
struct Asset {
    name: String,
    size: u64,
    digest: Option<String>,
}

fn repository(value: &str) -> Result<()> {
    let parts = value.split('/').collect::<Vec<_>>();
    if parts.len() != 2
        || parts.iter().any(|part| {
            part.is_empty()
                || part.len() > 100
                || part.starts_with(['.', '-'])
                || !part
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"-._".contains(&b))
        })
    {
        return Err("repository must be OWNER/REPO".into());
    }
    Ok(())
}

fn encoded(value: &str) -> String {
    value
        .bytes()
        .map(|byte| {
            if byte.is_ascii_alphanumeric() || b"-._~".contains(&byte) {
                (byte as char).to_string()
            } else {
                format!("%{byte:02X}")
            }
        })
        .collect()
}

/// A character a terminal would act on or reorder rather than show: controls and bidirectional marks.
pub(crate) fn terminal_control(c: char) -> bool {
    c.is_control()
        || matches!(c, '\u{061c}' | '\u{200e}' | '\u{200f}' | '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}')
}

fn printable(value: &str) -> bool {
    !value.is_empty() && value.len() <= 1024 && !value.chars().any(terminal_control)
}

fn matches(pattern: &str, text: &str) -> bool {
    let pattern = pattern.as_bytes();
    let text = text.as_bytes();
    let mut previous = vec![false; text.len() + 1];
    previous[0] = true;
    for byte in pattern {
        let mut current = vec![false; text.len() + 1];
        if *byte == b'*' {
            current[0] = previous[0];
        }
        for i in 1..=text.len() {
            current[i] = if *byte == b'*' {
                previous[i] || current[i - 1]
            } else {
                previous[i - 1] && (*byte == b'?' || *byte == text[i - 1])
            };
        }
        previous = current;
    }
    previous[text.len()]
}

fn platform(name: &str) -> &'static str {
    let lower = name.to_ascii_lowercase();
    let os = if lower.contains("linux") {
        "linux"
    } else if lower.contains("darwin") || lower.contains("macos") {
        "macos"
    } else if lower.contains("windows") || lower.contains("win64") {
        "windows"
    } else {
        "unknown"
    };
    let arch = if lower.contains("aarch64") || lower.contains("arm64") {
        "arm64"
    } else if lower.contains("x86_64") || lower.contains("amd64") {
        "x86_64"
    } else {
        "unknown"
    };
    match (os, arch) {
        ("linux", "x86_64") => "linux/x86_64",
        ("linux", "arm64") => "linux/arm64",
        ("macos", "x86_64") => "macos/x86_64",
        ("macos", "arm64") => "macos/arm64",
        ("windows", "x86_64") => "windows/x86_64",
        ("windows", "arm64") => "windows/arm64",
        _ => "unknown",
    }
}

pub(super) fn command(args: &[String]) -> Result<()> {
    let repo = args.first().ok_or("releases needs OWNER/REPO")?;
    repository(repo)?;
    let mut tag: Option<&str> = None;
    let mut pattern: Option<&str> = None;
    let mut target: Option<&str> = None;
    let mut index = 1;
    while index < args.len() {
        let value = args.get(index + 1).ok_or("release option needs a value")?;
        match args[index].as_str() {
            "--ver"
                if tag.is_none()
                    && printable(value)
                    && value.len() <= 200
                    && !value.chars().any(char::is_whitespace) =>
            {
                tag = Some(value)
            }
            "--asset" if pattern.is_none() && printable(value) && value.len() <= 256 => {
                pattern = Some(value)
            }
            "--platform" if target.is_none() => {
                target = Some(match value.as_str() {
                    "host" => match (std::env::consts::OS, std::env::consts::ARCH) {
                        ("linux", "x86_64") => "linux/x86_64",
                        ("linux", "aarch64") => "linux/arm64",
                        _ => {
                            return Err(
                                "host platform is not supported for release filtering".into()
                            );
                        }
                    },
                    "linux/x86_64" => "linux/x86_64",
                    "linux/arm64" => "linux/arm64",
                    _ => return Err("platform must be host, linux/x86_64, or linux/arm64".into()),
                });
            }
            _ => {
                return Err(
                    "usage: binit releases OWNER/REPO [--ver TAG|latest] [--asset GLOB] [--platform PLATFORM]".into(),
                );
            }
        }
        index += 2;
    }
    let path = match tag {
        None | Some("latest") => "latest".to_owned(),
        Some(value) => format!("tags/{}", encoded(value)),
    };
    let bytes = acquire::metadata(
        &format!("https://api.github.com/repos/{repo}/releases/{path}"),
        MAX_METADATA,
    )?;
    let release: Release =
        serde_json::from_slice(&bytes).map_err(|_| "invalid release metadata")?;
    if !printable(&release.tag_name)
        || release.assets.len() > MAX_ASSETS
        || release.assets.iter().any(|asset| !printable(&asset.name))
    {
        return Err("invalid or oversized release metadata".into());
    }
    if tag.is_some_and(|wanted| wanted != "latest" && wanted != release.tag_name) {
        return Err("release metadata tag does not match the requested tag".into());
    }
    println!("release  {} {}", repo, release.tag_name);
    let mut count = 0;
    for asset in release.assets {
        if pattern.is_some_and(|pattern| !matches(pattern, &asset.name)) {
            continue;
        }
        if target.is_some_and(|target| platform(&asset.name) != target) {
            continue;
        }
        let digest = asset
            .digest
            .as_deref()
            .and_then(|digest| digest.strip_prefix("sha256:"))
            .filter(|value| acquire::sha256_ok(value))
            .unwrap_or("not provided");
        println!(
            "{}\t{} bytes\t{}\tSHA256: {}",
            asset.name,
            asset.size,
            platform(&asset.name),
            digest
        );
        count += 1;
    }
    if count == 0 {
        println!("no matching assets");
    }
    Ok(())
}
