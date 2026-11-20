#!/usr/bin/env python3
"""Draw the README performance charts from a publication scoreboard receipt.

Usage: render_readme_chart.py SCOREBOARD.json FONT_DIR OUT_DIR
FONT_DIR holds IBMPlexSans-Regular.ttf and IBMPlexSans-SemiBold.ttf.
Writes {startup,typing,completion,menus,overhead}-{light,dark}.svg with text drawn as outlines, so
the images render the same wherever a README is shown, and prints each chart's alt text as JSON.

Each chart plots milliseconds on one linear scale from zero: one row per subject, one column per
question, the value written at the end of every bar. Charts group questions of similar size so a
shared scale keeps every bar readable. Binit's bars carry the accent colour and every
other subject is neutral. Titles state the finding in words and are checked against the data, so a
new receipt that changes a finding fails instead of drawing a false title.
"""
import json
import re
import statistics
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

# GitHub's light and dark page colours; bars keep at least 3:1 contrast with the page.
THEMES = {
    'light': {'paper': '#FFFFFF', 'ink': '#1F2328', 'muted': '#59636E', 'rule': '#D1D9E0',
              'neutral': '#8C959F', 'binit': '#D9480F'},
    'dark': {'paper': '#0D1117', 'ink': '#E6EDF3', 'muted': '#9198A1', 'rule': '#3D444D',
             'neutral': '#6E7681', 'binit': '#F0612C'},
}
# The README shows each chart at up to 800 px, about 730 px in a narrow window. No text is drawn
# below MIN_TEXT units, which keeps the smallest text near 14 px on screen.
WIDTH, MARGIN, MIN_TEXT = 800, 24, 15
TITLE, BODY = 22, 16
BANNED = ('shorter is', 'longer is', 'lower is', 'higher is', 'less is', 'more is better')
ROW, BAR = 40, 24
MACHINE = 'Binit 0.1.0 on one Linux machine: Ryzen 7 7800X3D, GNU Bash 5.3.20.'
STUB = 4


NBSP = '\u00a0'
# Names and number-unit pairs never break across lines.
UNBROKEN = ('Oh My Bash', 'plain .bashrc', 'plain Readline', 'plain Zsh', '1,024-item menu')


def joined(text):
    for phrase in UNBROKEN:
        text = text.replace(phrase, phrase.replace(' ', NBSP))
    return re.sub(r'(\d) (ms\b)', lambda m: m.group(1) + NBSP + m.group(2), text)


def number(value):
    return f'{value:.1f}'.rstrip('0').rstrip('.')


def ms(value):
    return f'{value:.2f}' if value < 1 else f'{value:.1f}'


class Face:
    def __init__(self, path, key):
        font = TTFont(path)
        self.key = key
        self.glyphs = font.getGlyphSet()
        self.cmap = font.getBestCmap()
        self.metrics = font['hmtx']
        self.em = font['head'].unitsPerEm
        self.used = {}

    def glyph(self, character):
        name = self.cmap.get(ord(' ' if character == NBSP else character))
        if name is None:
            raise ValueError(f'{character!r} is missing from {self.key}')
        return name

    def width(self, text, size):
        return sum(self.metrics[self.glyph(c)][0] for c in text) * size / self.em

    def uses(self, text, x, y, size, anchor='start'):
        """Place each glyph, defined once in font units, with one scale per text run."""
        x -= {'start': 0, 'middle': self.width(text, size) / 2, 'end': self.width(text, size)}[anchor]
        cursor, placed = 0, []
        for character in text:
            name = self.glyph(character)
            if character not in (' ', NBSP):
                if name not in self.used:
                    pen = SVGPathPen(self.glyphs, ntos=lambda v: str(round(v)))
                    self.glyphs[name].draw(TransformPen(pen, (1, 0, 0, -1, 0, 0)))
                    self.used[name] = (f'{self.key}{len(self.used)}', pen.getCommands())
                placed.append(f'<use href="#{self.used[name][0]}" x="{cursor}"/>')
            cursor += self.metrics[name][0]
        return f'<g transform="translate({number(x)} {number(y)}) scale({size / self.em:.5f})">' + ''.join(placed) + '</g>'

    def wrap(self, text, size, room):
        lines, line = [], ''
        for word in text.split(' '):
            candidate = f'{line} {word}'.strip()
            if line and self.width(candidate, size) > room:
                lines.append(line)
                line = word
            else:
                line = candidate
        return lines + [line]

    def balanced(self, text, size, room):
        """Wrap into as few lines as greedy wrapping needs, with line widths as even as possible."""
        count, words = len(self.wrap(text, size, room)), text.split(' ')
        if count == 1:
            return [text]

        def best(words, lines):
            if lines == 1:
                joined = ' '.join(words)
                return (self.width(joined, size), [joined]) if self.width(joined, size) <= room else (float('inf'), [])
            options = []
            for cut in range(1, len(words) - lines + 2):
                head = ' '.join(words[:cut])
                if self.width(head, size) > room:
                    break
                widest, rest = best(words[cut:], lines - 1)
                options.append((max(widest, self.width(head, size)), [head] + rest))
            return min(options, default=(float('inf'), []))

        return best(words, count)[1]

    def definitions(self):
        return ''.join(f'<path id="{key}" d="{commands}"/>' for key, commands in self.used.values())


