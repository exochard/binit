/* An advisory incremental-parser experiment, never an execution authority. */
#define _GNU_SOURCE 1
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <tree_sitter/api.h>

extern const TSLanguage *tree_sitter_bash(void);

static TSPoint point_at(const char *text, size_t size)
{
    TSPoint point = {0, 0};
    for (size_t i = 0; i < size; ++i) {
        if (text[i] == '\n') {++point.row; point.column = 0;}
        else ++point.column;
    }
    return point;
}

static bool advance(TSTreeCursor *cursor)
{
    if (ts_tree_cursor_goto_first_child(cursor)) return true;
    do {
        if (ts_tree_cursor_goto_next_sibling(cursor)) return true;
    } while (ts_tree_cursor_goto_parent(cursor));
    return false;
}

static bool same_tree(const TSTree *left, const TSTree *right)
{
    TSTreeCursor a = ts_tree_cursor_new(ts_tree_root_node(left));
    TSTreeCursor b = ts_tree_cursor_new(ts_tree_root_node(right));
    bool equal = true;
    for (;;) {
        TSNode x = ts_tree_cursor_current_node(&a), y = ts_tree_cursor_current_node(&b);
        TSPoint xs = ts_node_start_point(x), ys = ts_node_start_point(y);
        TSPoint xe = ts_node_end_point(x), ye = ts_node_end_point(y);
        if (strcmp(ts_node_type(x), ts_node_type(y)) != 0 ||
            ts_node_start_byte(x) != ts_node_start_byte(y) || ts_node_end_byte(x) != ts_node_end_byte(y) ||
            xs.row != ys.row || xs.column != ys.column || xe.row != ye.row || xe.column != ye.column ||
            ts_node_child_count(x) != ts_node_child_count(y) || ts_node_is_missing(x) != ts_node_is_missing(y) ||
            ts_node_has_error(x) != ts_node_has_error(y)) {equal = false; break;}
        bool next_a = advance(&a), next_b = advance(&b);
        if (next_a != next_b) {equal = false; break;}
        if (!next_a) break;
    }
    ts_tree_cursor_delete(&a);
    ts_tree_cursor_delete(&b);
    return equal;
}

static uint64_t nanoseconds(void)
{
    struct timespec time;
    if (clock_gettime(CLOCK_MONOTONIC, &time) != 0) return 0;
    return (uint64_t)time.tv_sec * UINT64_C(1000000000) + (uint64_t)time.tv_nsec;
}

static int sequence_check(TSParser *parser, const char *text)
{
    uint32_t size = (uint32_t)strlen(text), old_size = 0, steps = 0;
    TSTree *tree = ts_parser_parse_string(parser, NULL, "", 0);
    if (!tree) return 2;
    bool equal = true;
    for (uint32_t i = 0; i < 2 * size; ++i) {
        uint32_t new_size = i < size ? i + 1 : 2 * size - i - 1;
        uint32_t start = old_size < new_size ? old_size : new_size;
        TSInputEdit edit = {start, old_size, new_size, point_at(text, start), point_at(text, old_size), point_at(text, new_size)};
        ts_tree_edit(tree, &edit);
        TSTree *next = ts_parser_parse_string(parser, tree, text, new_size);
        TSTree *fresh = ts_parser_parse_string(parser, NULL, text, new_size);
        if (!next || !fresh || !same_tree(next, fresh)) equal = false;
        ts_tree_delete(tree);
        ts_tree_delete(fresh);
        tree = next;
        ++steps;
        if (!tree) break;
        old_size = new_size;
    }
    ts_tree_delete(tree);
    printf("{\"sequence_matches\":%s,\"steps\":%u}\n", equal ? "true" : "false", steps);
    return 0;
}

