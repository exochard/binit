#define _GNU_SOURCE 1
#include <config.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include "builtins.h"
#include "variables.h"
#include "common.h"
#include "shell.h"
#include "assoc.h"
#include "alias.h"
#include "externs.h"
#include "xmalloc.h"
#include "effects.h"

static bool letter(unsigned char c)
{
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_';
}

static bool simple_name(const char *name, bool function)
{
    if (!name || !letter((unsigned char)*name)) return false;
    for (const unsigned char *p = (const unsigned char *)name + 1; *p; ++p)
        if (!letter(*p) && (*p < '0' || *p > '9') && !(function && *p == '-')) return false;
    return true;
}

static bool tracked(const char *name)
{
    return simple_name(name, true) && strncmp(name, "__binit_", 8) != 0 &&
        strcmp(name, "binit") != 0 && strcmp(name, "binit_on") != 0 && strcmp(name, "binit_segment") != 0;
}

int binit_effects(WORD_LIST *args)
{
    if (!args || !args->next || !args->next->next || args->next->next->next) return EX_USAGE;
    const char *kind = args->word->word;
    const char *name = args->next->word->word;
    const char *output = args->next->next->word->word;
    if (!simple_name(output, false)) return EXECUTION_FAILURE;
    SHELL_VAR *target = find_variable_noref(output);
    if (!target || readonly_p(target) || noassign_p(target) || nameref_p(target) ||
        target->assign_func || target->dynamic_value) return EXECUTION_FAILURE;
    if (!strcmp(kind, "function")) {
        const unsigned int transformed = (unsigned int)att_uppercase | (unsigned int)att_lowercase | (unsigned int)att_capcase;
        if (array_p(target) || assoc_p(target) || integer_p(target) ||
            ((unsigned int)target->attributes & transformed)) return EXECUTION_FAILURE;
        SHELL_VAR *function = find_function(name);
        if (!function) return EXECUTION_FAILURE;
        /* Bash's serializer handles here-documents and function redirections. */
        char *definition = named_function_string((char *)name, function_cell(function), FUNC_MULTILINE | FUNC_EXTERNAL);
        return bind_variable(output, definition, 0) ? EXECUTION_SUCCESS : EXECUTION_FAILURE;
    }
    if (strcmp(kind, "names") != 0 || !assoc_p(target)) return EX_USAGE;
    if (strcmp(name, "function") != 0 && strcmp(name, "alias") != 0) return EX_USAGE;
    assoc_flush(assoc_cell(target));
    if (!strcmp(name, "function")) {
        SHELL_VAR **functions = all_visible_functions();
        if (functions) {
            for (size_t i = 0; functions[i]; ++i)
                if (tracked(functions[i]->name)) assoc_insert(assoc_cell(target), savestring(functions[i]->name), "1");
            free((void *)functions);
        }
    } else {
        alias_t **values = all_aliases();
        if (values) {
            for (size_t i = 0; values[i]; ++i)
                if (tracked(values[i]->name)) assoc_insert(assoc_cell(target), savestring(values[i]->name), "1");
            free((void *)values);
        }
    }
    return EXECUTION_SUCCESS;
}
