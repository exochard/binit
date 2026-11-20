#include "highlight.h"
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <tree_sitter/api.h>

extern const TSLanguage *tree_sitter_bash(void);
static TSParser *parser;
static TSTree *previous;
static char *previous_line;
static uint32_t previous_length;

struct bracket {
    uint16_t start;
    uint8_t width;
    char kind;
};

struct decoration {
    struct binit_highlight_tokens *tokens;
    size_t cursor, depth, overflow;
    struct bracket brackets[128];
    struct bracket opening, closing;
};

static TSPoint point_at(const char *line, uint32_t length)
{
    TSPoint point = {0, 0};
    for (uint32_t i = 0; i < length; ++i) {
        if (line[i] == '\n') {++point.row; point.column = 0;}
        else ++point.column;
    }
    return point;
}

void binit_highlight_clear(void)
{
    if (previous) ts_tree_delete(previous);
    if (parser) ts_parser_delete(parser);
    free(previous_line);
    previous = NULL;
    parser = NULL;
    previous_line = NULL;
    previous_length = 0;
}

static uint8_t node_color(const char *type)
{
    if (!strcmp(type, "comment")) return 1;
    if (!strcmp(type, "string") || !strcmp(type, "raw_string") || !strcmp(type, "heredoc_body")) return 2;
    if (!strcmp(type, "command_name")) return 3;
    if (!strcmp(type, "variable_name")) return 4;
    const char *keywords[] = {"case", "do", "done", "elif", "else", "esac", "fi", "for", "function", "if", "in", "select", "then", "until", "while"};
    for (size_t i = 0; i < sizeof(keywords) / sizeof(*keywords); ++i)
        if (!strcmp(type, keywords[i])) return 5;
    return 0;
}

static bool same(const char *left, const char *right)
{
    return strcmp(left, right) == 0;
}

static void range(TSNode node, uint8_t color, uint8_t *colors, uint32_t length)
{
    uint32_t start = ts_node_start_byte(node), end = ts_node_end_byte(node);
    if (start < end && end <= length) memset(colors + start, color, end - start);
}

static void token(struct decoration *extra, TSNode node, enum binit_highlight_role role)
{
    uint32_t start = ts_node_start_byte(node), end = ts_node_end_byte(node);
    struct binit_highlight_tokens *tokens = extra->tokens;
    if (tokens->count == BINIT_HIGHLIGHT_TOKENS || start >= end || end > BINIT_HIGHLIGHT_LIMIT) return;
    tokens->items[tokens->count++] = (struct binit_highlight_token){(uint16_t)start, (uint16_t)end, role};
}

static void delimiters(struct decoration *extra, TSNode node)
{
    const char *type = ts_node_type(node);
    static const char *pairs[][2] = {{"(", ")"}, {"[", "]"}, {"{", "}"}, {"[[", "]]"}, {"((", "))"}};
    for (size_t i = 0; i < sizeof(pairs) / sizeof(*pairs); ++i) {
        bool opens = same(type, pairs[i][0]);
        if (i == 0)
            opens = opens || same(type, "$(") || same(type, "<(") || same(type, ">(");
        if (i == 2) opens = opens || same(type, "${");
        if (!opens && !same(type, pairs[i][1])) continue;
        uint32_t start = ts_node_start_byte(node), end = ts_node_end_byte(node);
        if (start >= end || end > BINIT_HIGHLIGHT_LIMIT || end - start > 2) return;
        struct bracket here = {(uint16_t)start, (uint8_t)(end - start), (char)i};
        if (opens) {
            if (extra->depth == sizeof(extra->brackets) / sizeof(*extra->brackets)) ++extra->overflow;
            else extra->brackets[extra->depth++] = here;
        } else if (extra->overflow) {
            --extra->overflow;
        } else if (extra->depth) {
            struct bracket previous_bracket = extra->brackets[--extra->depth];
            size_t cursor = extra->cursor;
            if (previous_bracket.kind == here.kind &&
                ((cursor >= start && cursor <= end) ||
                 (cursor >= previous_bracket.start && cursor <= (size_t)previous_bracket.start + previous_bracket.width))) {
                extra->opening = previous_bracket;
                extra->closing = here;
            }
        }
        return;
    }
}

