#!/usr/bin/env python3
"""Draw the README comparison chart from a publication scoreboard receipt.

Usage: render_readme_chart.py SCOREBOARD.json FONT_DIR OUT_DIR
FONT_DIR holds IBMPlexMono-Light.ttf, IBMPlexMono-Regular.ttf and IBMPlexMono-Bold.ttf.
Writes comparison-light.svg and comparison-dark.svg with text drawn as outlines, so the
images render the same wherever a README is shown.
"""
import json
import math
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

THEMES = {
    'light': {'paper': '#F7F5F0', 'ink': '#191919', 'ink2': '#4F4B45', 'rule': '#CFC9BD',
              'grey': '#8A857B', 'accent': '#D9480F'},
    'dark': {'paper': '#1D1B19', 'ink': '#EDEAE3', 'ink2': '#B3AEA4', 'rule': '#37342F',
             'grey': '#8A857C', 'accent': '#F0612C'},
}
WIDTH, MARGIN, PLOT_LEFT, PLOT_RIGHT = 600, 28, 54, 524
LOW, HIGH = 0.1, 600.0
GROUPS = [2, 4]


def number(value):
    return f'{value:.1f}'.rstrip('0').rstrip('.')


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
        return self.cmap.get(ord(character), '.notdef')

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
        scale = size / self.em
        return f'<g transform="translate({number(x)} {number(y)}) scale({scale:.5f})">' + ''.join(placed) + '</g>'

    def definitions(self):
        return ''.join(f'<path id="{key}" d="{commands}"/>' for key, commands in self.used.values())


def closest(aspect, suffix):
    """Binit and competitor times, both runs averaged, in the tested cell where the competitor comes closest."""
    cells = [comparison for key, comparison in aspect['comparisons'].items() if key.split(' vs ')[0].endswith(suffix)]
    cell = max(cells, key=lambda comparison: sum(run['ratio'] for run in comparison['runs']))
    return [sum(run[side] for run in cell['runs']) / len(cell['runs']) for side in ('binit', 'competitor')]


def rows(board):
    aspects = board['aspects']

    def median(aspect, subject, field):
        values = aspects[aspect]['medians_ms_by_run'][subject][field]
        return sum(values) / len(values)

    startup = [('Ready to type, 50 plugins', 'first-usable-input', 'startup_usable_ms'),
               ('All 50 plugins loaded', 'all-effects-ready', 'required_effects_ms')]
    result = [(label, median(aspect, 'binit', field),
               [('Bash-it', median(aspect, 'bash-it', field)), ('Oh My Bash', median(aspect, 'oh-my-bash', field))])
              for label, aspect, field in startup]
    editor = [('Keystroke, p99', 'keystroke-p99', 'insert p99'),
              ('Delete key, p99', 'keystroke-p99', 'delete p99'),
              ('Typing during a slow completion, p95', 'slow-completion', 'isolated_response p95'),
              ('Cancel a 1,024-item menu, median', 'slow-completion', 'large_cancel median')]
    for label, aspect, suffix in editor:
        binit, rival = closest(aspects[aspect], suffix)
        result.append((label, binit, [('ble.sh', rival)]))
    return result


def x_of(value):
    span = math.log10(HIGH) - math.log10(LOW)
    return PLOT_LEFT + (math.log10(value) - math.log10(LOW)) / span * (PLOT_RIGHT - PLOT_LEFT)


def factor(value):
    return f'{value:.0f}x' if value >= 10 else f'{value:.1f}x'


