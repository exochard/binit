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
#include <termios.h>
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
#include <readline/keymaps.h>
#include "menu.h"
#include "effects.h"
#include "layout.h"
#include "worker.h"
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
    bool transient_input, undo, captured, painted, resized, oversized, bare_escape, interrupted, escape_wait;
    int row, column, rows, columns, height, cells, cell_width;
    int input[INPUT_LIMIT];
    size_t input_count, input_index;
    long input_deadline;
    char sequence[32];
    size_t sequence_length;
    struct binit_menu menu;
    bool filtering, history;
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
static bool history_enabled, history_owns_suggestions, history_dismissed, history_live;
static bool history_painted, colors_painted;
static bool history_deprep_active, history_deprep_running, history_deprep_detached;
static rl_voidfunc_t *history_previous_deprep, *history_base_deprep;
static char history_line[4097];
static struct binding history_tabs[2], history_searches[2], word_keys[2];
static bool keys_enabled;
static size_t history_matches;
static bool poll_installed;
static bool poll_running;
static long last_poll;
static rl_hook_func_t *previous_event_hook;
static bool suggestions_enabled;
static bool highlighting_enabled;
static bool suggestion_visible, ghost_on_blank_row;
static int ghost_column;
static bool redisplay_running;
static rl_voidfunc_t *previous_redisplay;
static int option(const char *);
static size_t history_panel(const char **, const struct binit_layout *, int, int, bool);
static void history_draw(const char **, size_t, int, int *);
static void history_clear(void);
static int history_tab(int, int);
static int binit_history_search(int, int);
static void completion_cleanup(void *);

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

static const char *history_suggestion(void)
{
    if (!suggestions_enabled || active || !interactive_shell || executing_builtin || running_trap || parse_and_execute_level || get_current_prompt_level() != 1 ||
        rl_point != rl_end || rl_end <= 0 || rl_end > 4096 || !rl_line_buffer) return NULL;
    if (binit_cells(rl_line_buffer, (size_t)rl_end) < 0) return NULL;
    HIST_ENTRY **entries = history_list();
    if (!entries) return NULL;
    int lower = history_length > 1000 ? history_length - 1000 : 0;
    for (int i = history_length - 1; i >= lower; --i) {
        const char *line = entries[i]->line;
        if (!line || strncmp(line, rl_line_buffer, (size_t)rl_end) != 0 || !line[rl_end]) continue;
        const char *suffix = line + rl_end;
        size_t size = strnlen(suffix, 1025);
        if (size > 1024 || binit_cells(suffix, size) < 0) return NULL;
        return suffix;
    }
    return NULL;
}

static int mode_cells(void)
{
    if (!option("show-mode-in-prompt") || rl_display_prompt != rl_prompt) return 0;
    const char *name = rl_editing_mode == 1 /* Readline's emacs mode */ ? "emacs-mode-string" :
                       rl_get_keymap() == vi_insertion_keymap ? "vi-ins-mode-string" : "vi-cmd-mode-string";
    const char *text = rl_variable_value(name);
    return text ? binit_prompt_cells(text) : 0;
}

/* Where Readline has drawn the line, or false when the overlays cannot be placed with confidence. */
static bool current_layout(struct binit_layout *layout, int *rows, int *columns)
{
    const char *up = rl_get_termcap("up");
    if (!rl_line_buffer || !rl_instream || !up || !*up) return false;
    rl_get_screen_size(rows, columns); /* Readline wraps at its own idea of the width */
    int prompt = binit_prompt_cells(rl_display_prompt ? rl_display_prompt : ""), mode = mode_cells();
    if (prompt < 0 || mode < 0) return false;
    // Readline marks an edited history line with '*' before the prompt.
    if (option("mark-modified-lines") && current_history() && rl_undo_list) ++prompt;
    if (!binit_layout(prompt + mode, *columns, rl_line_buffer, (size_t)rl_end, (size_t)rl_point, layout) || layout->rows > *rows - 1)
        return false;
    // In horizontal-scroll mode Readline scrolls a long line instead of wrapping it.
    return !option("horizontal-scroll-mode") || (layout->rows == 1 && layout->end.column < *columns - 1);
}

/* The overlays are drawn after Readline's redisplay and only ever move relative to Readline's cursor:
   an absolute row from an earlier cursor query is wrong after the screen scrolls or reflows. */
static void move_to(int *row, int to_row, int to_column)
{
    if (to_row < *row) fprintf(rl_outstream, "\033[%dA", *row - to_row);
    else if (to_row > *row) fprintf(rl_outstream, "\033[%dB", to_row - *row);
    *row = to_row;
    fprintf(rl_outstream, "\033[%dG", to_column + 1);
}

#ifdef BINIT_HIGHLIGHT
/* The input is rewritten as one run that the terminal wraps, as Readline wrote it: moving between
   rows would end the terminal's soft wrap, and a copied command would then contain line breaks. */
