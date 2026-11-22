#include <config.h>
#include <signal.h>
#include <string.h>
#include <stdio.h>
#include "builtins.h"
#include "variables.h"
#include "execute_cmd.h"
#include "common.h"
#include "shell.h"
#include <readline/readline.h>

static rl_voidfunc_t *previous;
static rl_voidfunc_t *previous_deprep;
static rl_hook_func_t *previous_pre_input;
static int prompts;
static int events;

static int foreign_event(void)
{
    ++events;
    return 0;
}

static int foreign_pre_input(void)
{
    ++prompts;
    return previous_pre_input ? previous_pre_input() : 0;
}

static void foreign_redisplay(void)
{
    if (previous) previous();
}

static void foreign_deprep(void)
{
    if (previous_deprep) previous_deprep();
}

/* Replays a resize's forced update the way Readline 8.3 runs it after a second SIGWINCH's asynchronous
   handler has cleared RL_STATE_SIGHANDLER: _rl_handling_signal still names SIGWINCH, the state bit is gone.
   It runs once, from the event hook, while Readline waits for a key. */
extern int volatile _rl_handling_signal __attribute__((weak));
static rl_hook_func_t *previous_event;

static int clobbered_resize(void)
{
    rl_event_hook = previous_event;
    int rows, columns;
    rl_get_screen_size(&rows, &columns);
    _rl_handling_signal = SIGWINCH;
    rl_readline_state &= ~(unsigned long)RL_STATE_SIGHANDLER;
    rl_set_screen_size(rows, 60);
    rl_forced_update_display();
    _rl_handling_signal = 0;
    return 0;
}

static int test_builtin(WORD_LIST *args)
{
    if (!args || args->next) return EX_USAGE;
    if (!strcmp(args->word->word, "event")) rl_event_hook = foreign_event;
    else if (!strcmp(args->word->word, "event-status")) printf("EVENT_HOOK:%s\n", rl_event_hook ? "set" : "unset");
    else if (!strcmp(args->word->word, "pre-input")) {
        previous_pre_input = rl_pre_input_hook;
        rl_pre_input_hook = foreign_pre_input;
    }
    else if (!strcmp(args->word->word, "pre-input-count")) printf("PRE_INPUT_COUNT:%d\n", prompts);
    else if (!strcmp(args->word->word, "count")) printf("EVENT_COUNT:%d\n", events);
    else if (!strcmp(args->word->word, "null-deprep")) rl_deprep_term_function = NULL;
    else if (!strcmp(args->word->word, "deprep")) {
        previous_deprep = rl_deprep_term_function;
        rl_deprep_term_function = foreign_deprep;
    }
    else if (!strcmp(args->word->word, "deprep-off")) rl_deprep_term_function = previous_deprep;
    else if (!strcmp(args->word->word, "resize-clobbered")) {
        if (!&_rl_handling_signal) {puts("NO_MARKER"); return EXECUTION_SUCCESS;}
        previous_event = rl_event_hook;
        rl_event_hook = clobbered_resize;
        puts("RESIZE_ARMED");
    }
    else if (!strcmp(args->word->word, "on")) {
        previous = rl_redisplay_function;
        rl_redisplay_function = foreign_redisplay;
    } else return EX_USAGE;
    return EXECUTION_SUCCESS;
}

static char *documentation[] = {"Test a foreign redisplay hook.", NULL};
__attribute__((visibility("default"))) struct builtin binit_redisplay_test_struct = {
    "binit_redisplay_test", test_builtin, BUILTIN_ENABLED, documentation,
    "binit_redisplay_test on", NULL
};
