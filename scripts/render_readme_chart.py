#!/usr/bin/env python3
"""Draw the README performance charts from a publication scoreboard receipt.

Usage: render_readme_chart.py SCOREBOARD.json FONT_DIR OUT_DIR
FONT_DIR holds IBMPlexSans-{Regular,SemiBold}.ttf and IBMPlexMono-{Regular,SemiBold}.ttf.
Writes {startup,typing,completion,in-shell,overhead}-{light,dark}.svg with text drawn as outlines, so
the images render the same wherever a README is shown, and prints each chart's alt text as JSON.

Every chart is landscape. Speed charts plot the competitor's time divided by Binit's on a
log scale centred on "same": bars to the right are comparisons Binit wins, bars to the left
are comparisons it loses. Solid bars are scored comparisons; outlined bars are controls the
scoreboard reports without a verdict. Rows covering several editor cells show the median
cell as the bar and the slowest and fastest cells as a whisker.
"""
import json
import math
import statistics
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

# Validated with the dataviz palette checks against GitHub's light and dark page colours.
THEMES = {
    'light': {'paper': '#FFFFFF', 'ink': '#1F2328', 'muted': '#59636E', 'rule': '#D1D9E0', 'neutral': '#8C959F',
              'faster': '#D9480F', 'slower': '#3E6FB0', 'faster_tint': '#F9DACD', 'slower_tint': '#D8E2EF'},
    'dark': {'paper': '#0D1117', 'ink': '#E6EDF3', 'muted': '#9198A1', 'rule': '#3D444D', 'neutral': '#7D8590',
             'faster': '#F0612C', 'slower': '#5A8BD0', 'faster_tint': '#3D1E14', 'slower_tint': '#18263A'},
}
WIDTH, MARGIN = 1000, 24
ROW, HEADING, GAP = 38, 24, 16
CELLS = 'Bars show the median of six cells: Emacs and vi, each with three history sizes.'
MACHINE = 'One Linux machine (Ryzen 7 7800X3D), GNU Bash 5.3.20. Medians of two runs of 20 interleaved pairs. Each chart has its own scale.'


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
        name = self.cmap.get(ord(character))
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
            if character != ' ':
                if name not in self.used:
                    pen = SVGPathPen(self.glyphs, ntos=lambda v: str(round(v)))
                    self.glyphs[name].draw(TransformPen(pen, (1, 0, 0, -1, 0, 0)))
                    self.used[name] = (f'{self.key}{len(self.used)}', pen.getCommands())
                placed.append(f'<use href="#{self.used[name][0]}" x="{cursor}"/>')
            cursor += self.metrics[name][0]
        return f'<g transform="translate({number(x)} {number(y)}) scale({size / self.em:.5f})">' + ''.join(placed) + '</g>'

    def definitions(self):
        return ''.join(f'<path id="{key}" d="{commands}"/>' for key, commands in self.used.values())


def mean(values):
    return sum(values) / len(values)


def speed_row(label, binit, rival, rival_name, scored):
    """binit and rival are per-cell times in ms, both runs averaged, in the same cell order."""
    ratios = [r / b for b, r in zip(binit, rival)]
    return {'label': label, 'rival': rival_name, 'scored': scored, 'ratio': statistics.median(ratios),
            'low': min(ratios), 'high': max(ratios), 'binit': (min(binit), max(binit)), 'other': (min(rival), max(rival))}


def startup_rows(aspects):
    def value(aspect, subject, field):
        return mean(aspects[aspect]['medians_ms_by_run'][subject][field])

    groups = []
    for heading, aspect, field in (('Ready to type in a new shell, 50 plugins', 'first-usable-input', 'startup_usable_ms'),
                                   ('All 50 plugins loaded', 'all-effects-ready', 'required_effects_ms')):
        binit, wait = value(aspect, 'binit', field), value(aspect, 'binit-wait', field)
        rows = [speed_row('vs Bash-it', [binit], [value(aspect, 'bash-it', field)], 'Bash-it', True),
                speed_row('vs Oh My Bash', [binit], [value(aspect, 'oh-my-bash', field)], 'Oh My Bash', True),
                speed_row('vs a .bashrc sourcing them directly', [binit], [value(aspect, 'bash', field)], '.bashrc', False),
                speed_row('load=wait plugins, vs Bash-it', [wait], [value(aspect, 'bash-it', field)], 'Bash-it', False),
                speed_row('load=wait plugins, vs Oh My Bash', [wait], [value(aspect, 'oh-my-bash', field)], 'Oh My Bash', False)]
        groups.append((heading, rows))
    return groups