static bool word_equals(TSNode node, const char *line, const char *word)
{
    uint32_t start = ts_node_start_byte(node), end = ts_node_end_byte(node);
    return end >= start && strlen(word) == end - start && memcmp(line + start, word, end - start) == 0;
}

static bool git_subcommand(TSNode node, const char *line)
{
    const char *commands[] = {
        "add", "am", "apply", "archive", "bisect", "blame", "branch", "checkout",
        "cherry-pick", "clean", "clone", "commit", "config", "describe", "diff",
        "fetch", "grep", "init", "log", "merge", "mv", "pull", "push", "rebase",
        "reflog", "remote", "reset", "restore", "revert", "rm", "show", "stash",
        "status", "submodule", "switch", "tag", "worktree"
    };
    for (size_t i = 0; i < sizeof(commands) / sizeof(*commands); ++i)
        if (word_equals(node, line, commands[i])) return true;
    return false;
}

static bool is_path(TSNode node, const char *line, bool destination)
{
    const char *type = ts_node_type(node);
    if (!same(type, "word") && !same(type, "raw_string") && !same(type, "string")) return false;
    if (same(type, "string") && ts_node_named_child_count(node) > 1) return false;
    uint32_t start = ts_node_start_byte(node), end = ts_node_end_byte(node);
    return start < end && (destination || memchr(line + start, '/', end - start));
}

static bool decorate_argument(TSNode child, const char *line, uint8_t *colors, uint32_t length,
                              struct decoration *extra, bool *options, bool *git, bool *git_value)
{
    uint32_t at = ts_node_start_byte(child), end = ts_node_end_byte(child);
    if (at >= end) return false;
    if (*git_value) {
        *git_value = false;
    } else if (*options && line[at] == '-' && end - at > 1 && same(ts_node_type(child), "word")) {
        range(child, HL_OPTION, colors, length);
        if (word_equals(child, line, "--")) *options = false;
        if (*git)
            *git_value = word_equals(child, line, "-C") || word_equals(child, line, "-c") ||
                         word_equals(child, line, "--git-dir") || word_equals(child, line, "--work-tree");
        return true;
    } else if (*git) {
        *git = false;
        if (git_subcommand(child, line)) {
            range(child, HL_GIT, colors, length);
            return true;
        }
    }
    if (is_path(child, line, false)) token(extra, child, HL_PATH_NAME);
    return false;
}

static int command_position(TSNode error, uint32_t index)
{
    if (!index) return 1;
    const char *before = ts_node_type(ts_node_child(error, index - 1));
    const char *starts[] = {"then", "do", "else", "elif", "if", "while", "until", ";", "|", "|&", "&&", "||", "&", "{", "(", "$("};
    for (size_t i = 0; i < sizeof(starts) / sizeof(*starts); ++i)
        if (!strcmp(before, starts[i])) return 1;
    return 0;
}

/* A quote without a later partner in the same ERROR is unterminated: the string runs to the ERROR end.
 * An unterminated single quote comes as a childless ERROR that starts with it. */
static void paint_unterminated_quote(TSNode error, const char *line, uint8_t *colors, uint32_t length)
{
    uint32_t count = ts_node_child_count(error), end = ts_node_end_byte(error);
    if (!count && end > ts_node_start_byte(error) && end <= length && line[ts_node_start_byte(error)] == '\'')
        memset(colors + ts_node_start_byte(error), 2, end - ts_node_start_byte(error));
    for (uint32_t i = 0; i < count; ++i) {
        const char *type = ts_node_type(ts_node_child(error, i));
        if (strcmp(type, "\"") != 0 && strcmp(type, "'") != 0) continue;
        uint32_t j = i + 1;
        while (j < count && strcmp(ts_node_type(ts_node_child(error, j)), type) != 0) ++j;
        if (j < count) {i = j; continue;}
        uint32_t start = ts_node_start_byte(ts_node_child(error, i));
        if (start <= end && end <= length) memset(colors + start, 2, end - start);
        return;
    }
}

