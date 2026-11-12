use super::*;

#[derive(Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Origin {
    pub(super) url: String,
    pub(super) sha256: String,
    pub(super) format: String,
}

pub(super) fn is_source(source: &str) -> bool {
    source.starts_with("release:") || source.starts_with("snippet:")
}

fn component(value: &str) -> String {
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

pub(super) fn origin(argv: &[String]) -> Result<Origin> {
    let source = argv.first().ok_or("empty remote plugin line")?;
    let keys = package::line_keys(argv)?;
    let sha256 = keys.get("sha256").ok_or("remote plugins require sha256=")?;
    if !acquire::sha256_ok(sha256) {
        return Err("sha256= must be 64 hexadecimal characters".into());
    }
    if !keys.contains_key("id") {
        return Err("remote plugins require id=".into());
    }
    let (url, format) = if let Some(url) = source.strip_prefix("snippet:") {
        acquire::validate_url(url)?;
        if keys.contains_key("ver")
            || keys.contains_key("asset")
            || keys.contains_key("format")
            || keys.contains_key("bin")
            || keys.contains_key("files")
        {
            return Err("snippets do not accept ver=, asset=, format=, bin= or files=".into());
        }
        (url.to_owned(), "snippet".to_owned())
    } else if let Some(repository) = source.strip_prefix("release:") {
        let parts = repository.split('/').collect::<Vec<_>>();
        if parts.len() != 2
            || parts.iter().any(|part| {
                part.is_empty()
                    || *part == "."
                    || *part == ".."
                    || !part
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"-._".contains(&b))
            })
        {
            return Err("release source must be release:OWNER/REPO".into());
        }
        let tag = keys
            .get("ver")
            .filter(|v| !v.is_empty())
            .ok_or("release plugins require ver=TAG")?;
        let asset = keys
            .get("asset")
            .filter(|v| !v.is_empty())
            .ok_or("release plugins require asset=NAME")?;
        if asset.contains('/')
            || asset.contains('\\')
            || tag
                .chars()
                .chain(asset.chars())
                .any(|c| c.is_control() || c.is_whitespace())
        {
            return Err("release tag and asset must not contain whitespace or control characters; asset must be a file name".into());
        }
        if !keys.contains_key("bin") {
            return Err("release plugins require bin=PATH[:NAME]".into());
        }
        let inferred = if asset.ends_with(".tar.gz") || asset.ends_with(".tgz") {
            "tar.gz"
        } else if asset.ends_with(".tar") {
            "tar"
        } else if asset.ends_with(".zip") {
            "zip"
        } else {
            "raw"
        };
        let format = keys.get("format").copied().unwrap_or(inferred);
        if !["raw", "tar", "tar.gz", "zip"].contains(&format) {
            return Err("format= must be raw, tar, tar.gz or zip".into());
        }
        (
            format!(
                "https://github.com/{repository}/releases/download/{}/{}",
                component(tag),
                component(asset)
            ),
            format.to_owned(),
        )
    } else {
        return Err("unknown remote source".into());
    };
    Ok(Origin {
        url,
        sha256: sha256.to_ascii_lowercase(),
        format,
    })
}

pub(super) fn materialize(
    state: &Path,
    argv: &[String],
    origin: &Origin,
) -> Result<BTreeMap<String, Vec<u8>>> {
    let limit = if origin.format == "snippet" {
        MAX_BYTES
    } else {
        acquire::MAX_DOWNLOAD_BYTES
    };
    let assets = state.join("assets");
    private_dir(&assets)?;
    let path = assets.join(&origin.sha256);
    let bytes = if fs::symlink_metadata(&path).is_ok() {
        let bytes = read_regular_limit(&path, limit)?;
        if hash(&bytes) != origin.sha256 {
            return Err("cached remote source checksum mismatch".into());
        }
        bytes
    } else {
        let bytes = acquire::fetch(&origin.url, &origin.sha256, limit)?;
        atomic_write(&path, &bytes)?;
        bytes
    };
    if origin.format == "snippet" {
        return Ok(BTreeMap::from([("snippet.bash".into(), bytes)]));
    }
    if origin.format == "raw" {
        if bytes.len() as u64 > acquire::MAX_PROGRAM_BYTES {
            return Err("release program exceeds 32 MiB".into());
        }
        let keys = package::line_keys(argv)?;
        return Ok(BTreeMap::from([(keys["asset"].to_string(), bytes)]));
    }
    acquire::unpack(&bytes, &origin.format)
}