def editor_rows(aspect, metrics):
    values = aspect['values_ms_by_run']

    def cells(subject, metric):
        found = {key.split(' ', 2)[1]: mean(runs) for key, runs in values.items()
                 if key.split(' ', 1)[0] == subject and key.split(' ', 2)[2] == metric}
        return [found[cell] for cell in sorted(found)]

    groups = []
    for heading, metric, scored in metrics:
        binit = cells('binit', metric)
        if len(binit) != 6:
            raise ValueError(f'{metric}: expected six cells, found {len(binit)}')
        groups.append((heading, [speed_row('vs ble.sh', binit, cells('blesh', metric), 'ble.sh', scored),
                                 speed_row('vs plain Readline', binit, cells('readline', metric), 'Readline', False)]))
    return groups


def span(pair):
    low, high = pair
    return f'{ms(low)} ms' if ms(low) == ms(high) else f'{ms(low)}–{ms(high)} ms'


def near_one(row):
    return row['low'] < 1 < row['high'] or 0.95 <= row['low'] <= row['high'] <= 1.05


def verdict(row):
    def phrase(ratio):
        return f'{ratio:.0f}' if ratio >= 10 else f'{ratio:.1f}'
    low, high = row['low'], row['high']
    if near_one(row):
        both = f'{low:.2f}' if f'{low:.2f}' == f'{high:.2f}' else f'{low:.2f}–{high:.2f}'
        return f'about the same, {both}×'
    if low >= 1:
        words = phrase(low) if phrase(low) == phrase(high) else f'{phrase(low)}–{phrase(high)}'
        return f'{words}× faster'
    slow, fast = phrase(1 / high), phrase(1 / low)
    return f'{slow if slow == fast else f"{slow}–{fast}"}× slower'


def times(row):
    return f'Binit {span(row["binit"])} · {row["rival"]} {span(row["other"])}'


class Chart:
    def __init__(self, faces, theme):
        self.sans, self.sans_bold, self.mono, self.mono_bold = faces
        self.theme = theme
        self.parts = []

    def text(self, face, value, x, y, size, colour, anchor='start'):
        if self.text_end(face, value, x, size, anchor) > WIDTH - MARGIN / 2:
            raise ValueError(f'{value!r} runs past the right edge')
        self.parts.append(f'<g fill="{self.theme[colour]}">{face.uses(value, x, y, size, anchor)}</g>')

    def line(self, x1, y1, x2, y2, colour, width=1):
        self.parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                          f'stroke="{self.theme[colour]}" stroke-width="{width}"/>')

    def rect(self, x, y, width, height, colour, outline=False):
        # Every fill is opaque; an outlined bar uses its hue's tint, so grid lines never show through.
        fill = self.theme[f'{colour}_tint'] if outline else self.theme[colour]
        stroke = f' stroke="{self.theme[colour]}" stroke-width="1.5"' if outline else ''
        self.parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height}" rx="3" fill="{fill}"{stroke}/>')

    def diamond(self, x, y):
        self.parts.append(f'<path d="M{x:.1f} {y - 7:.1f}L{x + 7:.1f} {y:.1f}L{x:.1f} {y + 7:.1f}L{x - 7:.1f} {y:.1f}Z" '
                          f'fill="{self.theme["paper"]}" stroke="{self.theme["neutral"]}" stroke-width="2"/>')

    def whisker(self, x1, x2, y, colour='ink'):
        self.line(x1, y, x2, y, colour, 1.5)
        for x in (x1, x2):
            self.line(x, y - 6, x, y + 6, colour, 1.5)

    def header(self, title, subtitle):
        self.text(self.sans_bold, title, MARGIN, 34, 20, 'ink')
        self.text(self.sans, subtitle, MARGIN, 58, 14.5, 'muted')

    def legend(self, y, items):
        x = MARGIN
        for draw, caption in items:
            draw(x, y - 5)
            self.text(self.sans, caption, x + 32, y, 14, 'ink')
            x += 32 + self.sans.width(caption, 14) + 28

    def text_end(self, face, value, x, size, anchor='start'):
        return x + {'start': 1, 'middle': 0.5, 'end': 0}[anchor] * face.width(value, size)

    def svg(self, faces, height, title, alt):
        if WIDTH < 1.5 * height:
            raise ValueError(f'{title}: {WIDTH}x{height} is not landscape; README charts stay wider than 1.5:1')
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}" '
                f'role="img" aria-labelledby="t d"><title id="t">{escape(title)}</title><desc id="d">{escape(alt)}</desc>'
                f'<defs>{"".join(face.definitions() for face in faces)}</defs>'
                f'<rect width="{WIDTH}" height="{height}" fill="{self.theme["paper"]}"/>' + ''.join(self.parts) + '</svg>\n')