static void paint(TSNode node, const char *line, uint8_t *colors, uint32_t length,
                  unsigned int depth, struct decoration *extra)
{
    if (depth > 128 || ts_node_is_missing(node)) return;
    int error = ts_node_is_error(node);
    const char *type = ts_node_type(node);
    uint32_t count = ts_node_child_count(node);
    bool command = extra && same(type, "command"), redirect = extra && same(type, "file_redirect");
    bool options = true, git = false, git_value = false;
    if (error) paint_unterminated_quote(node, line, colors, length);
    else {
        uint8_t color = node_color(type);
        uint32_t start = ts_node_start_byte(node), end = ts_node_end_byte(node);
        if (same(type, "heredoc_body") && end > start && end <= length && line[end - 1] == '\n') --end;
        if (extra) {
            if (same(type, "expansion") || same(type, "simple_expansion")) color = HL_EXPANSION;
            if (same(type, "file_descriptor")) color = HL_REDIRECTION;
            if (count == 0) delimiters(extra, node);
            if (color == HL_COMMAND) token(extra, node, HL_COMMAND_NAME);
        }
        if (color && start <= end && end <= length) memset(colors + start, color, end - start);
    }
    for (uint32_t i = 0; i < count; ++i) {
        TSNode child = ts_node_child(node, i);
        if (error && !strcmp(ts_node_type(child), "word") && command_position(node, i)) {
            range(child, HL_COMMAND, colors, length);
            if (extra) token(extra, child, HL_COMMAND_NAME);
        }
        if (extra) {
            const char *field = (command || redirect) ? ts_node_field_name_for_child(node, i) : NULL;
            if (command && field && same(field, "name")) git = word_equals(child, line, "git");
            if (command && field && same(field, "argument") &&
                decorate_argument(child, line, colors, length, extra, &options, &git, &git_value)) continue;
            if (redirect && field && same(field, "destination") && is_path(child, line, true))
                token(extra, child, HL_PATH_NAME);
            if ((redirect || same(type, "heredoc_redirect") || same(type, "herestring_redirect")) &&
                !ts_node_is_named(child)) range(child, HL_REDIRECTION, colors, length);
            if ((same(type, "command_substitution") || same(type, "process_substitution")) &&
                !ts_node_is_named(child)) range(child, HL_SUBSTITUTION, colors, length);
        }
        paint(child, line, colors, length, depth + 1, extra);
    }
}

static int parse(const char *line, size_t length)
{
    if (length > BINIT_HIGHLIGHT_LIMIT) return 0;
    if (!parser) {
        parser = ts_parser_new();
        if (!parser || !ts_parser_set_language(parser, tree_sitter_bash())) {binit_highlight_clear(); return 0;}
    }
    uint32_t size = (uint32_t)length, start = 0, suffix = 0;
    if (previous) {
        while (start < previous_length && start < size && previous_line[start] == line[start]) ++start;
        if (start == previous_length && start == size) return 1;
        while (suffix < previous_length - start && suffix < size - start && previous_line[previous_length-suffix-1] == line[size-suffix-1]) ++suffix;
        TSInputEdit edit = {start, previous_length-suffix, size-suffix, point_at(previous_line, start),
                            point_at(previous_line, previous_length-suffix), point_at(line, size-suffix)};
        ts_tree_edit(previous, &edit);
    }
    TSTree *tree = ts_parser_parse_string(parser, previous, line, size);
    if (!tree) {binit_highlight_clear(); return 0;}
    char *copy = malloc(length + 1);
    if (!copy) {ts_tree_delete(tree); binit_highlight_clear(); return 0;}
    memcpy(copy, line, length);
    copy[length] = '\0';
    if (previous) ts_tree_delete(previous);
    free(previous_line);
    previous = tree;
    previous_line = copy;
    previous_length = size;
    return 1;
}

int binit_highlight(const char *line, size_t length, uint8_t *colors)
{
    if (!parse(line, length)) return 0;
    memset(colors, 0, length);
    paint(ts_tree_root_node(previous), line, colors, (uint32_t)length, 0, NULL);
    return 1;
}

int binit_highlight_extended(const char *line, size_t length, size_t cursor,
                            uint8_t *colors, struct binit_highlight_tokens *tokens)
{
    tokens->count = 0;
    if (!parse(line, length)) return 0;
    struct decoration extra = {.tokens = tokens, .cursor = cursor, .depth = 0, .overflow = 0,
                               .opening = {0}, .closing = {0}};
    memset(colors, 0, length);
    paint(ts_tree_root_node(previous), line, colors, (uint32_t)length, 0, &extra);
    memset(colors + extra.opening.start, HL_BRACKET, extra.opening.width);
    memset(colors + extra.closing.start, HL_BRACKET, extra.closing.width);
    return 1;
}
