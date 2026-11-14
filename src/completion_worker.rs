//! Private execution boundary for explicitly eligible, single-process providers.
use std::ffi::OsString;

#[cfg(all(
    target_os = "linux",
    target_pointer_width = "64",
    target_endian = "little",
    any(target_arch = "x86_64", target_arch = "aarch64")
))]
mod linux {
    use super::OsString;
    use std::io::{self, Read, Seek, SeekFrom, Write};
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::process::CommandExt;
    use std::path::Path;
    use std::process::Command;

    fn checked(result: libc::c_int) -> io::Result<()> {
        if result == -1 {
            Err(io::Error::last_os_error())
        } else {
            Ok(())
        }
    }

    fn limit(resource: libc::__rlimit_resource_t, maximum: libc::rlim_t) -> io::Result<()> {
        let mut current = libc::rlimit {
            rlim_cur: 0,
            rlim_max: 0,
        };
        // Only this newly executed helper is changed, never the interactive shell.
        unsafe {
            checked(libc::getrlimit(resource, &mut current))?;
            let value = maximum.min(current.rlim_cur).min(current.rlim_max);
            checked(libc::setrlimit(
                resource,
                &libc::rlimit {
                    rlim_cur: value,
                    rlim_max: value,
                },
            ))
        }
    }

    fn stage_context() -> crate::Result<()> {
        let mut context = Vec::new();
        io::stdin().lock().take(65537).read_to_end(&mut context)?;
        if context.len() > 65536 {
            return Err("completion worker context exceeds its limit".into());
        }
        // Anonymous sealed input keeps completion words out of argv and named files.
        let descriptor = unsafe {
            libc::memfd_create(
                c"binit-completion".as_ptr(),
                libc::MFD_CLOEXEC | libc::MFD_ALLOW_SEALING,
            )
        };
        if descriptor < 0 {
            return Err(io::Error::last_os_error().into());
        }
        let mut input = unsafe { std::fs::File::from_raw_fd(descriptor) };
        input.write_all(&context)?;
        input.seek(SeekFrom::Start(0))?;
        unsafe {
            checked(libc::fcntl(
                input.as_raw_fd(),
                libc::F_ADD_SEALS,
                libc::F_SEAL_SEAL | libc::F_SEAL_SHRINK | libc::F_SEAL_GROW | libc::F_SEAL_WRITE,
            ))?;
            checked(libc::dup2(input.as_raw_fd(), libc::STDIN_FILENO))?;
        }
        Ok(())
    }

    fn statement(code: u32, value: u32) -> libc::sock_filter {
        libc::sock_filter {
            code: code as u16,
            jt: 0,
            jf: 0,
            k: value,
        }
    }

    fn equal(value: u32, yes: u8, no: u8) -> libc::sock_filter {
        libc::sock_filter {
            code: (libc::BPF_JMP | libc::BPF_JEQ | libc::BPF_K) as u16,
            jt: yes,
            jf: no,
            k: value,
        }
    }

