use crate::{Result, valid_digest, valid_name};
use std::collections::{BTreeMap, BTreeSet};
use std::io::Write;

pub(super) struct Session<'a> {
    generation: &'a str,
    plugins: BTreeMap<&'a str, (&'a str, &'a str, &'a str)>,
}

impl<'a> Session<'a> {
    pub(super) fn parse(bytes: &'a [u8]) -> Result<Self> {
        let input = std::str::from_utf8(bytes)?;
        let mut fields = input
            .strip_suffix('\0')
            .ok_or("incomplete plugin session")?
            .split('\0');
        let generation = fields
            .next()
            .filter(|value| valid_digest(value))
            .ok_or("invalid shell generation")?;
        let mut plugins = BTreeMap::new();
        while let Some(id) = fields.next() {
            let (Some(state), Some(load), Some(reason)) =
                (fields.next(), fields.next(), fields.next())
            else {
                return Err("incomplete plugin session row".into());
            };
            if !valid_name(id) || reason.len() > 1024 || state.len() > 32 || load.len() > 32 {
                return Err("invalid plugin session row".into());
            }
            if plugins.insert(id, (state, load, reason)).is_some() {
                return Err("duplicate plugin session row".into());
            }
        }
        Ok(Self {
            generation,
            plugins,
        })
    }

    pub(super) fn write<'next>(
        &self,
        output: &mut impl Write,
        next_generation: Option<&str>,
        installed: impl IntoIterator<Item = (&'next str, bool, &'next str)>,
        width: usize,
        color: bool,
    ) -> Result<()> {
        let installed: BTreeMap<_, _> = installed
            .into_iter()
            .map(|(id, enabled, load)| (id, (enabled, load)))
            .collect();
        let names: BTreeSet<&str> = self
            .plugins
            .keys()
            .copied()
            .chain(installed.keys().copied())
            .collect();
        let width = width.max(24);
        let wide = width >= 74;
        writeln!(output, "{}", styled("Plugins", "1", color))?;
        match next_generation {
            None => wrap(
                output,
                width,
                "",
                "No generation is selected for the next shell.",
            )?,
            Some(id) if id != self.generation => wrap(
                output,
                width,
                "",
                "This shell and the next shell use different generations.",
            )?,
            Some(_) => {}
        }
        if names.is_empty() {
            writeln!(output, "No plugins")?;
            return Ok(());
        }
        if wide {
            let header = format!(
                "{:<23} {:<13} {:<12} LOAD",
                "PLUGIN", "THIS SHELL", "NEXT SHELL"
            );
            writeln!(output, "{}", styled(&header, "1", color))?;
        }
        for id in names {
            let current = self.plugins.get(id).copied();
            let next = installed.get(id).copied();
            let (state, live_load, reason) = current.unwrap_or(("", "", ""));
            let live_label = match state {
                "" => "Absent",
                "loaded" => "Loaded",
                "loading" => "Loading",
                "pending" => "Waiting",
                "failed" => "Failed",
                "unavailable" => "Unavailable",
                "blocked" => "Blocked",
                "disabled" => "Disabled",
                "undeclared" => "Undeclared",
                "unloaded" => "Unloaded",
                "checking" => "Checking",
                _ => "Unknown",
            };
            let next_label = match next {
                None if next_generation.is_none() => "No selection",
                Some((true, _)) => "Enabled",
                Some((false, _)) => "Disabled",
                None => "Removed",
            };
            let next_load = next.map_or("", |(_, load)| load);
            let trigger =
                if !live_load.is_empty() && !next_load.is_empty() && live_load != next_load {
                    format!("{} / {}", load_label(live_load), load_label(next_load))
                } else {
                    load_label(if live_load.is_empty() {
                        next_load
                    } else {
                        live_load
                    })
                    .into()
                };
            let failed = matches!(state, "failed" | "unavailable" | "blocked");
            if wide && id.len() <= 23 && 51 + trigger.len() <= width {
                let head = format!("{id:<23} {live_label:<13} {next_label:<12} {trigger}");
                let face = if failed {
                    "33"
                } else if state == "loaded" {
                    "36"
                } else {
                    ""
                };
                writeln!(output, "{}", styled(&head, face, color && !face.is_empty()))?;
            } else {
                wrap(output, width, "", id)?;
                wrap(
                    output,
                    width,
                    "  ",
                    &format!("{live_label} here · {next_label} next · {trigger}"),
                )?;
            }
            if !reason.is_empty() {
                let safe: String = reason
                    .chars()
                    .map(|c| if c.is_control() { ' ' } else { c })
                    .collect();
                wrap(output, width, "  ", &safe)?;
            }
            if failed || next.is_none() {
                wrap(output, width, "  ", &format!("binit report {id}"))?;
            } else if current.is_none() || matches!(next, Some((false, _))) {
                wrap(output, width, "  ", &format!("binit inspect {id}"))?;
            }
        }
        writeln!(output)?;
        wrap(
            output,
            width,
            "",
            "Details  binit report ID · binit inspect ID",
        )?;
        Ok(())
    }
}

fn styled(text: &str, code: &str, enabled: bool) -> String {
    if enabled {
        format!("\x1b[{code}m{text}\x1b[0m")
    } else {
        text.into()
    }
}

fn load_label(load: &str) -> &str {
    match load {
        "startup" => "Startup",
        "prompt" => "First prompt",
        "wait" => "After prompt",
        "completion" => "First Tab",
        "command" => "First use",
        "timed" => "Timer",
        "event" => "Event",
        "watch" => "File change",
        _ => "Unknown",
    }
}

fn wrap(output: &mut impl Write, width: usize, indent: &str, value: &str) -> std::io::Result<()> {
    let mut column = 0;
    for word in value.split_whitespace() {
        let size = word.chars().count();
        if column > 0 && column + size + 1 > width {
            writeln!(output)?;
            column = 0;
        }
        if column == 0 {
            write!(output, "{indent}")?;
            column = indent.len();
        } else {
            write!(output, " ")?;
            column += 1;
        }
        for character in word.chars() {
            if column == width {
                write!(output, "\n{indent}")?;
                column = indent.len();
            }
            write!(output, "{character}")?;
            column += 1;
        }
    }
    writeln!(output)
}