def mean(values):
    return sum(values) / len(values)


class Chart:
    def __init__(self, faces, theme):
        self.faces = faces
        self.sans, self.bold = faces
        self.theme = theme
        self.parts = []

    def text(self, face, value, x, y, size, colour, anchor='start'):
        if any(phrase in value.lower() for phrase in BANNED):
            raise ValueError(f'{value!r} tells the reader how to read the chart; the units and title already do')
        if size < MIN_TEXT:
            raise ValueError(f'{value!r} at {size} units is below the {MIN_TEXT}-unit readable minimum')
        right = x + {'start': 1, 'middle': 0.5, 'end': 0}[anchor] * face.width(value, size)
        if right > WIDTH - MARGIN / 2:
            raise ValueError(f'{value!r} runs past the right edge')
        self.parts.append(f'<g fill="{self.theme[colour]}">{face.uses(value, x, y, size, anchor)}</g>')

    def paragraph(self, face, value, y, size, colour, leading, even=False):
        """Wrap `value` across the chart width from baseline `y`; return the baseline after it."""
        wrap = face.balanced if even else face.wrap
        for line in wrap(joined(value), size, WIDTH - 2 * MARGIN):
            self.text(face, line, MARGIN, y, size, colour)
            y += leading
        return y

    def bar(self, x, centre, length, colour):
        """A solid bar from the baseline with a rounded data end."""
        r = min(4, length / 2)
        top = centre - BAR / 2
        self.parts.append(f'<path d="M{x:.1f} {top:.1f}h{length - r:.1f}a{r:.1f} {r:.1f} 0 0 1 {r:.1f} {r:.1f}'
                          f'v{BAR - 2 * r:.1f}a{r:.1f} {r:.1f} 0 0 1 {-r:.1f} {r:.1f}h{-(length - r):.1f}z" '
                          f'fill="{self.theme[colour]}"/>')

    def line(self, x1, y1, x2, y2, colour, width=1):
        self.parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                          f'stroke="{self.theme[colour]}" stroke-width="{width}"/>')

    def svg(self, height, title, alt):
        if WIDTH < 1.5 * height:
            raise ValueError(f'{title}: {WIDTH}x{height} is not landscape; README charts stay wider than 1.5:1')
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}" '
                f'role="img" aria-labelledby="t d"><title id="t">{escape(title)}</title><desc id="d">{escape(alt)}</desc>'
                f'<defs>{"".join(face.definitions() for face in self.faces)}</defs>'
                f'<rect width="{WIDTH}" height="{height}" fill="{self.theme["paper"]}"/>' + ''.join(self.parts) + '</svg>\n')


