use super::*;
use std::os::unix::process::ExitStatusExt;
use std::time::Duration;

mod runtime;

const MAX_PLAN_BYTES: u64 = 64 * 1024;
const MAX_INPUT_BYTES: usize = 8 * 1024 * 1024;
pub(super) const MAX_RECEIPT_BYTES: u64 = 16 * 1024 * 1024;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub(super) struct FileIdentity {
    pub path: PathBuf,
    pub sha256: String,
}

pub(super) fn manager_identity() -> Result<FileIdentity> {
    runtime::manager_identity()
}

#[derive(Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub(super) struct Subject {
    pub generation: String,
    pub base_generation: Option<String>,
    pub config: FileIdentity,
    pub manager: FileIdentity,
}

fn one() -> u32 {
    1
}
fn timeout() -> u64 {
    5000
}
fn view_budget() -> u64 {
    256 * 1024 * 1024
}

#[derive(Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Spec {
    id: String,
    script: String,
    #[serde(default)]
    setup: Option<String>,
    #[serde(default = "one")]
    samples: u32,
    #[serde(default = "timeout")]
    timeout_ms: u64,
    #[serde(default)]
    max_elapsed_ms: Option<u64>,
}

#[derive(Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Plan {
    schema: u32,
    #[serde(default)]
    files: Vec<String>,
    #[serde(default)]
    native: Option<bool>,
    #[serde(default = "view_budget")]
    max_generation_bytes: u64,
    checks: Vec<Spec>,
}

#[derive(Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Input {
    relative: String,
    sha256: String,
}

#[derive(Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Binding {
    plan_file: FileIdentity,
    plan: Plan,
    inputs: Vec<Input>,
}

struct ReadInputs {
    binding: Binding,
    bytes: BTreeMap<String, Vec<u8>>,
}