def legend_items(chart, rows):
    kinds = []
    for colour, scored, caption in (('faster', True, 'Binit faster, scored'), ('faster', False, 'Binit faster, reported'),
                                    ('slower', True, 'Binit slower, scored'), ('slower', False, 'Binit slower, reported')):
        if any(not near_one(row) and (row['ratio'] >= 1) == (colour == 'faster') and row['scored'] == scored for row in rows):
            kinds.append((lambda x, y, c=colour, s=scored: chart.rect(x, y - 6, 22, 12, c, outline=not s), caption))
    if any(near_one(row) for row in rows):
        kinds.append((lambda x, y: chart.diamond(x + 11, y), 'about the same'))
    if any(row['low'] != row['high'] for row in rows):
        kinds.append((lambda x, y: (chart.rect(x, y - 6, 14, 12, 'neutral'), chart.whisker(x + 10, x + 24, y)),
                      'whisker: slowest to fastest cell'))
    return kinds


def fit(chart, face, text, size, room):
    """The largest size up to `size` at which `text` fits in `room` pixels."""
    while face.width(text, size) > room and size > 15:
        size -= 0.5
    return size


def speed_chart(faces, theme, title, subtitle, groups, domain, ticks, notes):
    chart = Chart(faces, theme)
    chart.text(chart.sans_bold, title, MARGIN, 34, fit(chart, chart.sans_bold, title, 20, WIDTH - 2 * MARGIN), 'ink')
    chart.text(chart.sans, subtitle, MARGIN, 58, 14.5, 'muted')
    rows_all = [row for _, rows in groups for row in rows]
    # The label gutter and the number column take only the room their longest text needs; the plot gets the rest.
    plot_left = MARGIN + max(chart.sans.width(row['label'], 15) for row in rows_all) + 28
    numbers = WIDTH - MARGIN - max(max(chart.mono_bold.width(verdict(row), 14), chart.mono.width(times(row), 13.5))
                                   for row in rows_all)
    plot_right = numbers - 28
    chart.legend(88, legend_items(chart, rows_all))
    low, high = (math.log10(value) for value in domain)

    def x_of(ratio):
        return plot_left + (math.log10(ratio) - low) / (high - low) * (plot_right - plot_left)

    y = 104
    bands, grid_at = [], len(chart.parts)
    for heading, rows in groups:
        y += GAP
        chart.text(chart.sans_bold, heading, MARGIN, y + 16, 15.5, 'ink')
        y += HEADING
        bands.append((y, y + ROW * len(rows)))
        for row in rows:
            centre = y + ROW / 2
            chart.text(chart.sans, row['label'], plot_left - 16, centre + 5, 15, 'ink', 'end')
            if near_one(row):
                chart.whisker(x_of(row['low']), x_of(row['high']), centre, 'neutral')
                chart.diamond(x_of(row['ratio']), centre)
            else:
                colour = 'faster' if row['ratio'] >= 1 else 'slower'
                start, end = sorted((x_of(1), x_of(row['ratio'])))
                chart.rect(start, centre - 7, max(end - start, 3), 14, colour, outline=not row['scored'])
                if row['low'] != row['high']:
                    chart.whisker(x_of(row['low']), x_of(row['high']), centre)
            chart.text(chart.mono_bold, verdict(row), numbers, centre - 3, 14, 'ink')
            chart.text(chart.mono, times(row), numbers, centre + 14, 13.5, 'muted')
            y += ROW
    # Grid lines sit behind the bars and run through each group's rows only, never across a heading.
    grid = Chart(faces, theme)
    for tick, _ in ticks:
        for band_top, band_bottom in bands:
            grid.line(x_of(tick), band_top, x_of(tick), band_bottom, 'muted' if tick == 1 else 'rule', 1.5 if tick == 1 else 1)
    chart.parts[grid_at:grid_at] = grid.parts
    bottom = y + 6
    for tick, caption in ticks:
        chart.text(chart.sans, caption, x_of(tick), bottom + 18, 13, 'muted', 'middle')
    chart.text(chart.sans, '← Binit slower', plot_left - 38, bottom + 18, 13, 'ink', 'end')
    chart.text(chart.sans, 'Binit faster →', plot_right + 42, bottom + 18, 13, 'ink')
    for offset, note in enumerate(notes):
        chart.text(chart.sans, note, MARGIN, bottom + 48 + offset * 20, 13.5, 'muted')
    alt = ' '.join(f'{heading}: ' + '; '.join(f'{row["label"]}, {verdict(row)} ({times(row)}, '
                                                f'{"scored" if row["scored"] else "reported"})' for row in rows) + '.'
                   for heading, rows in groups)
    return chart.svg(faces, bottom + 52 + len(notes) * 20, title, alt), f'{title}. {subtitle} {alt}'


