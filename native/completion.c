#define _GNU_SOURCE 1
#include <config.h>
#include <dlfcn.h>
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>
#include <wchar.h>
#include "builtins.h"
#include "variables.h"
#include "execute_cmd.h"
#include "common.h"
#include "shell.h"
#include "externs.h"
#include "alias.h"
#include "version.h"
#include "unwind_prot.h"
#include "xmalloc.h"
#include "quit.h"
#include <readline/readline.h>
#include <readline/history.h>
#include "menu.h"
#ifdef BINIT_HIGHLIGHT
#include "highlight.h"
#endif

#define EXPORT __attribute__((visibility("default")))
#define INPUT_LIMIT 128

enum key {
    KEY_UP = 256, KEY_DOWN, KEY_LEFT, KEY_RIGHT, KEY_BACKWARD,
    KEY_HOME, KEY_END, KEY_PAGE_UP, KEY_PAGE_DOWN, KEY_REPLAY
};

struct context {
    char *line;
    int point, mark;
    rl_compdisp_func_t *display;
    rl_command_func_t *last;
    rl_hook_func_t *event;
    rl_getc_func_t *input_getc;
    rl_hook_func_t *input_event;
    int query_items, show_all, prefix_first;
    bool transient_input, undo, captured, painted, resized, oversized, bare_escape, interrupted;
    int row, column, rows, columns, height, cells, cell_width;
    int input[INPUT_LIMIT];
    size_t input_count, input_index;
    char sequence[32];
    size_t sequence_length;
    struct binit_menu menu;
    bool filtering;
};

struct binding {
    Keymap map;
    rl_command_func_t *previous;
    bool owned;
};

static struct context *active;
static struct binding bindings[2];
static FUNMAP *function_entry;
static bool enabled;
static int terminal_support;
extern struct builtin binit_complete_struct;
static int native_complete(int, int);
static int wait_hook(void);
extern int running_trap;
static rl_hook_func_t *previous_pre_input;
static bool wait_installed;
static bool poll_installed;
static bool poll_running;
static long last_poll;
static rl_hook_func_t *previous_event_hook;
static bool suggestions_enabled;
static bool highlighting_enabled;
static bool suggestion_visible;
static bool redisplay_running;
static rl_voidfunc_t *previous_redisplay;
static struct binding suggestion_bindings[2];

#ifdef BINIT_HIGHLIGHT
static bool alias_context_unknown(const char *line)
{
    while (*line == ' ' || *line == '\t') ++line;
    size_t length = 0;
    while (line[length] && !strchr(" \t\n;|&()<>", line[length])) ++length;
    if (!length || length > 256) return false;
    char name[257];
    memcpy(name, line, length);
    name[length] = '\0';
    return find_alias(name) != NULL;
}
#endif

static int display_width(const char *text, size_t size)
{
    mbstate_t state = {0};
    size_t offset = 0;
    int width = 0;
    while (offset < size) {
        wchar_t character;
        size_t used = mbrtowc(&character, text + offset, size - offset, &state); // NOLINT(concurrency-mt-unsafe): Readline runs on Bash's shell thread.
        if (used == (size_t)-1 || used == (size_t)-2 || used == 0) return -1;
        int cells = wcwidth(character);
        if (cells < 0) return -1;
        width += cells;
        offset += used;
    }
    return width;
}

/* Readline marks prompt escapes as non-printing with SOH/STX.  Only SGR
   sequences are safe to account for without terminal cursor queries. */
static int prompt_width(const char *prompt)
{
    const unsigned char *p = (const unsigned char *)prompt;
    size_t length = strnlen(prompt, 8193);
    if (length > 8192) return -1;
    const unsigned char *end = p + length;
    mbstate_t state = {0};
    int width = 0;
    bool marked = false;
    while (p < end) {
        if (*p == 1 && !marked) {marked = true; ++p; continue;}
        if (*p == 2 && marked) {marked = false; ++p; continue;}
        if (*p == '\033' && p + 1 < end && p[1] == '[') {
            p += 2;
            while (p < end && ((*p >= '0' && *p <= '9') || *p == ';' || *p == ':')) ++p;
            if (p == end || *p != 'm') return -1;
            ++p;
            continue;
        }
        if (marked) return -1;
        wchar_t character;
        size_t used = mbrtowc(&character, (const char *)p, (size_t)(end - p), &state); // NOLINT(concurrency-mt-unsafe): Readline runs on Bash's shell thread.
        if (used == (size_t)-1 || used == (size_t)-2 || used == 0 || used > (size_t)(end - p)) return -1;
        int cells = wcwidth(character);
        if (cells < 0) return -1;
        width += cells;
        p += used;
    }
    return marked ? -1 : width;
}

static const char *history_suggestion(void)
{
    if (!suggestions_enabled || !interactive_shell || executing_builtin || running_trap || parse_and_execute_level ||
        rl_point != rl_end || rl_end <= 0 || rl_end > 4096 || !rl_line_buffer) return NULL;
    if (display_width(rl_line_buffer, (size_t)rl_end) < 0) return NULL;
    HIST_ENTRY **entries = history_list();
    if (!entries) return NULL;
    int lower = history_length > 1000 ? history_length - 1000 : 0;
    for (int i = history_length - 1; i >= lower; --i) {
        const char *line = entries[i]->line;
        if (!line || strncmp(line, rl_line_buffer, (size_t)rl_end) != 0 || !line[rl_end]) continue;
        const char *suffix = line + rl_end;
        size_t size = strnlen(suffix, 1025);
        if (size > 1024 || display_width(suffix, size) < 0) return NULL;
        return suffix;
    }
    return NULL;
}