def draw(faces, theme, spec, gutter):
    """spec: title, subtitle, rows [(label, is_binit)], columns [(heading, [value per row])], note.

    Every column shares one linear scale from zero, so bar lengths compare across the whole chart.
    `gutter` is the x where bars start; it is the same in every chart so the stacked images align.
    """
    chart = Chart(faces, theme)
    sans, bold = chart.sans, chart.bold
    for words in [spec['title'], spec['subtitle'], spec['note'], *(heading for heading, _ in spec['columns'])]:
        if any(phrase in words.lower() for phrase in BANNED):
            raise ValueError(f'{words!r} tells the reader how to read the chart; the units and title already do')
    y = chart.paragraph(bold, spec['title'], MARGIN + TITLE, TITLE, 'ink', 29, even=True)
    y = chart.paragraph(sans, spec['subtitle'], y - 2, BODY, 'muted', 23)

    rows, columns = spec['rows'], spec['columns']
    gap = 36
    width = (WIDTH - MARGIN - gutter - gap * (len(columns) - 1)) / len(columns)
    values = [[ms(v) + ' ms' for v in column] for _, column in columns]
    plot = width - max(bold.width(text, BODY) for texts in values for text in texts) - 10
    if plot < 90:
        raise ValueError(f'{spec["title"]}: columns too narrow for readable bars')
    left = [gutter + i * (width + gap) for i in range(len(columns))]

    headings = [bold.balanced(joined(heading), BODY, width) for heading, _ in columns]
    y += 14
    for x, lines in zip(left, headings):
        for offset, line in enumerate(lines):
            chart.text(bold, line, x, y + offset * 21, BODY, 'ink')
    y += (max(len(lines) for lines in headings) - 1) * 21 + 14

    peak = max(max(data) for _, data in columns)
    rows_top = y
    for index, (label, binit) in enumerate(rows):
        centre = y + ROW / 2
        chart.text(bold if binit else sans, label, gutter - 18, centre + 5.5, BODY, 'ink', 'end')
        for x, texts, (_, data) in zip(left, values, columns):
            length = max(data[index] / peak * plot, STUB)
            chart.bar(x, centre, length, 'binit' if binit else 'neutral')
            chart.text(bold if binit else sans, texts[index], x + length + 8, centre + 5.5, BODY, 'ink')
        y += ROW
    for x in left:
        chart.line(x, rows_top + 4, x, y - 4, 'rule', 1)

    y = chart.paragraph(sans, spec['note'], y + 14 + MIN_TEXT, MIN_TEXT, 'muted', 21) - MIN_TEXT
    alt = ' '.join(f'{heading}: ' + ', '.join(f'{label} {ms(value)} ms' for (label, _), value in zip(rows, data)) + '.'
                   for heading, data in columns)
    alt = f'{spec["title"]}. {spec["subtitle"]} {alt} {spec["note"]}'
    return chart.svg(round(y + 22), spec['title'], alt), alt


def check(condition, claim):
    if not condition:
        raise ValueError(f'the data no longer supports: {claim}')


def startup_spec(aspects):
    subjects = [('Binit', 'binit', True), ('Binit, load=wait', 'binit-wait', True), ('Bash-it', 'bash-it', False),
                ('Oh My Bash', 'oh-my-bash', False), ('Plain .bashrc', 'bash', False)]
    columns = []
    for heading, aspect, field in (('Ready to type', 'first-usable-input', 'startup_usable_ms'),
                                   ('All 50 plugins loaded', 'all-effects-ready', 'required_effects_ms')):
        medians = aspects[aspect]['medians_ms_by_run']
        columns.append((heading, [mean(medians[key][field]) for _, key, _ in subjects]))
    ready = dict(zip((key for _, key, _ in subjects), columns[0][1]))
    title = 'Binit is ready sooner than Bash-it and Oh My Bash, and later than a plain .bashrc'
    check(ready['binit'] < ready['bash-it'] < ready['oh-my-bash'] and ready['bash'] < ready['binit'], title)
    return {'title': title,
            'subtitle': 'A new shell loading the same 50 plugins. Binit is scored against Bash-it and Oh My Bash; '
                        'load=wait and a plain .bashrc with no manager are reported.',
            'rows': [(label, binit) for label, _, binit in subjects], 'columns': columns, 'note': MACHINE}


def editor_columns(aspects, metrics):
    def median_cell(aspect, subject, metric):
        values = aspects[aspect]['values_ms_by_run']
        cells = [mean(runs) for key, runs in values.items()
                 if key.split(' ', 1)[0] == subject and key.split(' ', 2)[2] == metric]
        if len(cells) != 6:
            raise ValueError(f'{subject} {metric}: expected six cells, found {len(cells)}')
        return statistics.median(cells)

    return [(heading, [median_cell(aspect, subject, metric) for subject in ('binit', 'blesh', 'readline')])
            for heading, aspect, metric in metrics]


EDITORS = [('Binit', True), ('ble.sh', False), ('Plain Readline', False)]


