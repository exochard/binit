#include <config.h>
#include <signal.h>
#include <string.h>
#include <stdio.h>
#include <sys/ioctl.h>
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

/* Replay the forced update after a second SIGWINCH clears the handler state.
   Readline 8.3 has a marker for the first signal. On 8.2, deliver the second
   signal so its handler leaves SIGWINCH pending while Readline waits for input. */
extern int volatile _rl_handling_signal __attribute__((weak));
static rl_hook_func_t *previous_event;
static int force_resize;

static int clobbered_resize(void)
{
    rl_event_hook = previous_event;
    int rows, columns;
    rl_get_screen_size(&rows, &columns);
    if (force_resize && &_rl_handling_signal) _rl_handling_signal = SIGWINCH;
    else {
        struct winsize size = {.ws_row = (unsigned short)rows, .ws_col = 60};
        if (ioctl(fileno(rl_instream), TIOCSWINSZ, &size) != 0 || rl_pending_signal() != SIGWINCH)
            return 0;
    }
    rl_readline_state &= ~(unsigned long)RL_STATE_SIGHANDLER;
    rl_set_screen_size(rows, 60);
    if (force_resize) rl_forced_update_display();
    else (*rl_redisplay_function)();
    // The title marks the completed update without changing the prompt or cursor.
    fputs("\033]2;resize replay complete\007", rl_outstream);
    fflush(rl_outstream);
    if (force_resize && &_rl_handling_signal) _rl_handling_signal = 0;
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
    else if (!strcmp(args->word->word, "resize-clobbered") || !strcmp(args->word->word, "resize-pending")) {
        force_resize = !strcmp(args->word->word, "resize-clobbered");
        previous_event = rl_event_hook;
        rl_event_hook = clobbered_resize;
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