#[derive(Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Context {
    binding: Binding,
    runtime: runtime::RuntimeIdentity,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Sample {
    check: String,
    sample: u32,
    phase: String,
    check_return: Option<i32>,
    elapsed_ms: f64,
    code: Option<i32>,
    signal: Option<i32>,
    timed_out: bool,
    interrupted: i32,
    output_complete: bool,
    generation_unchanged: bool,
    stdout_preview: String,
    stderr_preview: String,
    stdout_truncated: bool,
    stderr_truncated: bool,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Receipt {
    schema: u32,
    pub subject: Subject,
    complete: bool,
    context: Option<Context>,
    pub error: Option<String>,
    samples: Vec<Sample>,
}

fn relative(name: &str) -> bool {
    valid_relative(name)
        && Path::new(name)
            .components()
            .collect::<PathBuf>()
            .as_os_str()
            == Path::new(name).as_os_str()
}

fn bind(path: &Path) -> Result<ReadInputs> {
    let path = fs::canonicalize(path)?;
    let bytes = read_regular_limit(&path, MAX_PLAN_BYTES)?;
    let plan: Plan = toml::from_str(std::str::from_utf8(&bytes)?)?;
    if plan.schema != 1
        || plan.checks.is_empty()
        || plan.checks.len() > 16
        || plan.files.len() > MAX_FILES
        || plan.max_generation_bytes == 0
        || plan.max_generation_bytes > 8 * 1024 * 1024 * 1024
    {
        return Err("invalid check schema or resource limits".into());
    }
    let mut names: BTreeSet<String> = plan.files.iter().cloned().collect();
    let mut ids = BTreeSet::new();
    let mut samples = 0u32;
    for spec in &plan.checks {
        if !valid_name(&spec.id)
            || !ids.insert(&spec.id)
            || !(1..=40).contains(&spec.samples)
            || !(1..=120000).contains(&spec.timeout_ms)
            || spec
                .max_elapsed_ms
                .is_some_and(|limit| limit == 0 || limit > spec.timeout_ms)
        {
            return Err("invalid check identifier, sample count or time limit".into());
        }
        samples += spec.samples;
        names.insert(spec.script.clone());
        if let Some(setup) = &spec.setup {
            names.insert(setup.clone());
        }
    }
    if samples > 40 || names.len() > MAX_FILES || names.iter().any(|name| !relative(name)) {
        return Err("checks exceed their sample/file limits or use invalid paths".into());
    }
    let mut inputs = Vec::new();
    let mut contents = BTreeMap::new();
    let mut total = 0usize;
    for name in names {
        let mut source = path
            .parent()
            .ok_or("check plan has no parent")?
            .to_path_buf();
        for component in Path::new(&name).components() {
            source.push(component.as_os_str());
            if fs::symlink_metadata(&source)?.file_type().is_symlink() {
                return Err("symlinks in check inputs are refused".into());
            }
        }
        let bytes = read_regular(&source)?;
        total = total
            .checked_add(bytes.len())
            .ok_or("check input size overflow")?;
        if total > MAX_INPUT_BYTES {
            return Err("check inputs exceed 8 MiB".into());
        }
        inputs.push(Input {
            relative: name.clone(),
            sha256: hash(&bytes),
        });
        contents.insert(name, bytes);
    }
    Ok(ReadInputs {
        binding: Binding {
            plan_file: FileIdentity {
                path,
                sha256: hash(&bytes),
            },
            plan,
            inputs,
        },
        bytes: contents,
    })
}

fn input_snapshot(root: &Path, inputs: &ReadInputs) -> Result<()> {
    private_dir(root)?;
    for (name, bytes) in &inputs.bytes {
        let mut parent = root.to_path_buf();
        for part in Path::new(name)
            .parent()
            .ok_or("check input has no parent")?
            .components()
        {
            parent.push(part.as_os_str());
            private_dir(&parent)?;
        }
        atomic_write_mode(&root.join(name), bytes, 0o400)?;
    }
    Ok(())
}

fn snapshot_unchanged(root: &Path, binding: &Binding) -> Result<()> {
    for input in &binding.inputs {
        if hash(&read_regular(&root.join(&input.relative))?) != input.sha256 {
            return Err("check input snapshot changed during execution".into());
        }
    }
    Ok(())
}

fn copy_generation(state: &Path, view: &Path, generation: &str, budget: u64) -> Result<PathBuf> {
    let lock = verify_generation(state, generation)?;
    let source = state.join("generations").join(generation);
    let names = lock
        .files
        .keys()
        .cloned()
        .chain(std::iter::once("lock.toml".to_owned()))
        .collect::<Vec<_>>();
    let mut size = 0u64;
    for name in &names {
        size = size
            .checked_add(fs::metadata(source.join(name))?.len())
            .ok_or("generation size overflow")?;
        if size > budget {
            return Err("generation exceeds the declared temporary-view budget".into());
        }
    }
    private_dir(view)?;
    private_dir(&view.join("generations"))?;
    let destination = view.join("generations").join(generation);
    private_dir(&destination)?;
    for name in names {
        let mut parent = destination.clone();
        for part in Path::new(&name)
            .parent()
            .ok_or("generation path has no parent")?
            .components()
        {
            parent.push(part.as_os_str());
            private_dir(&parent)?;
        }
        fs::copy(source.join(&name), destination.join(&name))?;
    }
    verify_generation(view, generation)?;
    atomic_write(&view.join("current"), generation.as_bytes())?;
    Ok(destination)
}

struct SampleContext<'a> {
    subject: &'a Subject,
    state: &'a Path,
    run: &'a Path,
    runtime: &'a runtime::RuntimeIdentity,
    budget: u64,
}