def typing_spec(aspects):
    columns = editor_columns(aspects, [('Typing a character', 'keystroke-p99', 'insert p99'),
                                       ('Deleting a character', 'keystroke-p99', 'delete p99')])
    binit, blesh, readline = columns[0][1]
    title = f'Binit echoes a key in {ms(binit)} ms, like plain Readline; ble.sh takes {number(blesh)} ms'
    check(all(abs(b - r) <= 0.15 * r and b < s for _, (b, s, r) in columns), title)
    return {'title': title,
            'subtitle': 'The slowest 1 in 100 keystrokes (p99), from key press to echo. Scored against ble.sh; '
                        'plain Readline, which has no menu or suggestions, is reported.',
            'rows': EDITORS, 'columns': columns, 'note': MACHINE}


def completion_spec(aspects):
    columns = editor_columns(aspects, [
        ('Typing while it runs in its own process, slowest 1 in 20 keys', 'slow-completion', 'isolated_response p95'),
        ('Waiting for one that runs inside the shell', 'slow-completion', 'stateful_completion median')])
    typing, waiting = columns[0][1], columns[1][1]
    title = (f'A slow completion leaves Binit answering keys in {ms(typing[0])} ms; '
             'one that must run inside the shell makes every editor wait')
    check(typing[0] < 0.05 * min(typing[1:]) and abs(waiting[0] - waiting[2]) <= 0.01 * waiting[2], title)
    return {'title': title,
            'subtitle': 'Typing is scored against ble.sh. Waiting is reported: a completion that changes shell state '
                        'blocks every editor until it ends.',
            'rows': EDITORS, 'columns': columns, 'note': MACHINE}


def menu_spec(aspects):
    columns = editor_columns(aspects, [('Accepting, own process', 'slow-completion', 'isolated_accept median'),
                                       ('Accepting, inside the shell', 'slow-completion', 'stateful_accept median'),
                                       ('Cancelling a 1,024-item menu', 'slow-completion', 'large_cancel median')])
    own, inside, cancel = (data for _, data in columns)
    title = 'Binit accepts and cancels sooner than ble.sh; plain Readline is ahead on a big menu and in-shell accepts'
    check(own[0] < own[1] and inside[0] < inside[1] and cancel[0] < cancel[1]
          and cancel[2] < cancel[0] and inside[2] < inside[0], title)
    return {'title': title,
            'subtitle': 'Medians. Own-process accept and cancel are scored against ble.sh; the in-shell accept and '
                        'plain Readline are reported.',
            'rows': EDITORS, 'columns': columns, 'note': MACHINE}


def overhead_spec(aspects):
    columns = []
    for heading, aspect in (('Ready to type', 'first-usable-input'), ('All 50 plugins loaded', 'all-effects-ready')):
        overhead = aspects[aspect]['framework_overhead_ms_by_run']
        columns.append((heading, [mean(overhead['binit']), mean(overhead['zinit'])]))
    binit, zinit = columns[0][1]
    title = f'Binit adds {number(binit)} ms over a plain .bashrc; Zinit adds {number(zinit)} ms over plain Zsh'
    return {'title': title,
            'subtitle': 'Time a manager adds over the same shell sourcing the same 50 plugins directly. Reported: '
                        'Zinit runs in Zsh, so this compares managers, not shells.',
            'rows': [('Binit, in Bash', True), ('Zinit, in Zsh', False)], 'columns': columns, 'note': MACHINE}


SPECS = {'startup': startup_spec, 'typing': typing_spec, 'completion': completion_spec,
         'menus': menu_spec, 'overhead': overhead_spec}


def main():
    board = json.loads(Path(sys.argv[1]).read_text())
    fonts, out = Path(sys.argv[2]), Path(sys.argv[3])
    out.mkdir(parents=True, exist_ok=True)

    def faces():
        return [Face(fonts / 'IBMPlexSans-Regular.ttf', 's'), Face(fonts / 'IBMPlexSans-SemiBold.ttf', 'S')]

    specs = {name: build(board['aspects']) for name, build in SPECS.items()}
    # One label column for every chart, sized to the longest label, so the stacked images line up.
    measure = faces()
    gutter = MARGIN + max((measure[1] if binit else measure[0]).width(label, BODY)
                          for spec in specs.values() for label, binit in spec['rows']) + 18
    alts = {}
    for mode, theme in THEMES.items():
        for name, spec in specs.items():
            svg, alts[name] = draw(faces(), theme, spec, gutter)
            (out / f'{name}-{mode}.svg').write_text(svg)
    print(json.dumps(alts, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
