#define _XOPEN_SOURCE 700
#include "layout.h"

#include <string.h>
#include <wchar.h>

/* Width of the first character of text and its length, or -1 when it is unmeasurable. */
static int measure(const char *text, size_t size, size_t *length)
{
    unsigned char byte = (unsigned char)*text;
    if (byte >= 0x20 && byte <= 0x7e) {
        *length = 1;
        return 1;
    }
    mbstate_t state = {0};
    wchar_t character;
    size_t used = mbrtowc(&character, text, size, &state); // NOLINT(concurrency-mt-unsafe): Readline runs on Bash's shell thread.
    if (used == (size_t)-1 || used == (size_t)-2 || used == 0) return -1;
    if ((character >= 0x80 && character <= 0x9f) || (character >= 0x202a && character <= 0x202e) ||
        (character >= 0x2066 && character <= 0x2069)) return -1;
    int cells = wcwidth(character);
    if (cells < 0) return -1;
    *length = used;
    return cells;
}

int binit_cells(const char *text, size_t size)
{
    int width = 0;
    for (size_t offset = 0, used; offset < size; offset += used) {
        int cells = measure(text + offset, size - offset, &used);
        if (cells < 0) return -1;
        width += cells;
    }
    return width;
}

/* Readline cuts the prompt at the last newline anywhere in it and treats SOH..STX as zero width.
   A stray STX after that cut (its SOH was before the newline) is a control character: -1. */
int binit_prompt_cells(const char *prompt)
{
    size_t length = strnlen(prompt, 8193);
    if (length > 8192) return -1;
    const char *newline = strrchr(prompt, '\n');
    const char *p = newline ? newline + 1 : prompt;
    const char *end = prompt + length;
    int width = 0;
    while (p < end) {
        if (*p == '\001') {
            p = memchr(p, '\002', (size_t)(end - p));
            if (!p) return -1;
            ++p;
            continue;
        }
        if (*p == '\033' && p + 1 < end && p[1] == '[') {
            p += 2;
            while (p < end && ((*p >= '0' && *p <= '9') || *p == ';' || *p == ':')) ++p;
            if (p == end || *p != 'm') return -1;
            ++p;
            continue;
        }
        size_t used;
        int cells = measure(p, (size_t)(end - p), &used);
        if (cells < 0) return -1;
        width += cells;
        p += used;
    }
    return width;
}

bool binit_layout(int prompt_cells, int columns, const char *line, size_t size, size_t point, struct binit_layout *out)
{
    if (columns < 2 || prompt_cells < 0 || prompt_cells >= columns || point > size || size > 4096) return false;
    int row = 0, column = prompt_cells;
    bool cursor = false;
    out->offset[0] = 0;
    out->column[0] = prompt_cells;
    for (size_t offset = 0, used = 0; offset < size; offset += used) {
        if (line[offset] == '\n' || line[offset] == '\t') {
            /* Readline starts a row at a newline. A tab becomes spaces to the next multiple of eight and
               continues on the next row when it reaches the margin. */
            used = 1;
            if (offset == point) {out->cursor = (struct binit_position){row, column}; cursor = true;}
            int next = line[offset] == '\n' ? columns : column + 8 - column % 8;
            if (next < columns) {
                column = next;
                continue;
            }
            if (++row >= BINIT_LAYOUT_ROWS || next - columns >= columns) return false;
            out->offset[row] = offset + 1;
            out->column[row] = column = next - columns;
            continue;
        }
        int cells = measure(line + offset, size - offset, &used);
        if (cells < 0) return false;
        if (cells > columns - column) { /* Readline pads the row and starts the character on the next */
            if (++row >= BINIT_LAYOUT_ROWS) return false;
            out->offset[row] = offset;
            out->column[row] = column = 0;
        }
        if (offset == point) {out->cursor = (struct binit_position){row, column}; cursor = true;}
        else if (point > offset && point < offset + used) return false;
        column += cells;
        if (column >= columns) { /* the right margin is the start of the next row, even at the end */
            if (++row >= BINIT_LAYOUT_ROWS) return false;
            out->offset[row] = offset + used;
            out->column[row] = column = 0;
        }
    }
    out->end = (struct binit_position){row, column};
    if (!cursor) out->cursor = out->end;
    out->rows = row + 1;
    return true;
}

size_t binit_fit(const char *text, size_t size, int cells, int *used)
{
    size_t offset = 0, length;
    int width = 0;
    while (offset < size) {
        int next = measure(text + offset, size - offset, &length);
        if (next < 0 || next > cells - width) break;
        width += next;
        offset += length;
    }
    *used = width;
    return offset;
}