static bool paint_colors(const struct binit_layout *layout, int columns, int *row)
{
    uint8_t colors[4096];
    if (alias_context_unknown(rl_line_buffer)) memset(colors, 0, (size_t)rl_end);
    else if (!binit_highlight(rl_line_buffer, (size_t)rl_end, colors)) return false;
    static const char *styles[] = {"0", "2", "32", "36", "33", "35"};
    move_to(row, 0, layout->column[0]);
    unsigned int color = 0;
    for (int r = 0; r < layout->rows; ++r) {
        size_t end = r + 1 < layout->rows ? layout->offset[r + 1] : (size_t)rl_end;
        for (size_t i = layout->offset[r]; i < end; ++i) {
            if (colors[i] != color) {color = colors[i]; fprintf(rl_outstream, "\033[%sm", styles[color]);}
            fputc((unsigned char)rl_line_buffer[i], rl_outstream);
        }
        if (r + 1 == layout->rows || end == (size_t)rl_end) break;
        // Readline pads a row with spaces when a wide character moves to the next one.
        int used = layout->column[r] + binit_cells(rl_line_buffer + layout->offset[r], end - layout->offset[r]);
        if (used < columns && color) {fputs("\033[0m", rl_outstream); color = 0;}
        for (; used < columns; ++used) fputc(' ', rl_outstream);
    }
    fputs("\033[0m", rl_outstream);
    // Text that ends at the right margin leaves the cursor on the last cell of the full row.
    bool full = layout->rows > 1 && layout->end.column == 0 && layout->offset[layout->rows - 1] == (size_t)rl_end;
    *row = full ? layout->rows - 2 : layout->rows - 1;
    return true;
}
#endif

/* Writes the suggestion after the input without using the last column, so it never wraps. */
static bool draw_ghost(const char *suffix, int available)
{
    size_t length = strnlen(suffix, 1025);
    int used;
    size_t size = binit_fit(suffix, length, available, &used);
    bool ellipsis = size < length && available > 0 && binit_cells("\xe2\x80\xa6", 3) == 1;
    if (ellipsis) size = binit_fit(suffix, length, available - 1, &used);
    if (!size && !ellipsis) return false;
    fprintf(rl_outstream, "\033[2m%.*s%s\033[0m", (int)size, suffix, ellipsis ? "\xe2\x80\xa6" : "");
    return true;
}

/* Back to a plain Readline line when the overlays can no longer be placed.  A forced update would
   print the first lines of a multi-line prompt again. */
static void reset_display(void)
{
    rl_clear_visible_line();
    fputs("\033[J", rl_outstream);
    rl_on_new_line();
    rl_redisplay();
    colors_painted = suggestion_visible = history_painted = false;
}

/* With a redisplay hook installed Readline changes two behaviors: a TERM assignment re-initializes it
   without terminal capabilities, and a resize gets a forced update from wherever the cursor is. */
static void prepare_redisplay(void)
{
    static char reinitialized[256];
    const char *term = get_string_value("TERM"), *up = rl_get_termcap("up");
    if ((!up || !*up) && term && *term && strcmp(term, "dumb") != 0 && strlen(term) < sizeof reinitialized &&
        strcmp(term, reinitialized) != 0) {
        snprintf(reinitialized, sizeof reinitialized, "%s", term);
        rl_voidfunc_t *custom = rl_redisplay_function;
        rl_redisplay_function = rl_redisplay;
        rl_reset_terminal(NULL);
        rl_redisplay_function = custom;
        up = rl_get_termcap("up");
        // Only cache failed resets: a later TERM assignment can drop capabilities again.
        if (up && *up) reinitialized[0] = '\0';
    }
    // Readline forces the update only when its screen size changed; an interrupt also redisplays from the
    // signal handler (clearing a message) and must not be treated as a resize.
    static int seen_rows, seen_columns;
    int rows, columns;
    rl_get_screen_size(&rows, &columns);
    bool resized = seen_columns && (rows != seen_rows || columns != seen_columns);
    seen_rows = rows;
    seen_columns = columns;
    if (!resized || !RL_ISSTATE(RL_STATE_SIGHANDLER) || !rl_outstream) return;
    // Return to the start of the prompt and clear below, as Readline's own resize handling does, so the
    // forced update replaces the old lines.  Terminals reflow wrapped input, so the new width places it.
    struct binit_layout layout;
    int lines = current_layout(&layout, &rows, &columns) ? layout.cursor.row : 0;
    for (const char *p = rl_display_prompt; p && *p; ++p) lines += *p == '\n';
    if (lines > 0) fprintf(rl_outstream, "\033[%dA", lines);
    fputs("\r\033[J", rl_outstream);
    colors_painted = suggestion_visible = history_painted = false;
}