fn run_sample(
    context: &SampleContext<'_>,
    index: usize,
    spec: &Spec,
    sample: u32,
) -> Result<Sample> {
    let SampleContext {
        subject,
        state,
        run,
        runtime,
        budget,
    } = context;
    let root = run.join("samples").join(format!("{index:04}"));
    private_dir(&root)?;
    let home = root.join("home");
    let work = root.join("work");
    let view = root.join("state");
    private_dir(&home)?;
    private_dir(&work)?;
    let execute = || -> Result<Sample> {
        let generation = copy_generation(state, &view, &subject.generation, *budget)?;
        let inputs = run.join("inputs");
        let phase = root.join("phase");
        let setup = spec
            .setup
            .as_ref()
            .map(|name| inputs.join(name))
            .unwrap_or_default();
        let mut command = runtime::command(&runtime.bash_invocation, &home, &work, &runtime.path);
        command
            .env("BINIT_CHECK_INPUTS", &inputs)
            .env("BINIT_CHECK_WORK", &work)
            .env("BINIT_CHECK_STATE", &view)
            .env("__binit_in_check", "1")
            .env(
                "BINIT_COMPLETION_MENU",
                if runtime.native { "1" } else { "0" },
            )
            .args([
                "--noprofile",
                "--norc",
                "-i",
                "-c",
                include_str!("../runtime/check.bash"),
                "binit-check",
            ])
            .arg(setup)
            .arg(generation.join("init.bash"))
            .arg(inputs.join(&spec.script))
            .arg(&phase)
            .arg(&view)
            .arg(&generation)
            .arg(if runtime.native { "1" } else { "0" });
        let output = crate::process::run(&mut command, Duration::from_millis(spec.timeout_ms))?;
        let phase = String::from_utf8_lossy(&read_regular_limit(&phase, 32).unwrap_or_default())
            .trim()
            .to_owned();
        let (phase, check_return) = match phase.strip_prefix("complete:") {
            Some(code) => (
                "complete".to_owned(),
                code.parse::<u8>().ok().map(i32::from),
            ),
            None => (phase, None),
        };
        let preview =
            |bytes: &[u8]| String::from_utf8_lossy(&bytes[..bytes.len().min(256)]).into_owned();
        Ok(Sample {
            check: spec.id.clone(),
            sample,
            phase,
            check_return,
            elapsed_ms: output.wall_ms,
            code: output.status.code(),
            signal: output.status.signal(),
            timed_out: output.timed_out,
            interrupted: output.interrupted,
            output_complete: output.stdout.complete && output.stderr.complete,
            generation_unchanged: verify_generation(&view, &subject.generation).is_ok(),
            stdout_preview: preview(&output.stdout.bytes),
            stderr_preview: preview(&output.stderr.bytes),
            stdout_truncated: output.stdout.truncated || output.stdout.bytes.len() > 256,
            stderr_truncated: output.stderr.truncated || output.stderr.bytes.len() > 256,
        })
    };
    let result = execute();
    for directory in [&home, &work, &view] {
        if directory.exists() {
            fs::remove_dir_all(directory)?;
        }
    }
    result
}

impl Receipt {
    pub(super) fn failure_reason(&self) -> String {
        if let Some(error) = &self.error {
            return error.clone();
        }
        if let Some(context) = &self.context {
            for result in &self.samples {
                let reason = if result.timed_out {
                    Some("timed out")
                } else if result.interrupted != 0 {
                    Some("was interrupted")
                } else if result.phase != "complete" || result.check_return.is_none() {
                    Some("stopped before its check script finished")
                } else if result.check_return != Some(0)
                    || result.code != Some(0)
                    || result.signal.is_some()
                {
                    Some("returned a failure")
                } else if !result.output_complete {
                    Some("left incomplete output capture")
                } else if !result.generation_unchanged {
                    Some("changed its generation copy")
                } else if context.binding.plan.checks.iter().any(|spec| {
                    spec.id == result.check
                        && spec
                            .max_elapsed_ms
                            .is_some_and(|limit| result.elapsed_ms > limit as f64)
                }) {
                    Some("exceeded its elapsed-time limit")
                } else {
                    None
                };
                if let Some(reason) = reason {
                    return format!(
                        "check {} sample {} {reason}; inspect its receipt",
                        result.check,
                        result.sample + 1
                    );
                }
            }
        }
        "candidate has no complete passing check receipt".to_owned()
    }

    pub(super) fn passed(&self) -> bool {
        let Some(context) = &self.context else {
            return false;
        };
        if self.schema != 1 || !self.complete || self.error.is_some() {
            return false;
        }
        let expected = context
            .binding
            .plan
            .checks
            .iter()
            .flat_map(|spec| (0..spec.samples).map(move |sample| (spec, sample)))
            .collect::<Vec<_>>();
        expected.len() == self.samples.len()
            && expected
                .iter()
                .zip(&self.samples)
                .all(|((spec, sample), result)| {
                    result.check == spec.id
                        && result.sample == *sample
                        && result.phase == "complete"
                        && result.check_return == Some(0)
                        && result.code == Some(0)
                        && result.signal.is_none()
                        && !result.timed_out
                        && result.interrupted == 0
                        && result.output_complete
                        && result.generation_unchanged
                        && result.elapsed_ms.is_finite()
                        && result.elapsed_ms > 0.0
                        && spec
                            .max_elapsed_ms
                            .is_none_or(|limit| result.elapsed_ms <= limit as f64)
                })
    }

