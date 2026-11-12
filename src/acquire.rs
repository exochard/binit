use super::*;
use std::io::Cursor;
use std::process::{Command, Stdio};

pub(super) const MAX_DOWNLOAD_BYTES: u64 = 64 * 1024 * 1024;
pub(super) const MAX_EXPANDED_BYTES: u64 = 64 * 1024 * 1024;
pub(super) const MAX_PROGRAM_BYTES: u64 = 32 * 1024 * 1024;

pub(super) fn sha256_ok(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

pub(super) fn validate_url(url: &str) -> Result<()> {
    let invalid = "invalid HTTPS source; credentials and whitespace are not allowed";
    let rest = url.strip_prefix("https://").ok_or(invalid)?;
    if !url.is_ascii()
        || url
            .bytes()
            .any(|b| b.is_ascii_control() || b.is_ascii_whitespace() || b == b'\\')
    {
        return Err(invalid.into());
    }
    let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
    if authority.is_empty()
        || authority.contains('@')
        || authority.starts_with('-')
        || url.contains('#')
    {
        return Err(invalid.into());
    }
    Ok(())
}

pub(super) fn fetch(url: &str, expected_sha256: &str, limit: u64) -> Result<Vec<u8>> {
    fetch_using(std::ffi::OsStr::new("curl"), url, expected_sha256, limit)
}

fn fetch_using(
    program: &std::ffi::OsStr,
    url: &str,
    expected_sha256: &str,
    limit: u64,
) -> Result<Vec<u8>> {
    validate_url(url)?;
    if !sha256_ok(expected_sha256) || limit == 0 || limit > MAX_DOWNLOAD_BYTES {
        return Err("invalid SHA-256 or download size limit".into());
    }
    let bytes = request_using(program, url, limit)?;
    if format!("{:x}", Sha256::digest(&bytes)) != expected_sha256.to_ascii_lowercase() {
        return Err("download SHA-256 mismatch".into());
    }
    Ok(bytes)
}

pub(super) fn metadata(url: &str, limit: u64) -> Result<Vec<u8>> {
    request_using(std::ffi::OsStr::new("curl"), url, limit)
}

fn request_using(program: &std::ffi::OsStr, url: &str, limit: u64) -> Result<Vec<u8>> {
    validate_url(url)?;
    if limit == 0 || limit > MAX_DOWNLOAD_BYTES {
        return Err("invalid download size limit".into());
    }
    let mut child = Command::new(program)
        .args([
            "-q",
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--max-time",
            "120",
            "--connect-timeout",
            "20",
            "--user-agent",
            "binit",
            "--",
            url,
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| "cannot start HTTPS download")?;
    let mut bytes = Vec::new();
    let output = child.stdout.take().ok_or("download output unavailable")?;
    let read = output.take(limit + 1).read_to_end(&mut bytes);
    if read.is_err() || bytes.len() as u64 > limit {
        let _ = child.kill();
        let _ = child.wait();
        return Err("HTTPS download failed or exceeded its size limit".into());
    }
    if !child.wait()?.success() {
        return Err("HTTPS download failed".into());
    }
    Ok(bytes)
}

fn archive_name(raw: &[u8]) -> Result<String> {
    let text = std::str::from_utf8(raw).map_err(|_| "archive path is not UTF-8")?;
    if text.len() > 4096
        || text.starts_with('/')
        || text.contains('\\')
        || text.bytes().any(|byte| byte.is_ascii_control())
    {
        return Err("unsafe archive path".into());
    }
    let text = text.trim_start_matches("./").trim_end_matches('/');
    if text.is_empty()
        || text.split('/').count() > 16
        || text
            .split('/')
            .any(|part| part.is_empty() || part == "." || part == ".." || part.contains(':'))
    {
        return Err("unsafe archive path".into());
    }
    Ok(text.to_owned())
}

fn read_entry(reader: &mut impl Read, declared: u64, total: &mut u64) -> Result<Vec<u8>> {
    if declared > MAX_EXPANDED_BYTES.saturating_sub(*total) {
        return Err("archive exceeds expanded size limit".into());
    }
    let mut bytes = Vec::new();
    reader
        .take(MAX_EXPANDED_BYTES - *total + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 != declared || bytes.len() as u64 > MAX_EXPANDED_BYTES - *total {
        return Err("archive entry has an invalid or oversized length".into());
    }
    *total += bytes.len() as u64;
    Ok(bytes)
}

fn unpack_tar(reader: impl Read) -> Result<BTreeMap<String, Vec<u8>>> {
    let mut archive = tar::Archive::new(reader);
    let mut files = BTreeMap::new();
    let mut seen = BTreeSet::new();
    let mut total = 0;
    let mut count = 0;
    let mut root_seen = false;
    for item in archive.entries()?.raw(true) {
        let mut entry = item?;
        count += 1;
        if count > MAX_FILES {
            return Err("archive has too many entries".into());
        }
        let kind = entry.header().entry_type();
        if !kind.is_file() && !kind.is_dir() {
            return Err("archive contains a link or unsupported entry".into());
        }
        let raw = entry.path_bytes();
        if kind.is_dir() && matches!(raw.as_ref(), b"." | b"./") {
            if root_seen || entry.size() != 0 {
                return Err("invalid or duplicate archive root".into());
            }
            root_seen = true;
            continue;
        }
        let name = archive_name(raw.as_ref())?;
        if !seen.insert(name.clone()) || seen.len() > MAX_FILES {
            return Err("archive contains duplicate paths or too many entries".into());
        }
        if kind.is_file() {
            let length = entry.size();
            files.insert(name, read_entry(&mut entry, length, &mut total)?);
        } else if entry.size() != 0 {
            return Err("archive directory has data".into());
        }
    }
    Ok(files)
}

pub(super) fn unpack(bytes: &[u8], format: &str) -> Result<BTreeMap<String, Vec<u8>>> {
    if bytes.len() as u64 > MAX_DOWNLOAD_BYTES {
        return Err("archive exceeds download size limit".into());
    }
    let files = match format {
        "tar" => unpack_tar(Cursor::new(bytes)),
        "tar.gz" => unpack_tar(flate2::read::GzDecoder::new(Cursor::new(bytes))),
        "zip" => {
            let mut archive = zip::ZipArchive::new(Cursor::new(bytes))?;
            if archive.len() > MAX_FILES {
                return Err("archive has too many entries".into());
            }
            let mut files = BTreeMap::new();
            let mut seen = BTreeSet::new();
            let mut total = 0;
            let mut root_seen = false;
            for index in 0..archive.len() {
                let mut entry = archive.by_index(index)?;
                if entry.encrypted() {
                    return Err("encrypted archive entries are not supported".into());
                }
                let mode = entry.unix_mode().unwrap_or(0) & 0o170000;
                if mode != 0 && mode != 0o100000 && mode != 0o040000 {
                    return Err("archive contains a link or unsupported entry".into());
                }
                if entry.is_dir() && matches!(entry.name_raw(), b"." | b"./") {
                    if root_seen || entry.size() != 0 || mode == 0o100000 {
                        return Err("invalid or duplicate archive root".into());
                    }
                    root_seen = true;
                    continue;
                }
                let name = archive_name(entry.name_raw())?;
                if !seen.insert(name.clone()) {
                    return Err("archive contains duplicate paths".into());
                }
                if entry.is_dir() {
                    if entry.size() != 0 || mode == 0o100000 {
                        return Err("invalid archive directory".into());
                    }
                } else {
                    if mode == 0o040000 {
                        return Err("invalid archive file".into());
                    }
                    let length = entry.size();
                    files.insert(name, read_entry(&mut entry, length, &mut total)?);
                }
            }
            Ok(files)
        }
        _ => Err("unsupported archive format; use tar, tar.gz or zip".into()),
    }?;
    if files.is_empty() {
        return Err("archive contains no regular files".into());
    }
    Ok(files)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn https_sources_and_digests_are_checked_without_echoing_credentials() {
        assert!(validate_url("https://example.org/a.tar.gz?x=1").is_ok());
        for url in [
            "http://example.org",
            "https://user:secret@example.org/x",
            "https://example.org/ bad",
            "https://example.org/#fragment",
            "https://example.org\\evil",
        ] {
            let error = validate_url(url).unwrap_err().to_string();
            assert!(!error.contains("secret"));
        }
        assert!(sha256_ok(&"a".repeat(64)));
        assert!(!sha256_ok(&"g".repeat(64)));
    }

    #[test]
    fn https_fetch_enforces_checksum_size_and_child_failure() {
        use std::os::unix::fs::PermissionsExt;
        let directory = tempfile::tempdir().unwrap();
        let program = directory.path().join("curl fixture");
        std::fs::write(&program, "#!/bin/sh\nprintf payload\n").unwrap();
        std::fs::set_permissions(&program, std::fs::Permissions::from_mode(0o700)).unwrap();
        let digest = format!("{:x}", Sha256::digest(b"payload"));
        let download = |checksum: &str, limit| {
            fetch_using(
                program.as_os_str(),
                "https://example.org/file",
                checksum,
                limit,
            )
        };
        assert_eq!(download(&digest, 7).unwrap(), b"payload");
        assert!(
            download(&digest, 6)
                .unwrap_err()
                .to_string()
                .contains("size limit")
        );
        assert!(
            download(&"0".repeat(64), 7)
                .unwrap_err()
                .to_string()
                .contains("SHA-256 mismatch")
        );
        assert!(download("bad", 7).is_err());
        std::fs::write(&program, "#!/bin/sh\nexit 7\n").unwrap();
        assert!(
            download(&digest, 7)
                .unwrap_err()
                .to_string()
                .contains("download failed")
        );
    }

    fn tar_file(name: &str, kind: tar::EntryType, payload: &[u8]) -> Vec<u8> {
        let mut header = tar::Header::new_gnu();
        header.set_path("placeholder").unwrap();
        header.set_entry_type(kind);
        header.set_mode(0o644);
        header.set_size(payload.len() as u64);
        let field = &mut header.as_mut_bytes()[..100];
        field.fill(0);
        field[..name.len()].copy_from_slice(name.as_bytes());
        header.set_cksum();
        let mut builder = tar::Builder::new(Vec::new());
        builder.append(&header, payload).unwrap();
        builder.finish().unwrap();
        builder.into_inner().unwrap()
    }

    #[test]
    fn tar_and_gzip_extract_bounded_regular_files() {
        let archive = tar_file("./bin/tool", tar::EntryType::Regular, b"hello");
        assert_eq!(unpack(&archive, "tar").unwrap()["bin/tool"], b"hello");
        let root = tar_file("./", tar::EntryType::Directory, b"");
        let mut with_root = root[..root.len() - 1024].to_vec();
        with_root.extend_from_slice(&archive);
        assert_eq!(unpack(&with_root, "tar").unwrap()["bin/tool"], b"hello");
        assert!(unpack(b"", "tar").is_err());
        let mut encoder = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        encoder.write_all(&archive).unwrap();
        let gzip = encoder.finish().unwrap();
        assert_eq!(unpack(&gzip, "tar.gz").unwrap()["bin/tool"], b"hello");
        assert!(unpack(&gzip[..gzip.len() / 2], "tar.gz").is_err());
    }

    #[test]
    fn tar_rejects_unsafe_entries_before_extraction() {
        for name in [
            "../escape",
            "/absolute",
            "a/../../escape",
            "a\\escape",
            "C:/escape",
        ] {
            assert!(
                unpack(&tar_file(name, tar::EntryType::Regular, b"x"), "tar").is_err(),
                "{name}"
            );
        }
        for kind in [
            tar::EntryType::Symlink,
            tar::EntryType::Link,
            tar::EntryType::Fifo,
            tar::EntryType::XHeader,
        ] {
            assert!(unpack(&tar_file("unsafe", kind, b""), "tar").is_err());
        }
        let mut header = tar::Header::new_gnu();
        header.set_path("huge").unwrap();
        header.set_entry_type(tar::EntryType::Regular);
        header.set_size(MAX_EXPANDED_BYTES + 1);
        header.set_cksum();
        let mut bytes = header.as_bytes().to_vec();
        bytes.extend([0; 1024]);
        assert!(unpack(&bytes, "tar").is_err());
    }

    fn zip_files(names: &[&str]) -> Vec<u8> {
        let mut writer = zip::ZipWriter::new(Cursor::new(Vec::new()));
        for name in names {
            writer
                .start_file(
                    *name,
                    zip::write::SimpleFileOptions::default()
                        .compression_method(zip::CompressionMethod::Deflated),
                )
                .unwrap();
            writer.write_all(b"content").unwrap();
        }
        writer.finish().unwrap().into_inner()
    }

    #[test]
    fn zip_checks_paths_duplicates_and_integrity() {
        assert_eq!(
            unpack(&zip_files(&["bin/tool"]), "zip").unwrap()["bin/tool"],
            b"content"
        );
        assert!(unpack(&zip_files(&["../escape"]), "zip").is_err());
        assert!(unpack(&zip_files(&["tool", "./tool"]), "zip").is_err());
        let mut symlink = zip::ZipWriter::new(Cursor::new(Vec::new()));
        symlink
            .add_symlink(
                "bin/tool",
                "../../escape",
                zip::write::SimpleFileOptions::default(),
            )
            .unwrap();
        let symlink = symlink.finish().unwrap().into_inner();
        assert!(unpack(&symlink, "zip").is_err());
        let bytes = zip_files(&["tool"]);
        assert!(unpack(&bytes[..bytes.len() / 2], "zip").is_err());
        assert!(unpack(b"not an archive", "zip").is_err());
        assert!(unpack(b"", "rar").is_err());
    }
}
