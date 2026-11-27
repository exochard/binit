#ifndef BINIT_HIGHLIGHT_H
#define BINIT_HIGHLIGHT_H
#include <stddef.h>
#include <stdint.h>

enum binit_highlight_color {
    HL_PLAIN, HL_COMMENT, HL_STRING, HL_COMMAND, HL_VARIABLE, HL_KEYWORD,
    HL_ALIAS, HL_FUNCTION, HL_BUILTIN, HL_EXTERNAL, HL_MISSING, HL_PATH,
    HL_MISSING_PATH, HL_OPTION, HL_EXPANSION, HL_SUBSTITUTION, HL_REDIRECTION,
    HL_BRACKET, HL_GIT, HL_SYNTAX_ERROR, HL_COLOR_COUNT
};

#define BINIT_HIGHLIGHT_LIMIT 4096u
#define BINIT_HIGHLIGHT_TOKENS 24u

enum binit_highlight_role { HL_COMMAND_NAME, HL_PATH_NAME };

struct binit_highlight_token {
    uint16_t start, end;
    enum binit_highlight_role role;
};

struct binit_highlight_tokens {
    size_t count;
    struct binit_highlight_token items[BINIT_HIGHLIGHT_TOKENS];
};

int binit_highlight(const char *line, size_t length, uint8_t *colors);
int binit_highlight_extended(const char *line, size_t length, size_t cursor,
                            uint8_t *colors, struct binit_highlight_tokens *tokens);
void binit_highlight_clear(void);
void binit_highlight_errors(const char *line, size_t length, uint8_t *colors);
#endif
