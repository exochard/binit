#define _GNU_SOURCE 1
#include <config.h>
#include <stdbool.h>
#include <stdio.h>
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

static bool valid_id(const char *id)
{
    if (!id || strlen(id) > 64 || !((*id >= 'a' && *id <= 'z') || (*id >= 'A' && *id <= 'Z'))) return false;
    for (const unsigned char *p = (const unsigned char *)id; *p; ++p)
        if (!letter(*p) && (*p < '0' || *p > '9') && *p != '-')
            return false;
    return true;
}

static bool function_owned(HASH_TABLE *owners, const char *name)
{
    for (int bucket = 0; bucket < owners->nbuckets; ++bucket) {
        for (BUCKET_CONTENTS *entry = owners->bucket_array[bucket]; entry; entry = entry->next) {
            const char *suffix = strchr(entry->key, ':');
            if (strcmp(suffix ? suffix + 1 : entry->key, name) == 0) return true;
        }
    }
    return false;
}

static int record_functions(WORD_LIST *args)
{
    if (!args || !args->next || !args->next->next || args->next->next->next)
        return EX_USAGE;
    const char *id = args->word->word;
    const char *before_name = args->next->word->word;
    const char *output = args->next->next->word->word;
    if (!valid_id(id) || !simple_name(before_name, false) || !simple_name(output, false))
        return EXECUTION_FAILURE;

    /* Raw lookup is important: even rejecting a dynamic output must not call it. */
    SHELL_VAR *target = var_lookup(output, shell_variables);
    const unsigned int transformed = (unsigned int)att_uppercase | (unsigned int)att_lowercase | (unsigned int)att_capcase;
    if (!target || !assoc_p(target) || readonly_p(target) || noassign_p(target) ||
        nameref_p(target) || integer_p(target) || ((unsigned int)target->attributes & transformed) ||
        target->assign_func || target->dynamic_value)
        return EXECUTION_FAILURE;
    SHELL_VAR *before = var_lookup(before_name, shell_variables);
    if (!before || !assoc_p(before) || invisible_p(before) || nameref_p(before) || before->dynamic_value || before->assign_func)
        return EXECUTION_FAILURE;
    HASH_TABLE *previous = assoc_cell(before);
    HASH_TABLE *owners = assoc_cell(target);
    if (!previous || !owners || before == target || previous == owners)
        return EXECUTION_FAILURE;

    /* All validation precedes insertion; existing ownership is never removed. */
    SHELL_VAR **functions = all_visible_functions();
    if (functions) {
        const size_t id_length = strlen(id);
        for (size_t i = 0; functions[i]; ++i) {
            const char *name = functions[i]->name;
            /* Bash 5.2 leaves the read-only lookup key unqualified. */
            if (!tracked(name) || assoc_reference(previous, (char *)name) != NULL ||
                function_owned(owners, name))
                continue;
            char *definition = named_function_string((char *)name, function_cell(functions[i]),
                                                     FUNC_MULTILINE | FUNC_EXTERNAL);
            size_t key_size = id_length + strlen(name) + 2;
            char *key = xmalloc(key_size);
            snprintf(key, key_size, "%s:%s", id, name);
            /* assoc_insert owns key and copies the serializer's definition. */
            assoc_insert(owners, key, definition);
            VUNSETATTR(target, att_invisible);
        }
        free((void *)functions);
    }
    return EXECUTION_SUCCESS;
}

int binit_effects(WORD_LIST *args)
{
    if (args && !strcmp(args->word->word, "record-functions"))
        return record_functions(args->next);
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