static void suggestion_redisplay(void)
{
    if (redisplay_running) {rl_redisplay(); return;}
    redisplay_running = true;
    if (previous_redisplay && previous_redisplay != suggestion_redisplay) previous_redisplay();
    else rl_redisplay();
    redisplay_running = false;
    if (!interactive_shell || executing_builtin || running_trap || parse_and_execute_level) return;
#ifdef BINIT_HIGHLIGHT
    if (highlighting_enabled && rl_outstream && rl_line_buffer && rl_end > 0 && rl_end <= 4096 && rl_point >= 0 && rl_point <= rl_end) {
        const char *prompt = rl_display_prompt ? rl_display_prompt : "";
        int rows, columns;
        rl_get_screen_size(&rows, &columns);
        (void)rows;
        int width = display_width(rl_line_buffer, (size_t)rl_end);
        int before = display_width(rl_line_buffer, (size_t)rl_point);
        uint8_t colors[4096];
        bool uncertain = alias_context_unknown(rl_line_buffer);
        int prompt_cells = prompt_width(prompt);
        if (prompt_cells >= 0 && width >= 0 && before >= 0 && columns > 1 &&
            (size_t)prompt_cells + (size_t)width < (size_t)columns &&
            (uncertain ? (memset(colors, 0, (size_t)rl_end), true) : binit_highlight(rl_line_buffer, (size_t)rl_end, colors))) {
            static const char *styles[] = {"0", "2", "32", "36", "33", "35"};
            fputs("\0337", rl_outstream);
            if (before) fprintf(rl_outstream, "\033[%dD", before);
            unsigned int color = 0;
            for (int i = 0; i < rl_end; ++i) {
                if (colors[i] != color) {color = colors[i]; fprintf(rl_outstream, "\033[%sm", styles[color]);}
                fputc((unsigned char)rl_line_buffer[i], rl_outstream);
            }
            fputs("\033[0m\0338", rl_outstream);
            fflush(rl_outstream);
        }
    }
#endif
    if (!suggestions_enabled || !rl_outstream) return;
    if (rl_point != rl_end) {
        if (suggestion_visible && rl_line_buffer && rl_point >= 0 && rl_point < rl_end) {
            int cells = display_width(rl_line_buffer + rl_point, (size_t)(rl_end - rl_point));
            if (cells >= 0) fprintf(rl_outstream, "\0337\033[%dC\033[K\0338", cells);
            fflush(rl_outstream);
            suggestion_visible = false;
        }
        return;
    }
    const char *suffix = history_suggestion();
    const char *prompt = rl_display_prompt ? rl_display_prompt : "";
    int rows, columns;
    rl_get_screen_size(&rows, &columns);
    (void)rows;
    int prompt_cells = prompt_width(prompt);
    if (prompt_cells < 0 || columns <= 1) suffix = NULL;
    int line_width = display_width(rl_line_buffer, (size_t)rl_end);
    int suffix_width = suffix ? display_width(suffix, strlen(suffix)) : 0;
    if (suffix && (line_width < 0 || suffix_width < 0 || (size_t)prompt_cells + (size_t)line_width + (size_t)suffix_width >= (size_t)columns)) suffix = NULL;
    if (!suffix && !suggestion_visible) return;
    fputs("\033[K", rl_outstream);
    if (suffix) fprintf(rl_outstream, "\0337\033[2m%s\033[0m\0338", suffix);
    fflush(rl_outstream);
    suggestion_visible = suffix != NULL;
}

static int accept_suggestion(int count, int key)
{
    const char *suffix = history_suggestion();
    if (suffix) return rl_insert_text(suffix) < 0 ? 1 : 0;
    for (size_t i = 0; i < 2; ++i)
        if (suggestion_bindings[i].map == rl_get_keymap() && suggestion_bindings[i].previous)
            return suggestion_bindings[i].previous(count, key);
    return rl_forward_char(count, key);
}

static void disable_suggestions(void)
{
    suggestions_enabled = false;
    suggestion_visible = false;
    if (!highlighting_enabled) {
        if (rl_redisplay_function == suggestion_redisplay) rl_redisplay_function = previous_redisplay;
        previous_redisplay = NULL;
    }
    for (size_t i = 0; i < 2; ++i) {
        struct binding *binding = &suggestion_bindings[i];
        if (binding->owned && binding->map[6].type == ISFUNC && binding->map[6].function == accept_suggestion)
            rl_bind_key_in_map(6, binding->previous, binding->map);
        binding->owned = false;
    }
}