def overhead_chart(faces, theme, aspects):
    chart = Chart(faces, theme)
    rows = []
    for heading, aspect in (('Ready to type', 'first-usable-input'), ('All 50 plugins loaded', 'all-effects-ready')):
        overhead = aspects[aspect]['framework_overhead_ms_by_run']
        rows.append((heading, mean(overhead['binit']), mean(overhead['zinit'])))
    title = f'Binit adds {number(rows[0][1])} ms to a new shell with 50 plugins; Zinit adds {number(rows[0][2])} ms to Zsh'
    subtitle = 'Time over the same shell sourcing the same plugins directly. Reported, not scored.'
    chart.text(chart.sans_bold, title, MARGIN, 34, 20, 'ink')
    chart.text(chart.sans, subtitle, MARGIN, 58, 14.5, 'muted')
    chart.legend(88, [(lambda x, y: chart.rect(x, y - 6, 22, 12, 'faster'), 'Binit, over Bash'),
                      (lambda x, y: chart.rect(x, y - 6, 22, 12, 'neutral'), 'Zinit, over Zsh')])
    plot_left = MARGIN + chart.sans.width('Binit', 15) + 28
    numbers = WIDTH - MARGIN - chart.mono_bold.width('+314.0 ms', 14)
    plot_right = numbers - 28
    scale = 350
    x_of = lambda value: plot_left + value / scale * (plot_right - plot_left)
    y, grid_at, bands = 104, len(chart.parts), []
    for heading, binit, zinit in rows:
        y += GAP
        chart.text(chart.sans_bold, heading, MARGIN, y + 16, 15.5, 'ink')
        y += HEADING
        bands.append((y, y + 2 * ROW))
        for name, value, colour in (('Binit', binit, 'faster'), ('Zinit', zinit, 'neutral')):
            centre = y + ROW / 2
            chart.text(chart.sans, name, plot_left - 16, centre + 5, 15, 'ink', 'end')
            chart.rect(plot_left, centre - 7, max(x_of(value) - plot_left, 3), 14, colour)
            chart.text(chart.mono_bold, f'+{number(value)} ms', numbers, centre + 5, 14, 'ink')
            y += ROW
    grid = Chart(faces, theme)
    ticks = (0, 100, 200, 300)
    for tick in ticks:
        for band_top, band_bottom in bands:
            grid.line(x_of(tick), band_top, x_of(tick), band_bottom, 'muted' if tick == 0 else 'rule', 1.5 if tick == 0 else 1)
    chart.parts[grid_at:grid_at] = grid.parts
    bottom = y + 6
    for tick in ticks:
        chart.text(chart.sans, f'{tick} ms', x_of(tick), bottom + 18, 13, 'muted', 'middle')
    chart.text(chart.sans, MACHINE, MARGIN, bottom + 48, 13.5, 'muted')
    chart.text(chart.sans, 'Zinit runs in Zsh, so its control is Zsh; the two overheads compare managers, not shells.',
               MARGIN, bottom + 68, 13.5, 'muted')
    alt = ' '.join(f'{heading}: Binit adds {number(binit)} ms over Bash sourcing the plugins directly; '
                   f'Zinit adds {number(zinit)} ms over Zsh doing the same.' for heading, binit, zinit in rows)
    return chart.svg(faces, bottom + 82, title, alt), f'{title}. {subtitle} {alt}'


def fold(rows):
    return min(row['low'] for row in rows), max(row['high'] for row in rows)


