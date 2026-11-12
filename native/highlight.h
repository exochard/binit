#ifndef BINIT_HIGHLIGHT_H
#define BINIT_HIGHLIGHT_H
#include <stddef.h>
#include <stdint.h>

int binit_highlight(const char *line, size_t length, uint8_t *colors);
void binit_highlight_clear(void);
#endif
