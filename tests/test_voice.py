"""Keeps Binit's output in the voice of a classic Unix tool.

Scans the string literals that reach the terminal from the Rust CLI, the native editor and the
shell runtime. Output is lowercase, states facts, and names keys as typed; docs/editor.md and
the interface spec describe the voice.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RUST_OUTPUT = re.compile(r'\b(?:e?println!|e?print!)\s*\(\s*"((?:[^"\\]|\\.)*)"', re.S)
C_LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
BASH_OUTPUT = re.compile(r'''(?:^|[;{&|]|\bthen|\belse|\bdo)\s*(?:builtin\s+)?(?:printf|echo)\s+(?:-[a-z]+\s+)*(?:'([^']*)'|"((?:[^"\\]|\\.)*)")''', re.M)

# Rule 7: the assistant register.
ASSISTANT = re.compile(
    r"\b(?:please|simply|just|let's|note that|make sure|feel free|successfully|now|you|your)\b"
    r"|!$|!\\n|—|[\U0001F300-\U0001FAFF☀-➿]", re.I)
# Rule 2: keys as typed, never a Title-Case key guide.
TITLE_KEY = re.compile(r'\b(?:Enter|Esc|Tab|Type|Backspace|Right|Left|Ctrl-[A-Za-z])\b')
# Rules 2 and 6: no sentence case and no `Key: value` rows; errors start with `binit:`.
SENTENCE = re.compile(r'^(?:\\[nr]|\s)*[A-Z][a-z]')
# Rule 2: no CAPS words outside table headers, usage placeholders and variable names.
CAPS = re.compile(r'\b[A-Z]{4,}\b')
# Rule 6: status rows are aligned `name  value`, never `name: value`.
STATUS_ROW = re.compile(r'^(?!binit:|usage:)(?:[a-z][a-z -]*|\{[^{}]*\}): \S')
# printf and Rust format conversions, which are not words.
CONVERSION = re.compile(r'%[-+ #0-9.*]*[a-zA-Z]|\{[^{}]*\}')


# Rust text whose braces do not count: comments, strings (raw ones too) and char literals.
RUST_OPAQUE = re.compile(
    r'//[^\n]*|/\*.*?\*/|r(#*)".*?"\1|b?"(?:[^"\\]|\\.)*"|b?\'(?:[^\'\\]|\\.[^\']*)\'', re.S)


def item_end(text, start):
    """The offset just past the item that begins at `start`: its `;` or its closing brace."""
    depth, position = 0, start
    while position < len(text):
        opaque = RUST_OPAQUE.match(text, position)
        if opaque:
            position = opaque.end()
            continue
        character = text[position]
        position += 1
        if character == ';' and depth == 0:
            return position
        if character in '{}':
            depth += 1 if character == '{' else -1
            if depth == 0:
                return position
    raise ValueError('unterminated test item')


def without_test_items(text):
    """Blanks each item under `#[cfg(test)]`, keeping line numbers; tests print nothing to users."""
    while (start := text.find('#[cfg(test)]')) >= 0:
        end = item_end(text, start + len('#[cfg(test)]'))
        text = text[:start] + re.sub(r'[^\n]', ' ', text[start:end]) + text[end:]
    return text


def literals():
    for path in sorted((ROOT / 'src').glob('*.rs')):
        text = without_test_items(path.read_text())
        for match in RUST_OUTPUT.finditer(text):
            kind = 'help' if match.group(1).startswith('usage:') else 'output'
            yield path, text.count('\n', 0, match.start()) + 1, match.group(1), kind
    for path in sorted((ROOT / 'native').glob('*.c')):
        if path.name.startswith('test_'):
            continue
        documentation = False
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith(('#include', '//', '/*', '*')):
                continue
            # Bash's `help` text is sentence case in every builtin, so Binit's follows it.
            if re.search(r'_?documentation\[\]\s*=\s*\{', line):
                documentation = True
            kind = 'help' if documentation else 'error' if 'builtin_error(' in line else 'output'
            for match in C_LITERAL.finditer(line):
                yield path, number, match.group(1), kind
            if documentation and '};' in line:
                documentation = False
    for path in sorted((ROOT / 'runtime').glob('*.bash')):
        text = path.read_text()
        for match in BASH_OUTPUT.finditer(text):
            yield path, text.count('\n', 0, match.start()) + 1, match.group(1) or match.group(2), 'output'


def table_header(text):
    """A row of CAPS column names, such as `%-20s %-12s PLUGIN STATE`."""
    words = re.findall(r'[A-Za-z_]+', CONVERSION.sub(' ', text))
    return bool(words) and all(word.isupper() for word in words)


def variable_name(text):
    """A single CAPS name such as `TERM` or `BINIT_ESCAPE_MS`, read from the environment."""
    return re.fullmatch(r'[A-Z][A-Z0-9_]*', text) is not None


# Environment names that messages cite, and markers that programs read instead of people.
NAMES = {'PATH', 'HOME', 'TERM', 'SHELL', 'BINIT-IDENTITY'}


def problems(text, kind='output'):
    """Rule breaks in one literal; `kind` is `output`, `error` (Bash adds the subject) or `help`."""
    if not re.search(r'[A-Za-z]{2}', text):
        return []
    found = []
    if ASSISTANT.search(text):
        found.append('assistant register')
    if TITLE_KEY.search(text):
        found.append('Title-Case key name')
    # Each printed line on its own: a literal often holds several.
    lines = re.split(r'\\n', text)
    if kind != 'help' and any(SENTENCE.search(line) for line in lines):
        found.append('sentence case')
    if kind == 'output' and any(STATUS_ROW.search(line) for line in lines):
        found.append('name: value row')
    # Usage lines and their continuations name placeholders in CAPS, as man pages do.
    usage = re.match(r'(?:\\n|\s)*(?:usage:|\[)', text) is not None
    words = CAPS.findall(re.sub(r'\b(?:' + '|'.join(map(re.escape, NAMES)) + r')\b', ' ', text))
    if words and not usage and not table_header(text) and not variable_name(text):
        found.append('CAPS word')
    return found


class VoiceTests(unittest.TestCase):
    def test_output_reads_like_a_unix_tool(self):
        failures = []
        for path, line, text, kind in literals():
            for problem in problems(text, kind):
                failures.append(f'{path.relative_to(ROOT)}:{line}: {problem}: {text!r}')
        self.assertEqual(failures, [], '\n' + '\n'.join(failures))

    def test_the_scanner_finds_each_kind_of_output(self):
        found = {path.parent.name for path, *_ in literals()}
        self.assertEqual(found, {'src', 'native', 'runtime'})

    def test_the_rules_catch_the_old_voice(self):
        for old in ('Completions 1/3', 'Tab next  arrows/hjkl move', 'History SEARCH /al  1/37',
                    'Native editor: available', 'Applied g12; start a new shell to use it',
                    'Synced 3 plugin lines', 'Done!', 'Note that you can press tab', 'please', 'now',
                    'menu: on', 'PLUGIN loaded', '{action}: {id}\\nGeneration: {}\\nApply: binit shell'):
            self.assertTrue(problems(old), old)
        for new in ('st /co  1/3   ?', 'history  1/37   ?', 'tab next  hjkl move  / filter',
                    'menu          on', 'applied %s; new shells use it', 'binit: %s: %s\\n',
                    '%-20s %-12s PLUGIN STATE', 'usage: binit load ID\\n', 'BINIT_ESCAPE_MS', 'TERM',
                    '{:>10} {:>10}  {:<16} SELF_US'):
            self.assertEqual(problems(new), [], new)
        self.assertEqual(problems('highlighting: built without BINIT_SYNTAX_SOURCE', 'error'), [])
        self.assertEqual(problems('Control the completion menu in the current shell.', 'help'), [])
        self.assertTrue(problems('Enter inserts, Esc closes.', 'help'))
        self.assertEqual(problems('binit: commands stay on PATH until binit sync'), [])


if __name__ == '__main__':
    unittest.main()