static void suggestion_redisplay(void)
{
    if (redisplay_running) {rl_redisplay(); return;}
    prepare_redisplay();
    redisplay_running = true;
    if (previous_redisplay && previous_redisplay != suggestion_redisplay) previous_redisplay();
    else rl_redisplay();
    redisplay_running = false;
    if (!interactive_shell || executing_builtin || running_trap || parse_and_execute_level || get_current_prompt_level() != 1 || !rl_outstream) return;
    struct binit_layout layout;
    int rows, columns;
    if (!current_layout(&layout, &rows, &columns)) {
        history_matches = 0;
        if (colors_painted || suggestion_visible || history_painted) reset_display();
        return;
    }
    bool searching = RL_ISSTATE(RL_STATE_ISEARCH | RL_STATE_NSEARCH);
    const char *items[5] = {0};
    const char *suffix = searching ? NULL : history_suggestion();
    size_t matches = history_panel(items, &layout, rows, columns, searching);
    bool colors = false;
#ifdef BINIT_HIGHLIGHT
    colors = highlighting_enabled && !searching && rl_end > 0;
#endif
    if (!rl_end) colors_painted = false;
    if (!colors && !suffix && !matches && !suggestion_visible && !history_painted) return;
    int row = layout.cursor.row;
#ifdef BINIT_HIGHLIGHT
    if (colors && paint_colors(&layout, columns, &row)) colors_painted = true;
#endif
    move_to(&row, layout.end.row, layout.end.column);
    fputs(history_painted && !matches ? "\033[J" : "\033[K", rl_outstream);
    suggestion_visible = suffix && draw_ghost(suffix, columns - layout.end.column - 1);
    ghost_column = layout.end.column;
    ghost_on_blank_row = layout.end.column == 0 && layout.end.row > 0;
    if (matches) history_draw(items, matches, columns, &row);
    else history_painted = false;
    move_to(&row, layout.cursor.row, layout.cursor.column);
    fflush(rl_outstream);
}

/* Inserts the whole suggestion when one is shown at the end of the input; otherwise the key keeps its meaning. */
static int accept_or(rl_command_func_t *fallback, int count, int key)
{
    const char *suffix = suggestion_visible ? history_suggestion() : NULL;
    if (suffix) return rl_insert_text(suffix) < 0 ? 1 : 0;
    return fallback(count, key);
}

static int accept_or_forward_char(int count, int key) {return accept_or(rl_forward_char, count, key);}
static int accept_or_end_of_line(int count, int key) {return accept_or(rl_end_of_line, count, key);}
static int accept_or_self_insert(int count, int key) {return accept_or(rl_insert, count, key);}

#define EMACS_KEYMAP 1u
#define VI_KEYMAP 2u
static const struct {
    const char *sequence;
    rl_command_func_t *original, *wrapper;
    unsigned int keymaps;
} accept_keys[] = {
    {"\006", rl_forward_char, accept_or_forward_char, EMACS_KEYMAP | VI_KEYMAP},
    {"\033[C", rl_forward_char, accept_or_forward_char, EMACS_KEYMAP | VI_KEYMAP},
    {"\033OC", rl_forward_char, accept_or_forward_char, EMACS_KEYMAP | VI_KEYMAP},
    {"\033[F", rl_end_of_line, accept_or_end_of_line, EMACS_KEYMAP | VI_KEYMAP},
    {"\033OF", rl_end_of_line, accept_or_end_of_line, EMACS_KEYMAP | VI_KEYMAP},
    {"\033[4~", rl_end_of_line, accept_or_end_of_line, EMACS_KEYMAP | VI_KEYMAP},
    {"\033[8~", rl_end_of_line, accept_or_end_of_line, EMACS_KEYMAP | VI_KEYMAP},
    {"\005", rl_end_of_line, accept_or_end_of_line, EMACS_KEYMAP},
    /* Stock vi-insert binds Ctrl-F to self-insert. */
    {"\006", rl_insert, accept_or_self_insert, VI_KEYMAP},
};
static const char *const accept_keymaps[] = {"emacs-standard", "vi-insert"};
static bool accept_owned[2][sizeof accept_keys / sizeof *accept_keys];

static rl_command_func_t *bound_function(Keymap map, const char *sequence)
{
    int type;
    rl_command_func_t *function = rl_function_of_keyseq_len(sequence, strlen(sequence), map, &type);
    return type == ISFUNC ? function : NULL;
}

static void deprep_install(void);
static void deprep_release(void);

static void disable_suggestions(void)
{
    suggestions_enabled = false;
    suggestion_visible = false;
    if (!highlighting_enabled && !history_enabled) {
        if (rl_redisplay_function == suggestion_redisplay) rl_redisplay_function = previous_redisplay;
        previous_redisplay = NULL;
    }
    for (size_t i = 0; i < 2; ++i) {
        Keymap map = rl_get_keymap_by_name(accept_keymaps[i]);
        for (size_t j = 0; j < sizeof accept_keys / sizeof *accept_keys; ++j) {
            if (!accept_owned[i][j]) continue;
            accept_owned[i][j] = false;
            if (map && bound_function(map, accept_keys[j].sequence) == accept_keys[j].wrapper)
                rl_bind_keyseq_in_map(accept_keys[j].sequence, accept_keys[j].original, map);
        }
    }
    deprep_release();
}