int main(int argc, char **argv)
{
    if ((argc != 3 && argc != 4) || strlen(argv[1]) > 65536 || strlen(argv[2]) > 65536 || (argc == 4 && strlen(argv[3]) > 4096)) return 2;
    TSParser *parser = ts_parser_new();
    if (!ts_parser_set_language(parser, tree_sitter_bash())) {ts_parser_delete(parser); return 2;}
    if (strcmp(argv[1], "--sequence") == 0) {
        int status = sequence_check(parser, argv[2]);
        ts_parser_delete(parser);
        return status;
    }
    uint32_t old_size = (uint32_t)strlen(argv[1]), new_size = (uint32_t)strlen(argv[2]);
    TSTree *old = ts_parser_parse_string(parser, NULL, argv[1], old_size);
    if (!old) {ts_parser_delete(parser); return 2;}
    uint32_t start = 0, suffix = 0;
    while (start < old_size && start < new_size && argv[1][start] == argv[2][start]) ++start;
    while (suffix < old_size - start && suffix < new_size - start &&
           argv[1][old_size - suffix - 1] == argv[2][new_size - suffix - 1]) ++suffix;
    TSInputEdit edit = {start, old_size - suffix, new_size - suffix, point_at(argv[1], start),
                       point_at(argv[1], old_size - suffix), point_at(argv[2], new_size - suffix)};
    ts_tree_edit(old, &edit);
    uint64_t began = nanoseconds();
    TSTree *incremental = ts_parser_parse_string(parser, old, argv[2], new_size);
    uint64_t elapsed = nanoseconds() - began;
    TSTree *fresh = ts_parser_parse_string(parser, NULL, argv[2], new_size);
    if (!incremental || !fresh) {ts_tree_delete(incremental); ts_tree_delete(fresh); ts_tree_delete(old); ts_parser_delete(parser); return 2;}
    bool parsed = !ts_node_has_error(ts_tree_root_node(incremental));
    bool alias_applied = false;
    if (argc == 4) {
        const char *equals = strchr(argv[3], '=');
        size_t name_length = equals ? (size_t)(equals - argv[3]) : 0;
        if (name_length && name_length <= 4096 && name_length <= new_size &&
            name_length == strcspn(argv[2], " \t\n;|&()<>") && !strncmp(argv[2], argv[3], name_length)) {
            const char *value = equals + 1;
            size_t value_length = strnlen(value, 4097), remaining = new_size - name_length;
            if (value_length > 4096 || remaining > 65536) {ts_tree_delete(fresh); ts_tree_delete(incremental); ts_tree_delete(old); ts_parser_delete(parser); return 2;}
            char logical[65536 + 4096 + 1];
            const char *tail = argv[2];
            for (size_t i = 0; i < name_length; ++i) {
                if (*tail == '\0') {ts_tree_delete(fresh); ts_tree_delete(incremental); ts_tree_delete(old); ts_parser_delete(parser); return 2;}
                ++tail;
            }
            int written = snprintf(logical, sizeof(logical), "%s%s", value, tail);
            if (written < 0 || (size_t)written >= sizeof(logical)) {ts_tree_delete(fresh); ts_tree_delete(incremental); ts_tree_delete(old); ts_parser_delete(parser); return 2;}
            TSTree *expanded = ts_parser_parse_string(parser, NULL, logical, (uint32_t)(value_length + remaining));
            if (!expanded) {ts_tree_delete(fresh); ts_tree_delete(incremental); ts_tree_delete(old); ts_parser_delete(parser); return 2;}
            parsed = !ts_node_has_error(ts_tree_root_node(expanded));
            alias_applied = true;
            ts_tree_delete(expanded);
        }
    }
    printf("{\"parsed\":%s,\"alias_applied\":%s,\"incremental_matches_fresh\":%s,\"incremental_ns\":%llu,\"nodes\":[",
           parsed ? "true" : "false", alias_applied ? "true" : "false", same_tree(incremental, fresh) ? "true" : "false",
           (unsigned long long)elapsed);
    TSTreeCursor cursor = ts_tree_cursor_new(ts_tree_root_node(incremental));
    bool first = true;
    do {
        TSNode node = ts_tree_cursor_current_node(&cursor);
        printf("%s[%u,%u]", first ? "" : ",", ts_node_start_byte(node), ts_node_end_byte(node));
        first = false;
        uint32_t start_byte = ts_node_start_byte(node), end_byte = ts_node_end_byte(node);
        if (strcmp(ts_node_type(node), "heredoc_body") == 0 && end_byte > start_byte &&
            argv[2][end_byte - 1] == '\n')
            printf(",[%u,%u]", start_byte, end_byte - 1);
        if (strcmp(ts_node_type(node), "word") == 0 && end_byte + 2 <= new_size &&
            argv[2][end_byte] == '\\' && argv[2][end_byte + 1] == '\n') {
            TSNode next = ts_node_next_named_sibling(node);
            while (!ts_node_is_null(next) && strcmp(ts_node_type(next), "word") == 0 &&
                   ts_node_start_byte(next) == end_byte + 2) {
                end_byte = ts_node_end_byte(next);
                printf(",[%u,%u]", start_byte, end_byte);
                if (end_byte + 2 > new_size || argv[2][end_byte] != '\\' || argv[2][end_byte + 1] != '\n') break;
                next = ts_node_next_named_sibling(next);
            }
        }
    } while (advance(&cursor));
    puts("]}");
    ts_tree_cursor_delete(&cursor);
    ts_tree_delete(fresh);
    ts_tree_delete(incremental);
    ts_tree_delete(old);
    ts_parser_delete(parser);
    return 0;
}
