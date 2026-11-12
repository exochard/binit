#include <config.h>
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
static int events;

static int foreign_event(void)
{
    ++events;
    return 0;
}

static void foreign_redisplay(void)
{
    if (previous) previous();
}

static void foreign_deprep(void)
{
    if (previous_deprep) previous_deprep();
}

static int test_builtin(WORD_LIST *args)
{
    if (!args || args->next) return EX_USAGE;
    if (!strcmp(args->word->word, "event")) rl_event_hook = foreign_event;
    else if (!strcmp(args->word->word, "count")) printf("EVENT_COUNT:%d\n", events);
    else if (!strcmp(args->word->word, "null-deprep")) rl_deprep_term_function = NULL;
    else if (!strcmp(args->word->word, "deprep")) {
        previous_deprep = rl_deprep_term_function;
        rl_deprep_term_function = foreign_deprep;
    }
    else if (!strcmp(args->word->word, "deprep-off")) rl_deprep_term_function = previous_deprep;
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