def factor(value):
    return f'{value:.0f}' if value >= 10 else f'{value:.1f}'


def charts(board, new_faces, theme):
    aspects = board['aspects']
    speed_ticks = [(0.5, '2×'), (1, 'same'), (2, '2×'), (4, '4×')]
    editor_ticks = [(0.1, '10×'), (1, 'same'), (10, '10×'), (100, '100×'), (1000, '1000×')]
    defined = 'Scored rows count toward the verdict; reported rows are controls without one.'
    startup = startup_rows(aspects)
    typing = editor_rows(aspects['keystroke-p99'], [('Typing a character, slowest 1 in 100 keystrokes (p99)', 'insert p99', True),
                                                     ('Deleting a character, slowest 1 in 100 keystrokes (p99)', 'delete p99', True)])
    completion = editor_rows(aspects['slow-completion'], [
        ('Typing while a slow completion runs in its own process, slowest 1 in 20 keys (p95)', 'isolated_response p95', True),
        ('Accepting that completion, median', 'isolated_accept median', True),
        ('Cancelling a 1,024-item menu, median', 'large_cancel median', True)])
    in_shell = editor_rows(aspects['slow-completion'], [
        ('A slow completion that has to run inside the shell, median', 'stateful_completion median', False),
        ('Accepting that in-shell completion, median', 'stateful_accept median', False)])
    ready = startup[0][1]
    won = fold([row for row in ready if row['scored']])
    direct = next(row for row in ready if row['rival'] == '.bashrc')
    keys = fold([row for _, rows in typing for row in rows if row['rival'] == 'ble.sh'])
    typing_ble = fold(completion[0][1][:1])
    cancel = completion[2][1][1]
    accept = in_shell[1][1][1]
    return {
        'startup': speed_chart(new_faces(), theme,
                               f'Binit is ready to type {factor(won[0])}–{factor(won[1])}× sooner than Bash-it and Oh My Bash',
                               f'Same 50 plugins. A .bashrc that sources them directly is ready {factor(1 / direct["ratio"])}× sooner '
                               'than Binit. Log scale.', startup, (0.4, 5), speed_ticks,
                               [MACHINE, defined + ' load=wait loads plugins right after the first prompt.']),
        'typing': speed_chart(new_faces(), theme,
                              f'Binit echoes keys {factor(keys[0])}–{factor(keys[1])}× sooner than ble.sh and as fast as plain Readline',
                              'Time from a key to its echo on the command line, in Emacs and vi modes. Log scale.',
                              typing, (0.1, 1000), editor_ticks, [MACHINE, defined, CELLS]),
        'completion': speed_chart(new_faces(), theme,
                                  f'Binit answers keys {factor(typing_ble[0])}–{factor(typing_ble[1])}× sooner than ble.sh; '
                                  f'plain Readline cancels a big menu {factor(1 / cancel["high"])}–{factor(1 / cancel["low"])}× sooner',
                                  'Completions that run in their own process, so typing can cancel them. Log scale.',
                                  completion, (0.1, 1000), editor_ticks, [MACHINE, defined, CELLS]),
        'in-shell': speed_chart(new_faces(), theme,
                                f'Completion inside the shell: Binit ties plain Readline, then accepts it '
                                f'{factor(1 / accept["high"])}–{factor(1 / accept["low"])}× slower',
                                'Such a provider changes shell state, so every editor waits for it. Reported, not scored. Log scale.',
                                in_shell, (0.1, 1000), editor_ticks, [MACHINE, defined, CELLS]),
        'overhead': overhead_chart(new_faces(), theme, aspects),
    }


def main():
    board = json.loads(Path(sys.argv[1]).read_text())
    fonts, out = Path(sys.argv[2]), Path(sys.argv[3])
    files = (('IBMPlexSans-Regular', 's'), ('IBMPlexSans-SemiBold', 'S'), ('IBMPlexMono-Regular', 'm'), ('IBMPlexMono-SemiBold', 'M'))
    out.mkdir(parents=True, exist_ok=True)
    alts = {}
    for mode, theme in THEMES.items():
        def new_faces():
            return [Face(fonts / f'{name}.ttf', key) for name, key in files]
        for name, (svg, alt) in charts(board, new_faces, theme).items():
            (out / f'{name}-{mode}.svg').write_text(svg)
            alts[name] = alt
    print(json.dumps(alts, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
