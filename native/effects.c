#define _GNU_SOURCE 1
#include <config.h>
#include <stdbool.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
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

static int check_functions(WORD_LIST *args)
{
    if (!args || !args->next || args->next->next) return EX_USAGE;
    const char *id = args->word->word;
    const char *source_name = args->next->word->word;
    if (!valid_id(id) || !simple_name(source_name, false)) return EXECUTION_FAILURE;
    /* Raw lookup rejects namerefs and dynamic arrays without invoking them. */
    SHELL_VAR *source = var_lookup(source_name, shell_variables);
    if (!source || !assoc_p(source) || invisible_p(source) || nameref_p(source) ||
        source->dynamic_value || source->assign_func)
        return EXECUTION_FAILURE;
    HASH_TABLE *owners = assoc_cell(source);
    if (!owners) return EXECUTION_FAILURE;
    const size_t id_length = strlen(id);
    for (int bucket = 0; bucket < owners->nbuckets; ++bucket) {
        for (BUCKET_CONTENTS *entry = owners->bucket_array[bucket]; entry; entry = entry->next) {
            if (strncmp(entry->key, id, id_length) != 0 || entry->key[id_length] != ':') continue;
            const char *name = entry->key + id_length + 1;
            if (!tracked(name) || !entry->data) return EXECUTION_FAILURE;
            SHELL_VAR *function = find_function(name);
            if (!function) continue;
            if (readonly_p(function)) return EXECUTION_FAILURE;
            char *definition = named_function_string((char *)name, function_cell(function),
                                                     FUNC_MULTILINE | FUNC_EXTERNAL);
            if (!definition || strcmp(definition, (char *)entry->data) != 0) return EXECUTION_FAILURE;
        }
    }
    return EXECUTION_SUCCESS;
}

static void print_alias_definition(FILE *stream, const alias_t *value)
{
    char *quoted = sh_single_quote(value->value);
    fprintf(stream, "alias %s%s=%s\n", value->name[0] == '-' ? "-- " : "", value->name, quoted);
    free(quoted);
}

static int record_aliases(WORD_LIST *args)
{
    if (!args || !args->next || !args->next->next || args->next->next->next) return EX_USAGE;
    const char *id = args->word->word;
    const char *before_name = args->next->word->word;
    const char *output = args->next->next->word->word;
    if (!valid_id(id) || !simple_name(before_name, false) || !simple_name(output, false))
        return EXECUTION_FAILURE;
    SHELL_VAR *target = var_lookup(output, shell_variables);
    SHELL_VAR *before = var_lookup(before_name, shell_variables);
    const unsigned int transformed = (unsigned int)att_uppercase | (unsigned int)att_lowercase | (unsigned int)att_capcase;
    if (!target || !assoc_p(target) || readonly_p(target) || noassign_p(target) ||
        nameref_p(target) || integer_p(target) || ((unsigned int)target->attributes & transformed) ||
        target->assign_func || target->dynamic_value || !before || !assoc_p(before) ||
        invisible_p(before) || nameref_p(before) || before->dynamic_value || before->assign_func)
        return EXECUTION_FAILURE;
    HASH_TABLE *previous = assoc_cell(before);
    HASH_TABLE *owners = assoc_cell(target);
    if (!previous || !owners || before == target || previous == owners) return EXECUTION_FAILURE;
    /* Named `alias -p` appends the named definition in 5.2, but not in 5.3. */
    const bool append_named = strcmp(dist_version, "5.2") == 0;
    if (!append_named && strcmp(dist_version, "5.3") != 0) return EXECUTION_FAILURE;
    alias_t **values = all_aliases();
    if (!values) return EXECUTION_SUCCESS;
    char *snapshot = NULL;
    size_t length = 0;
    FILE *stream = open_memstream(&snapshot, &length);
    if (!stream) { free(values); return EXECUTION_FAILURE; }
    for (size_t i = 0; values[i]; ++i) print_alias_definition(stream, values[i]);
    if (fclose(stream) != 0) { free(snapshot); free(values); return EXECUTION_FAILURE; }
    const size_t id_length = strlen(id);
    for (size_t i = 0; values[i]; ++i) {
        const char *name = values[i]->name;
        if (!tracked(name) || assoc_reference(previous, (char *)name) != NULL || function_owned(owners, name))
            continue;
        char *definition = snapshot;
        size_t definition_length = length;
        if (append_named) {
            definition = NULL;
            FILE *named = open_memstream(&definition, &definition_length);
            if (!named) { free(snapshot); free(values); return EXECUTION_FAILURE; }
            fwrite(snapshot, 1, length, named);
            print_alias_definition(named, values[i]);
            if (fclose(named) != 0) { free(definition); free(snapshot); free(values); return EXECUTION_FAILURE; }
        }
        /* Match command substitution's removal of the final output newline. */
        if (definition_length) definition[definition_length - 1] = '\0';
        size_t key_size = id_length + strlen(name) + 2;
        char *key = xmalloc(key_size);
        snprintf(key, key_size, "%s:%s", id, name);
        assoc_insert(owners, key, definition);
        VUNSETATTR(target, att_invisible);
        if (append_named) free(definition);
        else if (length) snapshot[length - 1] = '\n';
    }
    free(snapshot);
    free(values);
    return EXECUTION_SUCCESS;
}