def render(data, faces, theme, provenance):
    light, regular, bold = faces
    parts = []

    def text(face, value, x, y, size, colour, anchor='start', halo=True):
        stroke = (f' stroke="{theme["paper"]}" stroke-width="{3.5 * face.em / size:.0f}" stroke-linejoin="round" '
                  f'paint-order="stroke"') if halo else ''
        parts.append(f'<g fill="{theme[colour]}"{stroke}>{face.uses(value, x, y, size, anchor)}</g>')

    row, group_gap = 92, 26
    y = 46
    text(bold, 'Binit against Bash-it,', MARGIN, y, 25, 'ink', halo=False)
    text(bold, 'Oh My Bash and ble.sh', MARGIN, y + 31, 25, 'ink', halo=False)
    text(regular, 'ms, log scale, one machine, GNU Bash 5.3', MARGIN, y + 60, 15, 'ink2', halo=False)
    y += 104
    index = 0
    for count in GROUPS:
        closest = {min(rivals, key=lambda rival: rival[1])[0] for _, _, rivals in data[index:index + count]}
        title = f'x faster than {closest.pop()}' if len(closest) == 1 else 'x faster than the closest competitor'
        text(bold, title, MARGIN, y + 2, 14, 'ink2', halo=False)
        y += 26
        for position in range(count):
            label, binit, rivals = data[index]
            index += 1
            rivals = sorted(rivals, key=lambda rival: rival[1])
            text(regular, label, MARGIN, y + 18, 17, 'ink', halo=False)
            text(bold, factor(rivals[0][1] / binit), WIDTH - MARGIN, y + 18, 20, 'accent', 'end', halo=False)
            line_y = y + 50
            spans = []
            for order, (name, value_ms) in enumerate(rivals):
                caption = f'{name} {number(value_ms)}'
                width = regular.width(caption, 15)
                if order == 0 and len(rivals) > 1:
                    continue
                start = x_of(value_ms) + 11
                if start + width <= WIDTH - MARGIN:
                    spans.append((start - 4, start + width + 4))
            for tick in (0.1, 1, 10, 100):
                if any(low <= x_of(tick) <= high for low, high in spans):
                    continue
                parts.append(f'<line x1="{x_of(tick):.1f}" y1="{line_y - 9}" x2="{x_of(tick):.1f}" y2="{line_y + 9}" '
                             f'stroke="{theme["rule"]}" stroke-width="1.5"/>')
            near = x_of(rivals[0][1])
            parts.append(f'<line x1="{x_of(binit):.1f}" y1="{line_y}" x2="{near:.1f}" y2="{line_y}" stroke="{theme["grey"]}" stroke-width="2.5"/>')
            if len(rivals) > 1:
                parts.append(f'<line x1="{near + 6:.1f}" y1="{line_y}" x2="{x_of(rivals[-1][1]):.1f}" y2="{line_y}" '
                             f'stroke="{theme["grey"]}" stroke-width="2" stroke-dasharray="2 4"/>')
            parts.append(f'<circle cx="{x_of(binit):.1f}" cy="{line_y}" r="6" fill="{theme["accent"]}"/>')
            below = []
            for order, (name, value_ms) in enumerate(rivals):
                x = x_of(value_ms)
                parts.append(f'<circle cx="{x:.1f}" cy="{line_y}" r="5" fill="{theme["paper"]}" stroke="{theme["grey"]}" stroke-width="2.5"/>')
                caption = f'{name} {number(value_ms)}'
                right = x + 11 + regular.width(caption, 15)
                if order == 0 and len(rivals) > 1:
                    text(regular, caption, x, line_y - 14, 15, 'ink2', 'middle')
                elif right > WIDTH - MARGIN:
                    end = min(x + 6, WIDTH - MARGIN)
                    below.append((end - regular.width(caption, 15), end))
                    text(regular, caption, end, line_y + 28, 15, 'ink2', 'end')
                else:
                    text(regular, caption, x + 11, line_y + 5, 15, 'ink2')
            shown = number(binit) if binit >= 1 else f'{binit:.2f}'
            width = bold.width(f'Binit {shown}', 15)
            centre = max(x_of(binit), MARGIN + width / 2)
            if any(low < centre + width / 2 + 6 and centre - width / 2 - 6 < high for low, high in below):
                raise ValueError(f'{label}: a competitor label overlaps the Binit label; widen the axis')
            text(bold, f'Binit {shown}', centre, line_y + 28, 15, 'ink', 'middle')
            y += row
            if position < count - 1:
                parts.append(f'<line x1="{MARGIN}" y1="{y - 8}" x2="{WIDTH - MARGIN}" y2="{y - 8}" stroke="{theme["rule"]}" stroke-width="1"/>')
        y += group_gap
    grid_bottom = y - group_gap + 14
    parts.append(f'<line x1="{MARGIN}" y1="{grid_bottom - 14}" x2="{WIDTH - MARGIN}" y2="{grid_bottom - 14}" stroke="{theme["rule"]}" stroke-width="1"/>')
    for tick in (0.1, 1, 10, 100):
        text(regular, f'{tick:g} ms', x_of(tick), grid_bottom + 6, 15, 'ink2', 'middle', halo=False)
    height = grid_bottom + 56 + 20 * (len(provenance) - 1)
    for offset, line in enumerate(provenance):
        text(regular, line, MARGIN, grid_bottom + 42 + offset * 20, 14, 'ink2', halo=False)
    summary = '; '.join(f'{label}: Binit {binit:.2f} ms, ' + ', '.join(f'{name} {value:.2f} ms' for name, value in rivals)
                        for label, binit, rivals in data)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}" '
            f'role="img" aria-labelledby="t d"><title id="t">Binit measured against Bash-it, Oh My Bash and ble.sh</title>'
            f'<desc id="d">{escape(summary)}</desc><defs>{"".join(face.definitions() for face in faces)}</defs>'
            f'<rect width="{WIDTH}" height="{height}" fill="{theme["paper"]}"/>' + ''.join(parts) + '</svg>\n')


def main():
    board = json.loads(Path(sys.argv[1]).read_text())
    fonts, out = Path(sys.argv[2]), Path(sys.argv[3])
    faces = [Face(fonts / f'IBMPlexMono-{weight}.ttf', key) for weight, key in (('Light', 'l'), ('Regular', 'r'), ('Bold', 'b'))]
    provenance = sys.argv[4:] or ['Medians of 2 runs x 20 interleaved pairs. Editor rows',
                                  'show the mode and history size where ble.sh is closest.']
    data = rows(board)
    out.mkdir(parents=True, exist_ok=True)
    for mode, theme in THEMES.items():
        (out / f'comparison-{mode}.svg').write_text(render(data, faces, theme, provenance))
    print(json.dumps([[label, round(binit, 3), [[n, round(v, 3)] for n, v in rivals]] for label, binit, rivals in data]))


if __name__ == '__main__':
    main()