    pub(super) fn inputs_current(&self, subject: &Subject) -> Result<()> {
        if &self.subject != subject {
            return Err("candidate check subject changed".into());
        }
        let context = self
            .context
            .as_ref()
            .ok_or("candidate check has no complete context")?;
        if bind(&context.binding.plan_file.path)?.binding != context.binding
            || runtime::file_identity(&subject.config.path)? != subject.config
            || manager_identity()? != subject.manager
            || runtime::bash_invocation()? != context.runtime.bash_invocation
            || runtime::file_identity(&context.runtime.bash_invocation)? != context.runtime.bash
            || std::env::var("PATH").unwrap_or_else(|_| "/usr/bin:/bin".to_owned())
                != context.runtime.path
        {
            return Err("candidate check inputs or executable changed; run checks again".into());
        }
        for file in &context.runtime.files {
            if runtime::file_identity(&file.path)? != *file {
                return Err("candidate runtime files changed; run checks again".into());
            }
        }
        Ok(())
    }

    pub(super) fn validate_selection(
        &self,
        subject: &Subject,
        state: &Path,
        run: &Path,
    ) -> Result<()> {
        if !self.passed() {
            return Err("candidate has no complete passing check receipt".into());
        }
        self.inputs_current(subject)?;
        let context = self.context.as_ref().ok_or("missing check context")?;
        if runtime::probe(subject, state, context.runtime.native, run)? != context.runtime {
            return Err("candidate runtime identity changed; run checks again".into());
        }
        Ok(())
    }
}

pub(super) fn run(subject: &Subject, state: &Path, plan: &Path, root: &Path) -> Result<Receipt> {
    let mut receipt = Receipt {
        schema: 1,
        subject: subject.clone(),
        complete: false,
        context: None,
        error: None,
        samples: Vec::new(),
    };
    atomic_write(
        &root.join("progress.json"),
        &serde_json::to_vec_pretty(&receipt)?,
    )?;
    let execute = (|| -> Result<()> {
        if runtime::file_identity(&subject.config.path)? != subject.config {
            return Err("candidate configuration changed; prepare it again".into());
        }
        let inputs = bind(plan)?;
        let lock = verify_generation(state, &subject.generation)?;
        let native = inputs
            .binding
            .plan
            .native
            .unwrap_or_else(|| lock.files.contains_key("completion.so"));
        if native && !lock.files.contains_key("completion.so") {
            return Err("native checks requested without a native module".into());
        }
        input_snapshot(&root.join("inputs"), &inputs)?;
        private_dir(&root.join("samples"))?;
        let runtime = runtime::probe(subject, state, native, root)?;
        receipt.context = Some(Context {
            binding: inputs.binding.clone(),
            runtime: runtime.clone(),
        });
        atomic_write(
            &root.join("progress.json"),
            &serde_json::to_vec_pretty(&receipt)?,
        )?;
        let samples = SampleContext {
            subject,
            state,
            run: root,
            runtime: &runtime,
            budget: inputs.binding.plan.max_generation_bytes,
        };
        for spec in &inputs.binding.plan.checks {
            for sample in 0..spec.samples {
                let result = run_sample(&samples, receipt.samples.len(), spec, sample)?;
                let interrupted = result.interrupted;
                atomic_write(
                    &root
                        .join("samples")
                        .join(format!("{:04}", receipt.samples.len()))
                        .join("result.json"),
                    &serde_json::to_vec_pretty(&result)?,
                )?;
                receipt.samples.push(result);
                if interrupted != 0 {
                    return Err(
                        format!("candidate checks interrupted by signal {interrupted}").into(),
                    );
                }
                snapshot_unchanged(&root.join("inputs"), &inputs.binding)?;
            }
        }
        receipt.inputs_current(subject)?;
        if runtime::probe(subject, state, native, root)? != runtime {
            return Err("runtime changed during checks".into());
        }
        receipt.complete = true;
        Ok(())
    })();
    if let Err(error) = execute {
        receipt.error = Some(error.to_string());
    }
    atomic_write(
        &root.join("receipt.json"),
        &serde_json::to_vec_pretty(&receipt)?,
    )?;
    Ok(receipt)
}
