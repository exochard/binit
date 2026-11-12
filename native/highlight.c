#include "highlight.h"
#include <stdlib.h>
#include <string.h>
#include <tree_sitter/api.h>

extern const TSLanguage *tree_sitter_bash(void);
static TSParser *parser;
static TSTree *previous;
static char *previous_line;
static uint32_t previous_length;

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

static void paint(TSNode node, const char *line, uint8_t *colors, uint32_t length, unsigned int depth)
{
    if (depth > 128 || ts_node_is_error(node) || ts_node_is_missing(node)) return;
    uint8_t color = node_color(ts_node_type(node));
    uint32_t start = ts_node_start_byte(node), end = ts_node_end_byte(node);
    if (!strcmp(ts_node_type(node), "heredoc_body") && end > start && end <= length && line[end - 1] == '\n') --end;
    if (color && start <= end && end <= length) memset(colors + start, color, end - start);
    uint32_t count = ts_node_child_count(node);
    for (uint32_t i = 0; i < count; ++i) paint(ts_node_child(node, i), line, colors, length, depth + 1);
}

int binit_highlight(const char *line, size_t length, uint8_t *colors)
{
    if (length > 4096) return 0;
    if (!parser) {
        parser = ts_parser_new();
        if (!parser || !ts_parser_set_language(parser, tree_sitter_bash())) {binit_highlight_clear(); return 0;}
    }
    uint32_t size = (uint32_t)length, start = 0, suffix = 0;
    if (previous) {
        while (start < previous_length && start < size && previous_line[start] == line[start]) ++start;
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
    memset(colors, 0, length);
    paint(ts_tree_root_node(tree), line, colors, size, 0);
    return 1;
}