    fn filter() -> io::Result<()> {
        let load = libc::BPF_LD | libc::BPF_W | libc::BPF_ABS;
        let ret = libc::BPF_RET | libc::BPF_K;
        let denied = libc::SECCOMP_RET_ERRNO | libc::EPERM as u32;
        #[cfg(target_arch = "x86_64")]
        let architecture = 0xc000_003e; // Linux AUDIT_ARCH_X86_64.
        #[cfg(target_arch = "aarch64")]
        let architecture = 0xc000_00b7; // Linux AUDIT_ARCH_AARCH64.
        let args = std::mem::offset_of!(libc::seccomp_data, args) as u32;
        let mut instructions = vec![
            statement(load, std::mem::offset_of!(libc::seccomp_data, arch) as u32),
            equal(architecture, 1, 0),
            statement(ret, libc::SECCOMP_RET_KILL_PROCESS),
            statement(load, std::mem::offset_of!(libc::seccomp_data, nr) as u32),
        ];
        // x32 shares the x86-64 audit architecture but has a different syscall ABI.
        #[cfg(target_arch = "x86_64")]
        instructions.extend([
            libc::sock_filter {
                code: (libc::BPF_JMP | libc::BPF_JSET | libc::BPF_K) as u16,
                jt: 0,
                jf: 1,
                k: 0x4000_0000,
            },
            statement(ret, denied),
        ]);
        let blocked = [
            libc::SYS_clone,
            libc::SYS_clone3,
            libc::SYS_setrlimit,
            libc::SYS_ptrace,
            #[cfg(target_arch = "x86_64")]
            libc::SYS_fork,
            #[cfg(target_arch = "x86_64")]
            libc::SYS_vfork,
        ];
        for syscall in blocked {
            instructions.extend([equal(syscall as u32, 0, 1), statement(ret, denied)]);
        }
        // Keep getrlimit/prlimit queries usable, but freeze both resource limits.
        instructions.extend([
            equal(libc::SYS_prlimit64 as u32, 0, 5),
            statement(load, args + 16),
            equal(0, 0, 2),
            statement(load, args + 20),
            equal(0, 1, 0),
            statement(ret, denied),
            // Reload nr after the argument reads above.
            statement(load, std::mem::offset_of!(libc::seccomp_data, nr) as u32),
            equal(libc::SYS_prctl as u32, 0, 3),
            statement(load, args),
            equal(libc::PR_SET_PDEATHSIG as u32, 0, 1),
            statement(ret, denied),
            statement(ret, libc::SECCOMP_RET_ALLOW),
        ]);
        let program = libc::sock_fprog {
            len: instructions
                .len()
                .try_into()
                .expect("bounded worker filter"),
            filter: instructions.as_mut_ptr(),
        };
        // No privilege gain across exec; the kernel copies the filter before return.
        unsafe {
            checked(libc::prctl(
                libc::PR_SET_NO_NEW_PRIVS,
                1 as libc::c_ulong,
                0 as libc::c_ulong,
                0 as libc::c_ulong,
                0 as libc::c_ulong,
            ))?;
            checked(libc::prctl(
                libc::PR_SET_SECCOMP,
                libc::SECCOMP_MODE_FILTER as libc::c_ulong,
                &program,
                0 as libc::c_ulong,
                0 as libc::c_ulong,
            ))
        }
    }

    pub(super) fn run(arguments: &[OsString]) -> crate::Result<()> {
        if arguments.len() < 2 || !Path::new(&arguments[1]).is_absolute() {
            return Err("invalid completion worker request".into());
        }
        let parent: libc::pid_t = arguments[0]
            .to_str()
            .ok_or("invalid worker parent")?
            .parse()?;
        if parent <= 1
            || arguments
                .iter()
                .map(|arg| arg.as_bytes().len() + 1)
                .sum::<usize>()
                > 65536
        {
            return Err("completion worker context exceeds its limit".into());
        }
        unsafe {
            checked(libc::prctl(
                libc::PR_SET_PDEATHSIG,
                libc::SIGKILL as libc::c_ulong,
                0 as libc::c_ulong,
                0 as libc::c_ulong,
                0 as libc::c_ulong,
            ))?;
            if libc::getppid() != parent {
                return Err("completion worker parent changed".into());
            }
            // Fail closed when close_range is unavailable; no inherited private FDs.
            if libc::syscall(libc::SYS_close_range, 3u32, u32::MAX, 0u32) == -1 {
                return Err(io::Error::last_os_error().into());
            }
        }
        limit(libc::RLIMIT_AS, 256 * 1024 * 1024)?;
        limit(libc::RLIMIT_CPU, 2)?;
        limit(libc::RLIMIT_NOFILE, 32)?;
        limit(libc::RLIMIT_CORE, 0)?;
        stage_context()?;
        filter()?;
        Err(Command::new(&arguments[1])
            .args(&arguments[2..])
            .exec()
            .into())
    }
}

pub(crate) fn run(arguments: &[OsString]) -> crate::Result<()> {
    #[cfg(all(
        target_os = "linux",
        target_pointer_width = "64",
        target_endian = "little",
        any(target_arch = "x86_64", target_arch = "aarch64")
    ))]
    return linux::run(arguments);
    #[cfg(not(all(
        target_os = "linux",
        target_pointer_width = "64",
        target_endian = "little",
        any(target_arch = "x86_64", target_arch = "aarch64")
    )))]
    {
        let _ = arguments;
        Err("eligible completion workers are unavailable on this platform".into())
    }
}