static int enable_suggestions(void)
{
    if (suggestions_enabled) return EXECUTION_SUCCESS;
    const char *term = get_string_value("TERM");
    if ((rl_readline_state & RL_STATE_INITIALIZED) == 0 || !term || strcmp(term, "dumb") == 0) return EXECUTION_FAILURE;
    for (size_t i = 0; i < 2; ++i) {
        Keymap map = rl_get_keymap_by_name(accept_keymaps[i]);
        if (!map) continue;
        for (size_t j = 0; j < sizeof accept_keys / sizeof *accept_keys; ++j) {
            if (!(accept_keys[j].keymaps & (1u << i)) || bound_function(map, accept_keys[j].sequence) != accept_keys[j].original) continue;
            rl_bind_keyseq_in_map(accept_keys[j].sequence, accept_keys[j].wrapper, map);
            accept_owned[i][j] = true;
        }
    }
    if (!highlighting_enabled && !history_enabled) {
        previous_redisplay = rl_redisplay_function;
        rl_redisplay_function = suggestion_redisplay;
    }
    suggestions_enabled = true;
    deprep_install();
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

/* How long a bare Esc waits for the rest of a key sequence, like tmux's escape-time. Terminals write
 * a whole sequence at once, so the wait only matters for split transports; BINIT_ESCAPE_MS widens it. */
static int escape_time(void)
{
    const char *value = get_string_value("BINIT_ESCAPE_MS");
    intmax_t milliseconds_value;
    if (value && *value && legal_number(value, &milliseconds_value) && milliseconds_value >= 0 && milliseconds_value <= 1000)
        return (int)milliseconds_value;
    return 10;
}

static int timed_getc(FILE *stream)
{
    struct pollfd fd = {.fd = fileno(stream), .events = POLLIN};
    int timeout = active && active->escape_wait ? escape_time() : 50;
    if (active && active->input_deadline) {
        long now = milliseconds();
        if (!now || now >= active->input_deadline) return READERR;
        long remaining = active->input_deadline - now;
        if (remaining < timeout) timeout = (int)remaining;
    }
    int ready = poll(&fd, 1, timeout);
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
    long now = milliseconds();
    if (!now) return false;
    ctx->input_deadline = now + 120;
    fputs("\033[6n", rl_outstream);
    fflush(rl_outstream);
    bool valid = false;
    while ((now = milliseconds()) > 0 && now < ctx->input_deadline && ctx->input_count + length < INPUT_LIMIT - 8) {
        int key = read_timed(ctx);
        if (key < 0) {
            if (key == READERR && !ctx->interrupted && !interrupt_state && !terminating_signal) continue;
            break;
        }
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
    ctx->input_deadline = 0;
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
        snprintf(heading, sizeof heading, "%s/%s  %zu/%zu", ctx->history ? "History SEARCH " : "", query, selected, ctx->menu.count);
    } else snprintf(heading, sizeof heading, "%s %zu/%zu", ctx->history ? "History NAVIGATE" : "Completions", selected, ctx->menu.count);
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
            int width = binit_text(text, sizeof text, ctx->history ? value : label(value), ctx->cell_width - 3);
            bool chosen = item == ctx->menu.selected;
            fprintf(rl_outstream, "%s%s%s", chosen ? "\033[7m" : "", chosen ? "> " : "  ", text);
            for (int i = width + 2; i < ctx->cell_width; ++i) fputc(' ', rl_outstream);
            if (chosen) fputs("\033[0m", rl_outstream);
        }
    }
    const char *hint = ctx->filtering ? "Type to filter  Backspace erase  Enter insert  Esc cancel" :
                       "Tab next  arrows/hjkl move  / filter  Enter insert  Esc cancel";
    if (ctx->history) hint = ctx->filtering ?
        "Type to search  Backspace erase  Enter insert  Esc navigate" :
        "j/k select  / search  Enter insert  Esc close";
    if (ctx->columns < 65) hint = "Tab move  / filter  Enter insert  Esc cancel";
    if (ctx->history && ctx->columns < 65) hint = ctx->filtering ?
        "Type search  Enter insert  Esc navigate" : "j/k select  / search  Enter insert  Esc close";
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
        if (ctx->resized || !(ctx->history ? history_enabled : enabled)) return 27;
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
        // Only the byte after Esc is ambiguous; later sequence bytes keep the normal wait.
        ctx->escape_wait = i == 1;
        int next = input_key(ctx, true);
        ctx->escape_wait = false;
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
    if (rl_redisplay_function == suggestion_redisplay) suggestion_redisplay();
    else rl_redisplay();
    terminal_size(&ctx->rows, &ctx->columns);
    if (ctx->menu.total == 0 || ctx->rows < 4 || ctx->columns < 20 ||
        !cursor_position(ctx, &ctx->row, &ctx->column) ||
        ctx->row > ctx->rows || ctx->column > ctx->columns) {
        if (!ctx->history) terminal_support = -1;
        return -1;
    }
    if (!ctx->history) terminal_support = 1;
    char text[2048];
    int longest = 0;
    for (size_t i = 0; i < ctx->menu.total; ++i) {
        int width = binit_text(text, sizeof text, label(ctx->menu.values[i]), 32);
        if (width > longest) longest = width;
    }
    int cell = ctx->history ? ctx->columns - 2 : longest + 3;
    if (cell < 12) cell = 12;
    ctx->cells = (ctx->columns - 2) / cell;
    if (ctx->cells < 1) ctx->cells = 1;
    if (ctx->cells > 4) ctx->cells = 4;
    if ((size_t)ctx->cells > ctx->menu.total) ctx->cells = (int)ctx->menu.total;
    if (ctx->cells < 1) return -1;
    ctx->cell_width = (ctx->columns - 2) / ctx->cells < cell ? (ctx->columns - 2) / ctx->cells : cell;
    int rows = (int)((ctx->menu.total + (size_t)ctx->cells - 1) / (size_t)ctx->cells);
    if (rows > (ctx->history ? 5 : 6)) rows = ctx->history ? 5 : 6;
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
    while (ctx->history ? history_enabled : enabled) {
        paint_menu(ctx);
        int key = read_key(ctx);
        if (ctx->resized) break;
        if (key == '\r' || key == '\n') {
            choice = (int)binit_menu_choice(&ctx->menu);
            if (choice) break;
            continue;
        }
        if (key == 27 && ctx->history && ctx->filtering) {ctx->filtering = false; continue;}
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
        else if (key >= 32 && key <= 255 && key != 127 && (ctx->filtering || !ctx->history)) {
            ctx->filtering = true;
            binit_menu_append(&ctx->menu, (unsigned char)key);
        } else if (ctx->history && key >= 32 && key <= 255 && key != 127) {
            continue;
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

static size_t history_collect(const char **items, size_t limit, const char *query)
{
    HIST_ENTRY **entries = history_list();
    if (!entries) return 0;
    int lower = history_length > 1000 ? history_length - 1000 : 0;
    uint64_t hashes[1000];
    if (limit > sizeof hashes / sizeof *hashes) limit = sizeof hashes / sizeof *hashes;
    size_t count = 0;
    for (int i = history_length - 1; i >= lower && count < limit; --i) {
        const char *line = entries[i]->line;
        if (!line || !*line) continue;
        size_t length = strnlen(line, 4097);
        if (length > 4096 || !strcasestr(line, query) || binit_cells(line, length) < 0) continue;
        uint64_t hash = UINT64_C(14695981039346656037);
        for (size_t j = 0; j < length; ++j) hash = (hash ^ (unsigned char)line[j]) * UINT64_C(1099511628211);
        bool duplicate = false;
        for (size_t j = 0; j < count; ++j) if (hashes[j] == hash && !strcmp(items[j], line)) {duplicate = true; break;}
        if (!duplicate) {hashes[count] = hash; items[count++] = line;}
    }
    return count;
}

/* Erases the panel and the suggestion below and after the input, leaving the cursor where it was. */
static void history_clear(void)
{
    if (!history_painted || !rl_outstream) return;
    struct binit_layout layout;
    int rows, columns;
    if (!RL_ISSTATE(RL_STATE_DONE) && current_layout(&layout, &rows, &columns)) {
        int row = layout.cursor.row;
        move_to(&row, layout.end.row, layout.end.column);
        fputs("\033[J", rl_outstream);
        move_to(&row, layout.cursor.row, layout.cursor.column);
        fflush(rl_outstream);
        suggestion_visible = false;
    }
    history_painted = false;
}

/* Number of history rows to show below the input; 0 leaves Tab to completion. */
static size_t history_panel(const char **items, const struct binit_layout *layout, int rows, int columns, bool searching)
{
    history_matches = 0;
    if (!history_enabled || !history_live || active || searching || !rl_instream || !isatty(fileno(rl_outstream)) ||
        !isatty(fileno(rl_instream)) || rl_executing_macro) return 0;
    bool can_focus = false;
    for (size_t i = 0; i < 2; ++i) {
        Keymap map = history_tabs[i].map;
        if (history_tabs[i].owned && map == rl_get_keymap() && map['\t'].type == ISFUNC && map['\t'].function == history_tab)
            can_focus = true;
    }
    if (!can_focus) return 0;
    if (strcmp(history_line, rl_line_buffer) != 0) {
        memcpy(history_line, rl_line_buffer, (size_t)rl_end + 1);
        history_dismissed = false;
    }
    if (!rl_end || history_dismissed || rows < 5 || columns < 20) return 0;
    size_t count = history_collect(items, 5, rl_line_buffer);
    if (layout->end.row + 2 + (int)count > rows) return 0;
    history_matches = count;
    return count;
}

/* Draws below the input; \r\n scrolls the screen when the input is on the bottom row. */
static void history_draw(const char **items, size_t count, int columns, int *row)
{
    char text[4097];
    binit_text(text, sizeof text, "History  Tab focus  Ctrl-R search", columns - 1);
    fprintf(rl_outstream, "\r\n\033[2K\033[2m%s\033[0m", text);
    for (size_t i = 0; i < count; ++i) {
        binit_text(text, sizeof text, items[i], columns - 3);
        fprintf(rl_outstream, "\r\n\033[2K  %s", text);
    }
    if (history_painted) fputs("\033[J", rl_outstream);
    *row += (int)count + 1;
    history_painted = true;
}

static int history_pick(bool search)
{
    if (!history_enabled || !interactive_shell || executing_builtin || running_trap || parse_and_execute_level ||
        get_current_prompt_level() != 1 || active || !rl_line_buffer || rl_end > 4096) return 0;
    const char *items[1000];
    size_t count = history_collect(items, 1000, search ? "" : rl_line_buffer);
    if (!count) return 0;
    history_clear();
    struct context *ctx = calloc(1, sizeof *ctx);
    if (!ctx) return 0;
    ctx->line = strdup(rl_line_buffer);
    if (!ctx->line || binit_menu_init(&ctx->menu, items, count) != 0) {free(ctx->line); free(ctx); return 0;}
    ctx->point = rl_point;
    ctx->mark = rl_mark;
    ctx->display = rl_completion_display_matches_hook;
    ctx->last = rl_last_func;
    ctx->event = rl_event_hook;
    ctx->query_items = rl_completion_query_items;
    ctx->show_all = option("show-all-if-ambiguous");
    ctx->prefix_first = option("menu-complete-display-prefix");
    ctx->history = true;
    ctx->filtering = search;
    active = ctx;
    begin_unwind_frame("binit-history");
    add_unwind_protect(completion_cleanup, ctx);
    int selected = picker(ctx);
    if (selected < 0) replay_input(ctx, NULL, 0);
    rl_point = ctx->point;
    rl_mark = ctx->mark;
    if (selected > 0 && (size_t)selected <= count) {
        rl_replace_line(items[(size_t)selected - 1], 0);
        rl_point = rl_end;
    }
    history_dismissed = true;
    snprintf(history_line, sizeof history_line, "%s", rl_line_buffer);
    run_unwind_frame("binit-history");
    rl_redisplay();
    return selected < 0 ? 0 : 1;
}

static int delegate_binding(struct binding *owned, size_t count, int n, int key, rl_command_func_t *fallback)
{
    for (size_t i = 0; i < count; ++i)
        if (owned[i].owned && owned[i].map == rl_get_keymap() && owned[i].previous)
            return owned[i].previous(n, key);
    return fallback(n, key);
}

static int history_tab(int n, int key)
{
    if (!history_dismissed && history_matches && history_pick(false)) return 0;
    return delegate_binding(history_tabs, 2, n, key, rl_complete);
}

static int binit_history_search(int n, int key)
{
    if (history_pick(true)) return 0;
    return delegate_binding(history_searches, 2, n, key, rl_reverse_search_history);
}

/* Runs after Readline has left the input: with RL_STATE_DONE the cursor is at column 0 of the row below
   the last input row (on the blank wrap row itself when the input ends at the right margin); after ^C
   it is on the last input row, where the ghost starts. */
static void history_deprep(void)
{
    if (history_deprep_running) {
        if (history_base_deprep && history_base_deprep != history_deprep) history_base_deprep();
        return;
    }
    history_deprep_running = true;
    if (rl_outstream) {
        if (RL_ISSTATE(RL_STATE_SIGHANDLER)) {
            // After ^C the column is unknown, so the one place that saves and restores the cursor is here.
            if (suggestion_visible) fputs("\033[J", rl_outstream);
            else if (history_painted) fputs("\0337\r\n\033[J\0338", rl_outstream);
        } else if (RL_ISSTATE(RL_STATE_DONE)) {
            if (suggestion_visible && !ghost_on_blank_row) fprintf(rl_outstream, "\033[A\033[%dG\033[K\033[B\r", ghost_column + 1);
            if (history_painted || (suggestion_visible && ghost_on_blank_row)) fputs("\033[J", rl_outstream);
        }
        fflush(rl_outstream);
    }
    colors_painted = suggestion_visible = history_painted = false;
    history_dismissed = false;
    history_line[0] = '\0';
    if (history_previous_deprep && history_previous_deprep != history_deprep) history_previous_deprep();
    history_deprep_running = false;
}

static void deprep_install(void)
{
    if (history_deprep_active) return;
    if (!history_deprep_detached) history_base_deprep = rl_deprep_term_function;
    // A foreign hook that unwound itself can leave this hook installed while it was detached.
    history_previous_deprep = rl_deprep_term_function == history_deprep ? history_base_deprep : rl_deprep_term_function;
    rl_deprep_term_function = history_deprep;
    history_deprep_detached = false;
    history_deprep_active = true;
}

static void deprep_release(void)
{
    if (!history_deprep_active || suggestions_enabled || history_enabled) return;
    if (rl_deprep_term_function == history_deprep) {
        rl_deprep_term_function = history_previous_deprep;
        history_previous_deprep = NULL;
        history_deprep_detached = false;
    } else {
        history_deprep_detached = true;
    }
    history_deprep_active = false;
}

static void history_bind(struct binding *binding, Keymap map, int key, rl_command_func_t *current, rl_command_func_t *replacement)
{
    *binding = (struct binding){map, current, true};
    rl_bind_key_in_map(key, replacement, map);
}

static void history_unbind(struct binding *binding, int key, rl_command_func_t *replacement)
{
    if (binding->owned && binding->map[key].type == ISFUNC && binding->map[key].function == replacement)
        rl_bind_key_in_map(key, binding->previous, binding->map);
    binding->owned = false;
}

static void disable_history(void);

static int enable_history(bool live)
{
    if (history_enabled && history_live == live) return EXECUTION_SUCCESS;
    if (history_enabled) disable_history();
    if (!suggestions_enabled) {
        if (enable_suggestions() != EXECUTION_SUCCESS) return EXECUTION_FAILURE;
        history_owns_suggestions = true;
    }
    const char *names[] = {"emacs-standard", "vi-insert"};
    for (size_t i = 0; i < 2; ++i) {
        Keymap map = rl_get_keymap_by_name(names[i]);
        if (!map) continue;
        rl_command_func_t *tab = map['\t'].function;
        if (live && map['\t'].type == ISFUNC && (tab == native_complete || tab == rl_complete || tab == rl_menu_complete || tab == rl_backward_menu_complete))
            history_bind(&history_tabs[i], map, '\t', tab, history_tab);
        if (map[18].type == ISFUNC && map[18].function == rl_reverse_search_history)
            history_bind(&history_searches[i], map, 18, map[18].function, binit_history_search);
    }
    history_enabled = true;
    history_live = live;
    return EXECUTION_SUCCESS;
}

static void disable_history(void)
{
    history_clear();
    history_enabled = false;
    history_live = false;
    history_matches = 0;
    history_line[0] = '\0';
    for (size_t i = 0; i < 2; ++i) {
        history_unbind(&history_tabs[i], '\t', history_tab);
        history_unbind(&history_searches[i], 18, binit_history_search);
    }
    if (history_owns_suggestions) {history_owns_suggestions = false; disable_suggestions();}
    else if (!suggestions_enabled && !highlighting_enabled) {
        if (rl_redisplay_function == suggestion_redisplay) rl_redisplay_function = previous_redisplay;
        previous_redisplay = NULL;
    }
    deprep_release();
}

/* Kitty and several other terminals send Ctrl-H for Ctrl-Backspace. Binit takes Ctrl-H while it still deletes
   one character and the terminal's erase key is known to be another byte, so Backspace keeps its meaning. */
static int enable_keys(void)
{
    if (keys_enabled) return EXECUTION_SUCCESS;
    const char *term = get_string_value("TERM");
    if ((rl_readline_state & RL_STATE_INITIALIZED) == 0 || !term || strcmp(term, "dumb") == 0) return EXECUTION_FAILURE;
    struct termios settings;
    bool erase_known_other = rl_instream && tcgetattr(fileno(rl_instream), &settings) == 0 && settings.c_cc[VERASE] != '\b';
    const char *names[] = {"emacs-standard", "vi-insert"};
    for (size_t i = 0; i < 2 && erase_known_other; ++i) {
        Keymap map = rl_get_keymap_by_name(names[i]);
        if (map && map['\b'].type == ISFUNC && map['\b'].function == rl_rubout)
            history_bind(&word_keys[i], map, '\b', rl_rubout, rl_backward_kill_word);
    }
    keys_enabled = true;
    return EXECUTION_SUCCESS;
}

static void disable_keys(void)
{
    for (size_t i = 0; i < 2; ++i) history_unbind(&word_keys[i], '\b', rl_backward_kill_word);
    keys_enabled = false;
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
        } else if (binding->owned && history_tabs[i].owned && history_tabs[i].map == binding->map &&
                   history_tabs[i].previous == native_complete) {
            history_tabs[i].previous = binding->previous;
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
        bool wrapped = previous == history_tab && history_tabs[i].owned && history_tabs[i].map == map;
        if (wrapped) previous = history_tabs[i].previous;
        if (previous != rl_complete && previous != rl_menu_complete && previous != rl_backward_menu_complete && previous != native_complete) continue;
        if (previous != native_complete) bindings[i] = (struct binding){map, previous, true};
        if (wrapped) history_tabs[i].previous = native_complete;
        else rl_bind_key_in_map('\t', native_complete, map);
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
    if (strcmp(action, "request") == 0) return binit_worker_request(arguments->next);
    if (strcmp(action, "effects") == 0) return binit_effects(arguments->next);
    if (strcmp(action, "editor") == 0) {
        WORD_LIST *feature = arguments->next;
        WORD_LIST *setting = feature ? feature->next : NULL;
        if (feature && !setting && strcmp(feature->word->word, "status") == 0) {
            printf("Native editor: available\n");
            printf("Completion menu: %s\n", enabled ? terminal_support < 0 ? "unavailable in this terminal" : "on" : "off");
            printf("Suggestions: %s\n", suggestions_enabled ? "on" : "off");
            printf("History: %s\n", history_enabled ? history_live ? "live" : "search" : "off");
#ifdef BINIT_HIGHLIGHT
            printf("Highlighting: %s\n", highlighting_enabled ? "on" : "off");
#else
            printf("Highlighting: unavailable\n");
#endif
            Keymap map = rl_get_keymap_by_name(rl_editing_mode == 1 ? "emacs-standard" : "vi-insert");
            if (map) {
                rl_command_func_t *tab = bound_function(map, "\t");
                printf("Tab: %s\n", tab == native_complete || tab == rl_complete || tab == rl_menu_complete || tab == rl_backward_menu_complete ?
                       "complete" : tab == history_tab ? "history" : "custom binding");
                if (suggestions_enabled && bound_function(map, "\033[C") == accept_or_forward_char)
                    printf("Right: accept suggestion\n");
                rl_command_func_t *forward = bound_function(map, "\006");
                if (suggestions_enabled && (forward == accept_or_forward_char || forward == accept_or_self_insert))
                    printf("Ctrl-F: accept suggestion\n");
                if (history_enabled && bound_function(map, "\022") == binit_history_search)
                    printf("Ctrl-R: history search\n");
                for (size_t i = 0; i < 2; ++i)
                    if (word_keys[i].owned && word_keys[i].map == map && bound_function(map, "\b") == rl_backward_kill_word)
                        printf("Ctrl-Backspace: delete word\n");
            }
            return EXECUTION_SUCCESS;
        }
        if (!feature || !setting || setting->next) return EX_USAGE;
        if (strcmp(feature->word->word, "history") == 0) {
            if (strcmp(setting->word->word, "on") == 0) {
                const char *mode = get_string_value("BINIT_HISTORY_MODE");
                if (mode && strcmp(mode, "search") != 0 && strcmp(mode, "live") != 0) {
                    builtin_error("BINIT_HISTORY_MODE must be search or live");
                    return EXECUTION_FAILURE;
                }
                return enable_history(mode && strcmp(mode, "live") == 0);
            }
            if (strcmp(setting->word->word, "live") == 0) return enable_history(true);
            if (strcmp(setting->word->word, "off") == 0) {disable_history(); return EXECUTION_SUCCESS;}
            return EX_USAGE;
        }
        if (strcmp(feature->word->word, "keys") == 0) {
            if (strcmp(setting->word->word, "on") == 0) return enable_keys();
            if (strcmp(setting->word->word, "off") == 0) {disable_keys(); return EXECUTION_SUCCESS;}
            return EX_USAGE;
        }
        if (strcmp(feature->word->word, "suggestions") == 0) {
            if (strcmp(setting->word->word, "on") == 0) {history_owns_suggestions = false; return enable_suggestions();}
            if (strcmp(setting->word->word, "off") == 0) {history_owns_suggestions = false; disable_suggestions(); return EXECUTION_SUCCESS;}
        }
        if (strcmp(feature->word->word, "highlighting") == 0) {
#ifdef BINIT_HIGHLIGHT
            if (strcmp(setting->word->word, "on") == 0) {
                const char *term = get_string_value("TERM");
                if ((rl_readline_state & RL_STATE_INITIALIZED) == 0 || !term || strcmp(term, "dumb") == 0) return EXECUTION_FAILURE;
                if (!highlighting_enabled && !suggestions_enabled && !history_enabled) {previous_redisplay = rl_redisplay_function; rl_redisplay_function = suggestion_redisplay;}
                highlighting_enabled = true;
                return EXECUTION_SUCCESS;
            }
            if (strcmp(setting->word->word, "off") == 0) {
                highlighting_enabled = false;
                binit_highlight_clear();
                if (!suggestions_enabled && !history_enabled) {if (rl_redisplay_function == suggestion_redisplay) rl_redisplay_function = previous_redisplay; previous_redisplay = NULL;}
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
    disable_keys();
    disable_history();
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