static int enable_suggestions(void)
{
    if (suggestions_enabled) return EXECUTION_SUCCESS;
    const char *term = get_string_value("TERM");
    if ((rl_readline_state & RL_STATE_INITIALIZED) == 0 || !term || strcmp(term, "dumb") == 0) return EXECUTION_FAILURE;
    const char *names[] = {"emacs-standard", "vi-insert"};
    for (size_t i = 0; i < 2; ++i) {
        Keymap map = rl_get_keymap_by_name(names[i]);
        if (!map || map[6].type != ISFUNC || map[6].function != rl_forward_char) continue;
        suggestion_bindings[i] = (struct binding){map, map[6].function, true};
        rl_bind_key_in_map(6, accept_suggestion, map);
    }
    if (!highlighting_enabled) {
        previous_redisplay = rl_redisplay_function;
        rl_redisplay_function = suggestion_redisplay;
    }
    suggestions_enabled = true;
    return EXECUTION_SUCCESS;
}

static int option(const char *name)
{
    const char *value = rl_variable_value(name);
    return value && strcmp(value, "on") == 0;
}

static long milliseconds(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return 0;
    return now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static void terminal_size(int *rows, int *columns)
{
    struct winsize size;
    rl_get_screen_size(rows, columns);
    if (ioctl(fileno(rl_instream), TIOCGWINSZ, &size) == 0 && size.ws_row && size.ws_col) {
        *rows = size.ws_row;
        *columns = size.ws_col;
    }
}

static int timed_getc(FILE *stream)
{
    struct pollfd fd = {.fd = fileno(stream), .events = POLLIN};
    int ready = poll(&fd, 1, 50);
    if (ready <= 0) {
        if (active) active->interrupted = ready < 0 && errno == EINTR;
        return READERR;
    }
    rl_getc_func_t *reader = active ? active->input_getc : NULL;
    return reader ? reader(stream) : rl_getc(stream);
}

static int read_timed(struct context *ctx)
{
    ctx->input_getc = rl_getc_function;
    ctx->input_event = rl_event_hook;
    ctx->transient_input = true;
    ctx->interrupted = false;
    rl_getc_function = timed_getc;
    rl_event_hook = NULL;
    int key = rl_read_key();
    rl_getc_function = ctx->input_getc;
    rl_event_hook = ctx->input_event;
    ctx->transient_input = false;
    if ((ctx->interrupted || interrupt_state || terminating_signal) && rl_signal_event_hook) (*rl_signal_event_hook)();
    return key;
}

static bool position_reply(const char *text, size_t length, int *row, int *column)
{
    if (length < 6 || text[0] != 27 || text[1] != '[' || text[length - 1] != 'R') return false;
    int values[2] = {0};
    size_t i = 2;
    for (size_t field = 0; field < 2; ++field) {
        size_t digits = 0;
        while (i < length && text[i] >= '0' && text[i] <= '9') {
            if (++digits > 5) return false;
            values[field] = values[field] * 10 + text[i++] - '0';
        }
        if (digits == 0 || values[field] == 0 || values[field] > 65535) return false;
        if (i >= length || text[i] != (field == 0 ? ';' : 'R')) return false;
        ++i;
    }
    if (i != length) return false;
    *row = values[0];
    *column = values[1];
    return true;
}

static bool cursor_position(struct context *ctx, int *row, int *column)
{
    char reply[32] = {0};
    size_t length = 0;
    fputs("\033[6n", rl_outstream);
    fflush(rl_outstream);
    long deadline = milliseconds() + 120;
    bool valid = false;
    while (milliseconds() < deadline && ctx->input_count + length < INPUT_LIMIT - 8) {
        int key = read_timed(ctx);
        if (key < 0) break;
        if (key == 27 && length) {
            for (size_t i = 0; i < length; ++i) ctx->input[ctx->input_count++] = (unsigned char)reply[i];
            length = 0;
        }
        if (length == 0 && key != 27) ctx->input[ctx->input_count++] = key;
        else {
            reply[length++] = (char)key;
            if (position_reply(reply, length, row, column)) {valid = true; length = 0; break;}
            if (length == sizeof reply || (length == 2 && key != '[') ||
                (length > 2 && (key < '0' || key > '9') && key != ';')) {
                for (size_t i = 0; i < length; ++i) ctx->input[ctx->input_count++] = (unsigned char)reply[i];
                length = 0;
            }
        }
    }
    for (size_t i = 0; i < length; ++i) ctx->input[ctx->input_count++] = (unsigned char)reply[i];
    return valid;
}

static void move_cursor(int row, int column)
{
    fprintf(rl_outstream, "\033[%d;%dH", row, column);
}

static const char *label(const char *value)
{
    const char *slash = strrchr(value, '/');
    return slash && slash[1] ? slash + 1 : value;
}

static void clear_panel(struct context *ctx)
{
    if (!ctx->painted) return;
    int rows, columns;
    terminal_size(&rows, &columns);
    (void)columns;
    for (int i = 1; i <= ctx->height && ctx->row + i <= rows; ++i) {
        move_cursor(ctx->row + i, 1);
        fputs("\033[0m\033[2K", rl_outstream);
    }
    move_cursor(ctx->row, ctx->column);
    fflush(rl_outstream);
    ctx->painted = false;
}

static void paint_menu(struct context *ctx)
{
    char text[2048], heading[2048];
    size_t selected = ctx->menu.count ? ctx->menu.selected + 1 : 0;
    if (ctx->filtering) {
        char query[sizeof ctx->menu.query * 6];
        binit_text(query, sizeof query, ctx->menu.query, ctx->columns - 18);
        snprintf(heading, sizeof heading, "/%s  %zu/%zu", query, selected, ctx->menu.count);
    } else snprintf(heading, sizeof heading, "Completions %zu/%zu", selected, ctx->menu.count);
    binit_text(text, sizeof text, heading, ctx->columns - 1);
    move_cursor(ctx->row + 1, 1);
    fprintf(rl_outstream, "\033[2K\033[2m%s\033[0m", text);
    size_t page_size = (size_t)(ctx->height - 2) * (size_t)ctx->cells;
    size_t first = ctx->menu.count ? ctx->menu.selected / page_size * page_size : 0;
    for (int row = 0; row < ctx->height - 2; ++row) {
        move_cursor(ctx->row + 2 + row, 1);
        fputs("\033[2K", rl_outstream);
        for (int column = 0; column < ctx->cells; ++column) {
            size_t item = first + (size_t)(row * ctx->cells + column);
            if (item >= ctx->menu.count) break;
            move_cursor(ctx->row + 2 + row, column * ctx->cell_width + 1);
            const char *value = ctx->menu.values[ctx->menu.visible[item]];
            int width = binit_text(text, sizeof text, label(value), ctx->cell_width - 3);
            bool chosen = item == ctx->menu.selected;
            fprintf(rl_outstream, "%s%s%s", chosen ? "\033[7m" : "", chosen ? "> " : "  ", text);
            for (int i = width + 2; i < ctx->cell_width; ++i) fputc(' ', rl_outstream);
            if (chosen) fputs("\033[0m", rl_outstream);
        }
    }
    const char *hint = ctx->filtering ? "Type to filter  Backspace erase  Enter insert  Esc cancel" :
                       "Tab next  arrows/hjkl move  / filter  Enter insert  Esc cancel";
    if (ctx->columns < 65) hint = "Tab move  / filter  Enter insert  Esc cancel";
    if (ctx->columns < 44) hint = "Enter insert  Esc cancel";
    if (ctx->columns < 25) hint = "Enter insert  Esc";
    binit_text(text, sizeof text, hint, ctx->columns - 1);
    move_cursor(ctx->row + ctx->height, 1);
    fprintf(rl_outstream, "\033[2K\033[2m%s\033[0m", text);
    move_cursor(ctx->row, ctx->column);
    fflush(rl_outstream);
}

static int menu_event(void)
{
    if (active) {
        int rows, columns;
        terminal_size(&rows, &columns);
        if (rows != active->rows || columns != active->columns) {
            active->resized = true;
        }
    }
    return 0;
}

static int input_key(struct context *ctx, bool timed)
{
    if (ctx->input_index < ctx->input_count) return ctx->input[ctx->input_index++];
    for (;;) {
        int key = read_timed(ctx);
        if (key != READERR || timed) return key;
        menu_event();
        if (ctx->resized || !enabled) return 27;
    }
}

static int read_key(struct context *ctx)
{
    int key = input_key(ctx, false);
    ctx->sequence_length = 0;
    if (key != 27) return key;
    if (ctx->resized) return key;
    ctx->sequence[ctx->sequence_length++] = 27;
    for (size_t i = 1; i < sizeof ctx->sequence; ++i) {
        int next = input_key(ctx, true);
        if (next < 0) {
            ctx->bare_escape = ctx->sequence_length == 1;
            return ctx->bare_escape ? 27 : KEY_REPLAY;
        }
        ctx->sequence[ctx->sequence_length++] = (char)next;
        if (i == 1 && next != '[' && next != 'O') return KEY_REPLAY;
        if (i < 2 || next < '@' || next > '~') continue;
        if (i == 2) {
            if (next == 'A') return KEY_UP;
            if (next == 'B') return KEY_DOWN;
            if (next == 'C') return KEY_RIGHT;
            if (next == 'D') return KEY_LEFT;
            if (next == 'Z') return KEY_BACKWARD;
            if (next == 'H') return KEY_HOME;
            if (next == 'F') return KEY_END;
        }
        if (i == 3 && next == '~') {
            if (ctx->sequence[2] == '1' || ctx->sequence[2] == '7') return KEY_HOME;
            if (ctx->sequence[2] == '4' || ctx->sequence[2] == '8') return KEY_END;
            if (ctx->sequence[2] == '5') return KEY_PAGE_UP;
            if (ctx->sequence[2] == '6') return KEY_PAGE_DOWN;
        }
        return KEY_REPLAY;
    }
    return KEY_REPLAY;
}

static void replay_input(struct context *ctx, const char *prefix, size_t length)
{
    size_t remaining = ctx->input_count - ctx->input_index;
    char *macro = xmalloc(length + remaining + 1);
    size_t used = 0;
    for (size_t i = 0; i < length; ++i) if (prefix[i]) macro[used++] = prefix[i];
    for (size_t i = ctx->input_index; i < ctx->input_count; ++i) {
        if (ctx->input[i] > 0 && ctx->input[i] <= 255) macro[used++] = (char)ctx->input[i];
    }
    macro[used] = '\0';
    ctx->input_index = ctx->input_count;
    if (used) rl_push_macro_input(macro);
    else free(macro);
}

static int picker(struct context *ctx)
{
    rl_point = rl_end;
    rl_redisplay();
    terminal_size(&ctx->rows, &ctx->columns);
    if (ctx->menu.total == 0 || ctx->rows < 4 || ctx->columns < 20 ||
        !cursor_position(ctx, &ctx->row, &ctx->column) ||
        ctx->row > ctx->rows || ctx->column > ctx->columns) {
        terminal_support = -1;
        return -1;
    }
    terminal_support = 1;
    char text[2048];
    int longest = 0;
    for (size_t i = 0; i < ctx->menu.total; ++i) {
        int width = binit_text(text, sizeof text, label(ctx->menu.values[i]), 32);
        if (width > longest) longest = width;
    }
    int cell = longest + 3;
    if (cell < 12) cell = 12;
    ctx->cells = (ctx->columns - 2) / cell;
    if (ctx->cells < 1) ctx->cells = 1;
    if (ctx->cells > 4) ctx->cells = 4;
    if ((size_t)ctx->cells > ctx->menu.total) ctx->cells = (int)ctx->menu.total;
    if (ctx->cells < 1) return -1;
    ctx->cell_width = (ctx->columns - 2) / ctx->cells;
    int rows = (int)((ctx->menu.total + (size_t)ctx->cells - 1) / (size_t)ctx->cells);
    if (rows > 6) rows = 6;
    if (rows > ctx->rows / 3) rows = ctx->rows / 3;
    if (rows < 1) rows = 1;
    ctx->height = rows + 2;
    int scroll = ctx->row + ctx->height - ctx->rows;
    if (scroll > 0) {
        move_cursor(ctx->rows, 1);
        for (int i = 0; i < scroll; ++i) fputc('\n', rl_outstream);
        ctx->row -= scroll;
    }
    ctx->painted = true;
    int choice = 0;
    while (enabled) {
        paint_menu(ctx);
        int key = read_key(ctx);
        if (ctx->resized) break;
        if (key == '\r' || key == '\n') {
            choice = (int)binit_menu_choice(&ctx->menu);
            if (choice) break;
            continue;
        }
        if (key == 27 || key == 7 || key == 4 || key < 0) break;
        if (key == 3) {raise(SIGINT); rl_check_signals(); if (rl_signal_event_hook) (*rl_signal_event_hook)(); break;}
        if (key == KEY_REPLAY) {replay_input(ctx, ctx->sequence, ctx->sequence_length); break;}
        if (key == '\t' || key == 14) binit_menu_move(&ctx->menu, 1);
        else if (key == KEY_BACKWARD || key == 16) binit_menu_move(&ctx->menu, -1);
        else if (key == KEY_UP || (!ctx->filtering && key == 'k')) binit_menu_grid_move(&ctx->menu, 0, -1, (size_t)ctx->cells);
        else if (key == KEY_DOWN || (!ctx->filtering && key == 'j')) binit_menu_grid_move(&ctx->menu, 0, 1, (size_t)ctx->cells);
        else if (key == KEY_LEFT || (!ctx->filtering && key == 'h')) binit_menu_grid_move(&ctx->menu, -1, 0, (size_t)ctx->cells);
        else if (key == KEY_RIGHT || (!ctx->filtering && key == 'l')) binit_menu_grid_move(&ctx->menu, 1, 0, (size_t)ctx->cells);
        else if (key == KEY_HOME) ctx->menu.selected = 0;
        else if (key == KEY_END && ctx->menu.count) ctx->menu.selected = ctx->menu.count - 1;
        else if (key == KEY_PAGE_UP) binit_menu_move(&ctx->menu, -(ptrdiff_t)(ctx->height - 2) * ctx->cells);
        else if (key == KEY_PAGE_DOWN) binit_menu_move(&ctx->menu, (ptrdiff_t)(ctx->height - 2) * ctx->cells);
        else if (!ctx->filtering && key == '/') ctx->filtering = true;
        else if (ctx->filtering && (key == 127 || key == 8)) binit_menu_backspace(&ctx->menu);
        else if (ctx->filtering && key == 21) binit_menu_clear(&ctx->menu);
        else if (key >= 32 && key <= 255 && key != 127) {
            ctx->filtering = true;
            binit_menu_append(&ctx->menu, (unsigned char)key);
        } else {
            char byte = (char)key;
            replay_input(ctx, &byte, 1);
            break;
        }
    }
    rl_event_hook = ctx->event;
    if (ctx->resized) {
        int row, column;
        if (cursor_position(ctx, &row, &column)) {
            terminal_size(&ctx->rows, &ctx->columns);
            if (row < ctx->rows) {move_cursor(row + 1, 1); fputs("\033[J", rl_outstream);}
            ctx->row = row;
            ctx->column = column;
            ctx->painted = false;
        }
    }
    clear_panel(ctx);
    replay_input(ctx, "", 0);
    return choice;
}

static void capture(char **matches, int count, int width)
{
    (void)width;
    if (active && count > 1 && count <= BINIT_MENU_LIMIT) {
        active->captured = binit_menu_init(&active->menu, (const char *const *)(matches + 1), (size_t)count) == 0;
        active->oversized = !active->captured;
    } else if (active && count > BINIT_MENU_LIMIT) {
        active->oversized = true;
    }
}

static void completion_cleanup(void *data)
{
    struct context *ctx = data;
    if (ctx->transient_input) {
        rl_getc_function = ctx->input_getc;
        rl_event_hook = ctx->input_event;
    }
    clear_panel(ctx);
    rl_completion_display_matches_hook = ctx->display;
    rl_completion_query_items = ctx->query_items;
    rl_variable_bind("show-all-if-ambiguous", ctx->show_all ? "on" : "off");
    rl_variable_bind("menu-complete-display-prefix", ctx->prefix_first ? "on" : "off");
    rl_last_func = ctx->last;
    rl_event_hook = ctx->event;
    if (ctx->undo) rl_end_undo_group();
    binit_menu_free(&ctx->menu);
    free(ctx->line);
    free(ctx);
    active = NULL;
}

static bool resume_after_escape(void)
{
    // Retain a possible paste header across reads; replay other bytes through
    // Readline so custom bindings, overwrite mode, and undo keep their meaning.
    static const char header[] = "[200~";
    char buffered[sizeof header + 1];
    int next = rl_read_key();
    size_t length = 0;
    if (next == '[') {
        for (size_t i = 0; i < sizeof header - 1; ++i) {
            buffered[length++] = (char)next;
            if (i + 1 == sizeof header - 1) break;
            next = rl_read_key();
            if (next != header[i + 1]) break;
        }
        if (length == sizeof header - 1) {
            char *paste = xmalloc(sizeof header + 1);
            paste[0] = 27;
            memcpy(paste + 1, header, sizeof header);
            rl_push_macro_input(paste);
            return false;
        }
    }
    if (next == '\t' && length == 0) return true;
    if (next > 0 && next <= 255) buffered[length++] = (char)next;
    if (length) {
        char *input = xmalloc(length + 1);
        memcpy(input, buffered, length);
        input[length] = '\0';
        rl_push_macro_input(input);
    }
    return false;
}

static int native_complete(int count, int key)
{
restart:;
    const char *term = get_string_value("TERM");
    if (!enabled || active || rl_executing_macro || terminal_support < 0 || !term || strcmp(term, "dumb") == 0 ||
        !rl_instream || !rl_outstream || !isatty(fileno(rl_instream)) || !isatty(fileno(rl_outstream))) {
        return rl_complete(count, key);
    }
    struct context *ctx = calloc(1, sizeof *ctx);
    if (!ctx) return rl_complete(count, key);
    ctx->line = strdup(rl_line_buffer ? rl_line_buffer : "");
    if (!ctx->line) {free(ctx); return rl_complete(count, key);}
    ctx->point = rl_point;
    ctx->mark = rl_mark;
    ctx->display = rl_completion_display_matches_hook;
    ctx->last = rl_last_func;
    ctx->event = rl_event_hook;
    ctx->query_items = rl_completion_query_items;
    ctx->show_all = option("show-all-if-ambiguous");
    ctx->prefix_first = option("menu-complete-display-prefix");
    active = ctx;
    begin_unwind_frame("binit-completion");
    add_unwind_protect(completion_cleanup, ctx);
    rl_completion_display_matches_hook = capture;
    rl_completion_query_items = 0;
    rl_variable_bind("show-all-if-ambiguous", "on");
    rl_variable_bind("menu-complete-display-prefix", "off");
    rl_last_func = NULL;
    rl_begin_undo_group();
    ctx->undo = true;
    rl_menu_complete(0, key);
    rl_end_undo_group();
    ctx->undo = false;
    rl_completion_display_matches_hook = ctx->display;
    rl_completion_query_items = ctx->query_items;
    rl_variable_bind("show-all-if-ambiguous", ctx->show_all ? "on" : "off");
    rl_variable_bind("menu-complete-display-prefix", ctx->prefix_first ? "on" : "off");
    if (ctx->oversized) {
        rl_do_undo();
        rl_replace_line(ctx->line, 0);
        rl_point = ctx->point;
        rl_mark = ctx->mark;
        run_unwind_frame("binit-completion");
        return rl_complete(count, key);
    }
    if (ctx->captured) {
        rl_do_undo();
        rl_replace_line(ctx->line, 0);
        rl_point = ctx->point;
        rl_mark = ctx->mark;
        int selected = enabled ? picker(ctx) : 0;
        rl_point = ctx->point;
        rl_mark = ctx->mark;
        if (selected < 0) {
            replay_input(ctx, NULL, 0);
            run_unwind_frame("binit-completion");
            return rl_complete(count, key);
        }
        if (selected != 0) {
            rl_begin_undo_group();
            ctx->undo = true;
            rl_last_func = rl_menu_complete;
            rl_menu_complete(selected, key);
            rl_end_undo_group();
            ctx->undo = false;
        }
    } else if (strcmp(ctx->line, rl_line_buffer) == 0) rl_do_undo();
    bool resume = ctx->bare_escape && !ctx->resized;
    replay_input(ctx, NULL, 0);
    run_unwind_frame("binit-completion");
    if (resume) {
        rl_redisplay();
        if (resume_after_escape()) {count = 1; key = '\t'; goto restart;}
    }
    return 0;
}

static void restore_readline_signals(void *ignored)
{
    (void)ignored;
    rl_set_signals();
}

static void restore_wait_parser(void *state)
{
    restore_parser_state(state);
}

/*
 * Readline calls the pre-input hook after the prompt is drawn and before it
 * reads a key. The dispatch runs only for the top-level command reader: `read
 * -e` has a builtin running, `bind -x` and nested evaluation raise the parse
 * level, and a trap sets running_trap. Bash itself does not use this hook.
 */
static void run_wait_dispatch(const char *command, bool always_redraw)
{
    sh_parser_state_t state;
    int status = last_command_exit_value;
    char *last = get_string_value("_");
    char *saved = last ? strdup(last) : NULL;
    begin_unwind_frame("binit-wait");
    save_parser_state(&state);
    rl_clear_signals();
    add_unwind_protect(free, saved);
    add_unwind_protect(restore_wait_parser, &state);
    add_unwind_protect(restore_readline_signals, NULL);
#ifdef SEVAL_NOTIFY
    int flags = SEVAL_NOTIFY | SEVAL_NOHIST;
#else
    /* Bash 5.2 uses this combination for Readline's bind -x callbacks. */
    int flags = SEVAL_NONINT | SEVAL_NOHIST | SEVAL_NOOPTIMIZE;
#endif
    parse_and_execute(savestring(command), "binit_wait", flags);
    rl_set_signals();
    restore_parser_state(&state);
    discard_unwind_frame("binit-wait");
    if (saved) {
        SHELL_VAR *lastarg = bind_variable("_", saved, 0);
        if (lastarg) VUNSETATTR(lastarg, att_exported);
    }
    free(saved);
    last_command_exit_value = status;
    const char *changed = get_string_value("__binit_event_changed");
    if (always_redraw || (changed && strcmp(changed, "1") == 0)) {
        /* Output may have moved the cursor; forced redisplay assumes a fresh line. */
        rl_clear_visible_line();
        rl_forced_update_display();
    }
}

static int wait_hook(void)
{
    if (previous_pre_input && previous_pre_input != wait_hook) previous_pre_input();
    if (!wait_installed || !interactive_shell || executing_builtin || running_trap || parse_and_execute_level ||
        get_current_prompt_level() != 1) return 0;
    SHELL_VAR *pending = find_variable("__binit_wait_pending");
    if (!pending || !value_cell(pending) || strcmp(value_cell(pending), "1") != 0) return 0;
    run_wait_dispatch("__binit_wait_dispatch", true);
    return 0;
}

static int enable_wait(void)
{
    if (!wait_installed) {
        previous_pre_input = rl_pre_input_hook;
        rl_pre_input_hook = wait_hook;
        wait_installed = true;
    }
    return EXECUTION_SUCCESS;
}

static void remove_wait_hook(void)
{
    if (wait_installed && rl_pre_input_hook == wait_hook) rl_pre_input_hook = previous_pre_input;
    wait_installed = false;
    previous_pre_input = NULL;
}

static int poll_hook(void)
{
    if (poll_running) return 0;
    poll_running = true;
    if (previous_event_hook && previous_event_hook != poll_hook) previous_event_hook();
    poll_running = false;
    if (!poll_installed || !interactive_shell || executing_builtin || running_trap || parse_and_execute_level ||
        get_current_prompt_level() != 1 || rl_end != 0) return 0;
    SHELL_VAR *pending = find_variable("__binit_dispatch_pending");
    if (!pending || !value_cell(pending) || strcmp(value_cell(pending), "1") != 0) return 0;
    int saved_errno = errno;
    long now = milliseconds();
    errno = saved_errno;
    if (now <= 0 || (last_poll > 0 && now >= last_poll && now - last_poll < 250)) return 0;
    last_poll = now;
    run_wait_dispatch("__binit_event_dispatch", false);
    return 0;
}

static int enable_poll(void)
{
    if (!poll_installed) {
        previous_event_hook = rl_event_hook;
        rl_event_hook = poll_hook;
        last_poll = 0;
        poll_installed = true;
    }
    return EXECUTION_SUCCESS;
}

static void remove_poll_hook(void)
{
    if (poll_installed && rl_event_hook == poll_hook) rl_event_hook = previous_event_hook;
    poll_installed = false;
    last_poll = 0;
    previous_event_hook = NULL;
}

static void disable_menu(void)
{
    enabled = false;
    remove_wait_hook();
    for (size_t i = 0; i < 2; ++i) {
        struct binding *binding = &bindings[i];
        if (binding->owned && binding->map['\t'].type == ISFUNC && binding->map['\t'].function == native_complete) {
            rl_bind_key_in_map('\t', binding->previous, binding->map);
        }
        binding->owned = false;
    }
    if (function_entry) function_entry->function = rl_complete;
}

static int enable_menu(void)
{
    if ((rl_readline_state & RL_STATE_INITIALIZED) == 0 || rl_readline_version < 0x0802) return EXECUTION_FAILURE;
    const char *term = get_string_value("TERM");
    if (!term || strcmp(term, "dumb") == 0) return EXECUTION_FAILURE;
    const char *names[] = {"emacs-standard", "vi-insert"};
    bool available = false;
    for (size_t i = 0; i < 2; ++i) {
        Keymap map = rl_get_keymap_by_name(names[i]);
        if (!map || map['\t'].type != ISFUNC) continue;
        rl_command_func_t *previous = map['\t'].function;
        if (previous != rl_complete && previous != rl_menu_complete && previous != rl_backward_menu_complete && previous != native_complete) continue;
        if (previous != native_complete) bindings[i] = (struct binding){map, previous, true};
        rl_bind_key_in_map('\t', native_complete, map);
        available = true;
    }
    if (!available) return EXECUTION_FAILURE;
    if (!function_entry) {
        for (size_t i = 0; funmap && funmap[i]; ++i) {
            if (strcmp(funmap[i]->name, "binit-complete") == 0) {function_entry = funmap[i]; break;}
        }
        if (!function_entry) {
            char *name = strdup("binit-complete");
            if (!name) {disable_menu(); return EXECUTION_FAILURE;}
            rl_add_funmap_entry(name, native_complete);
            for (size_t i = 0; funmap && funmap[i]; ++i) {
                if (funmap[i]->name == name) {function_entry = funmap[i]; break;}
            }
        }
    }
    if (function_entry) function_entry->function = native_complete;
    terminal_support = 0;
    enabled = true;
    return EXECUTION_SUCCESS;
}

EXPORT int binit_complete_builtin(WORD_LIST *arguments)
{
    const char *action = arguments ? arguments->word->word : "status";
    if (strcmp(action, "editor") == 0) {
        WORD_LIST *feature = arguments->next;
        WORD_LIST *setting = feature ? feature->next : NULL;
        if (!feature || !setting || setting->next) return EX_USAGE;
        if (strcmp(feature->word->word, "suggestions") == 0) {
            if (strcmp(setting->word->word, "on") == 0) return enable_suggestions();
            if (strcmp(setting->word->word, "off") == 0) {disable_suggestions(); return EXECUTION_SUCCESS;}
        }
        if (strcmp(feature->word->word, "highlighting") == 0) {
#ifdef BINIT_HIGHLIGHT
            if (strcmp(setting->word->word, "on") == 0) {
                const char *term = get_string_value("TERM");
                if ((rl_readline_state & RL_STATE_INITIALIZED) == 0 || !term || strcmp(term, "dumb") == 0) return EXECUTION_FAILURE;
                if (!highlighting_enabled && !suggestions_enabled) {previous_redisplay = rl_redisplay_function; rl_redisplay_function = suggestion_redisplay;}
                highlighting_enabled = true;
                return EXECUTION_SUCCESS;
            }
            if (strcmp(setting->word->word, "off") == 0) {
                highlighting_enabled = false;
                binit_highlight_clear();
                if (!suggestions_enabled) {if (rl_redisplay_function == suggestion_redisplay) rl_redisplay_function = previous_redisplay; previous_redisplay = NULL;}
                return EXECUTION_SUCCESS;
            }
#else
            builtin_error("highlighting unavailable: rebuild with BINIT_SYNTAX_SOURCE");
            return EXECUTION_FAILURE;
#endif
        }
        return EX_USAGE;
    }
    if (arguments && arguments->next) return EX_USAGE;
    if (strcmp(action, "on") == 0) return enable_menu();
    if (strcmp(action, "wait") == 0) return enable_wait();
    if (strcmp(action, "poll") == 0) return enable_poll();
    if (strcmp(action, "poll-off") == 0) {remove_poll_hook(); return EXECUTION_SUCCESS;}
    if (strcmp(action, "off") == 0) {disable_menu(); return EXECUTION_SUCCESS;}
    if (strcmp(action, "status") != 0) return EX_USAGE;
    printf("Completion menu: %s\n", enabled ? terminal_support < 0 ? "unavailable in this terminal" : "on" : "off");
    return EXECUTION_SUCCESS;
}

EXPORT int binit_complete_builtin_load(char *name)
{
    (void)name;
    /* Parser structs and Readline callbacks cross the host's internal ABI. */
    if (strcmp(dist_version, DISTVERSION) != 0 || rl_readline_version != RL_READLINE_VERSION) {
        builtin_error("native module requires Bash %s and Readline %d.%d", DISTVERSION, RL_VERSION_MAJOR, RL_VERSION_MINOR);
        return 0;
    }
    Dl_info info;
    if (dladdr(&binit_complete_struct, &info) == 0) return 0;
    /* Readline can retain user-created callback bindings after builtin removal. */
    void *handle = dlopen(info.dli_fname, RTLD_NOW | RTLD_NOLOAD | RTLD_NODELETE);
    if (!handle) return 0;
    dlclose(handle);
    return 1;
}

EXPORT void binit_complete_builtin_unload(char *name)
{
    (void)name;
    disable_suggestions();
    remove_poll_hook();
#ifdef BINIT_HIGHLIGHT
    highlighting_enabled = false;
    binit_highlight_clear();
    if (rl_redisplay_function == suggestion_redisplay) rl_redisplay_function = previous_redisplay;
    previous_redisplay = NULL;
#endif
    disable_menu();
}

static char *documentation[] = {
    "Control Binit's completion menu in the current shell.",
    "Bash completion providers supply candidates; Enter inserts, Esc cancels.", NULL
};
EXPORT struct builtin binit_complete_struct = {
    "binit_complete", binit_complete_builtin, BUILTIN_ENABLED,
    documentation, "binit_complete [on|off|wait|status]", NULL
};
