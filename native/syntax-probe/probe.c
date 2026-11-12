/* Experimental parser boundary; never loaded by normal Binit initialization. */
#define _GNU_SOURCE 1
#include <config.h>
#include <dlfcn.h>
#include <poll.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "builtins.h"
#include "shell.h"
#include "version.h"
#include "common.h"
#include "execute_cmd.h"
#include "subst.h"
#include "dispose_cmd.h"
#include "unwind_prot.h"
#include "quit.h"
#include <readline/readline.h>

extern int line_number;

typedef void (*observer_fn)(int, int, size_t, int, size_t, void *);
typedef void (*install_fn)(observer_fn, void *);
typedef int (*active_fn)(void);

struct token {
    int kind, start_line, end_line;
    size_t start, end;
};
struct observation {
    size_t count;
    bool overflow;
    bool stress, announced;
    struct token tokens[1024];
};

static install_fn install_observer;
static active_fn observer_active;

static void observe(int kind, int start_line, size_t start, int end_line, size_t end, void *data)
{
    struct observation *result = data;
    if (result->stress && !result->announced) {
        result->announced = true;
        puts("SYNTAX parser suspended");
        fflush(stdout);
        /* A bounded wait avoids losing a signal between the test and wait. */
        while (!interrupt_state && !terminating_signal) poll(NULL, 0, 10);
        QUIT;
    }
    if (result->count == sizeof result->tokens / sizeof result->tokens[0]) {
        result->overflow = true;
        return;
    }
    result->tokens[result->count++] = (struct token){kind, start_line, end_line, start, end};
}

static void clear_observer(void *unused)
{
    (void)unused;
    if (install_observer) install_observer(NULL, NULL);
}

int binit_syntax_probe_builtin(WORD_LIST *arguments)
{
    bool noop = arguments && strcmp(arguments->word->word, "--noop") == 0;
    bool stress = arguments && strcmp(arguments->word->word, "--stress") == 0;
    if (noop || stress) arguments = arguments->next;
    if (!arguments || arguments->next || strlen(arguments->word->word) > 65536) return EX_USAGE;
    char *text = arguments->word->word;
    struct observation result = {.stress = stress};
    int status = last_command_exit_value;
    bool was_active = observer_active && observer_active();
    int origin = line_number;
    COMMAND *command = NULL;
    if (stress && !install_observer) {
        puts("SYNTAX stress entered");
        fflush(stdout);
    }
    if (!noop) {
        for (size_t iteration = 0; iteration < (stress ? 100000u : 1u); ++iteration) {
            result.count = 0;
            begin_unwind_frame("binit-syntax-probe");
            if (install_observer) install_observer(observe, &result);
            add_unwind_protect(clear_observer, NULL);
            command = parse_string_to_command(text, SX_COMPLETE | SX_NOERROR | SX_NOLONGJMP);
            run_unwind_frame("binit-syntax-probe");
            if (stress && command) {dispose_command(command); command = NULL;}
        }
    }
    printf("{\"parsed\":%s,\"observer\":%s,\"observer_was_active\":%s,\"overflow\":%s,\"origin\":%d,\"tokens\":[",
           command ? "true" : "false", install_observer ? "true" : "false", was_active ? "true" : "false", result.overflow ? "true" : "false", origin);
    for (size_t i = 0; i < result.count; ++i) {
        struct token *token = &result.tokens[i];
        printf("%s[%d,%d,%zu,%d,%zu]", i ? "," : "", token->kind,
               token->start_line - origin, token->start, token->end_line - origin, token->end);
    }
    puts("]}");
    if (command) dispose_command(command);
    return status;
}

int binit_syntax_probe_builtin_load(char *name)
{
    (void)name;
    if (strcmp(dist_version, DISTVERSION) != 0 || rl_readline_version != RL_READLINE_VERSION) return 0;
    void *symbol = dlsym(RTLD_DEFAULT, "binit_syntax_observe");
    _Static_assert(sizeof install_observer == sizeof symbol, "POSIX function pointer representation");
    memcpy((void *)&install_observer, (const void *)&symbol, sizeof symbol);
    symbol = dlsym(RTLD_DEFAULT, "binit_syntax_observer_active");
    _Static_assert(sizeof observer_active == sizeof symbol, "POSIX function pointer representation");
    memcpy((void *)&observer_active, (const void *)&symbol, sizeof symbol);
    return 1;
}

static char *documentation[] = {"Experimental parse-only boundary probe; not an editor interface.", NULL};
struct builtin binit_syntax_probe_struct = {
    "binit_syntax_probe", binit_syntax_probe_builtin, BUILTIN_ENABLED,
    documentation, "binit_syntax_probe [--noop|--stress] TEXT", NULL
};
