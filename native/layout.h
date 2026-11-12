#ifndef BINIT_LAYOUT_H
#define BINIT_LAYOUT_H

#include <stdbool.h>
#include <stddef.h>

#define BINIT_LAYOUT_ROWS 64

struct binit_position {
    int row, column;
};

struct binit_layout {
    struct binit_position cursor, end; /* row 0 is the prompt's last line */
    int rows;                          /* end.row + 1 */
    size_t offset[BINIT_LAYOUT_ROWS];  /* byte offset of the first input byte on each row */
    int column[BINIT_LAYOUT_ROWS];     /* column where that row's input starts */
};

/* Terminal cells of text, or -1 when it holds anything Readline cannot be trusted to measure. */
int binit_cells(const char *text, size_t size);
/* Cells of the prompt's last line, or -1 when it holds unmeasurable escapes. */
int binit_prompt_cells(const char *prompt);
/* Lays out line after prompt_cells cells the way Readline wraps it; false when it cannot be trusted. */
bool binit_layout(int prompt_cells, int columns, const char *line, size_t size, size_t point, struct binit_layout *out);
/* Bytes of the longest whole-character prefix within cells; *used receives its width. */
size_t binit_fit(const char *text, size_t size, int cells, int *used);

#endif