static bool stat_time(char *output, size_t capacity, struct timespec value)
{
    struct tm calendar;
    char zone[32];
    if (!localtime_r(&value.tv_sec, &calendar) || !strftime(zone, sizeof(zone), "%z", &calendar)) return false;
    /* GNU stat pads short years to four columns; libc strftime("%Y") does not. */
    int written = snprintf(output, capacity, "%04lld-%02d-%02d %02d:%02d:%02d.%09ld %s",
                           (long long)calendar.tm_year + 1900, calendar.tm_mon + 1, calendar.tm_mday,
                           calendar.tm_hour, calendar.tm_min, calendar.tm_sec, value.tv_nsec, zone);
    return written >= 0 && (size_t)written < capacity;
}

static int watch_metadata(const char *path, const char *output)
{
    SHELL_VAR *target = var_lookup(output, shell_variables);
    const unsigned int transformed = (unsigned int)att_uppercase | (unsigned int)att_lowercase | (unsigned int)att_capcase;
    if (!target || array_p(target) || assoc_p(target) || readonly_p(target) || noassign_p(target) ||
        nameref_p(target) || integer_p(target) || ((unsigned int)target->attributes & transformed) ||
        target->dynamic_value || target->assign_func) return EXECUTION_FAILURE;
    const char *loader_settings[] = {"LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "LD_DEBUG",
                                     "LD_PROFILE", "LD_TRACE_LOADED_OBJECTS", "LD_SHOW_AUXV", NULL};
    /* An external stat may use a different loader context from this shell. */
    for (size_t i = 0; loader_settings[i]; ++i) {
        SHELL_VAR *setting = var_lookup(loader_settings[i], shell_variables);
        if ((setting && exported_p(setting) && !invisible_p(setting)) || getenv(loader_settings[i]))
            return EXECUTION_FAILURE;
    }
    /* Bash may defer removing TZ from libc's environment after `export -n`.
       Fall back rather than rebuild the environment and evaluate dynamic vars. */
    SHELL_VAR *timezone = var_lookup("TZ", shell_variables);
    if (timezone && (nameref_p(timezone) || array_p(timezone) || assoc_p(timezone) ||
                     timezone->dynamic_value || timezone->assign_func)) return EXECUTION_FAILURE;
    const char *requested_zone = timezone && exported_p(timezone) && !invisible_p(timezone) ? value_cell(timezone) : NULL;
    const char *current_zone = getenv("TZ");
    if ((requested_zone == NULL) != (current_zone == NULL) ||
        (requested_zone && strcmp(requested_zone, current_zone) != 0)) return EXECUTION_FAILURE;
    tzset();
    struct stat data;
    char metadata[384], modified[128], changed[128];
    if (stat(path, &data) != 0)
        return bind_variable(output, "missing", 0) ? EXECUTION_SUCCESS : EXECUTION_FAILURE;
    if (!stat_time(modified, sizeof(modified), data.st_mtim) || !stat_time(changed, sizeof(changed), data.st_ctim))
        return EXECUTION_FAILURE;
    int written = snprintf(metadata, sizeof(metadata), "%ju:%ju:%jd:%s:%s",
                           (uintmax_t)data.st_dev, (uintmax_t)data.st_ino, (intmax_t)data.st_size, modified, changed);
    if (written < 0 || (size_t)written >= sizeof(metadata)) return EXECUTION_FAILURE;
    return bind_variable(output, metadata, 0) ? EXECUTION_SUCCESS : EXECUTION_FAILURE;
}

int binit_effects(WORD_LIST *args)
{
    if (args && !strcmp(args->word->word, "record-functions"))
        return record_functions(args->next);
    if (args && !strcmp(args->word->word, "check-functions"))
        return check_functions(args->next);
    if (args && !strcmp(args->word->word, "record-aliases"))
        return record_aliases(args->next);
    if (!args || !args->next || !args->next->next || args->next->next->next) return EX_USAGE;
    const char *kind = args->word->word;
    const char *name = args->next->word->word;
    const char *output = args->next->next->word->word;
    if (!simple_name(output, false)) return EXECUTION_FAILURE;
    if (!strcmp(kind, "watch")) return watch_metadata(name, output);
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
