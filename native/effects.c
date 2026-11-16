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

/* Bash exports these trap queries but omits trap.h from loadable headers. */
extern int decode_signal(const char *, int);
extern int signal_is_trapped(int);
extern int any_signals_trapped(void);

static bool plain_context(void)
{
    if (any_signals_trapped() > 0) return false;
    const char *names[] = {"DEBUG", "RETURN", "ERR"};
    for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); ++i) {
        int signal = decode_signal(names[i], 0);
        if (signal < 0 || signal_is_trapped(signal)) return false;
    }
    return true;
}

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

/* Raw lookup is important: even rejecting a dynamic output must not call it. */
static SHELL_VAR *owner_array(const char *output)
{
    if (!simple_name(output, false)) return NULL;
    SHELL_VAR *target = var_lookup(output, shell_variables);
    const unsigned int transformed = (unsigned int)att_uppercase | (unsigned int)att_lowercase | (unsigned int)att_capcase;
    if (!target || !assoc_p(target) || readonly_p(target) || noassign_p(target) ||
        nameref_p(target) || integer_p(target) || ((unsigned int)target->attributes & transformed) ||
        target->assign_func || target->dynamic_value || !assoc_cell(target))
        return NULL;
    return target;
}

static HASH_TABLE *before_names(const char *before_name)
{
    if (!simple_name(before_name, false)) return NULL;
    SHELL_VAR *before = var_lookup(before_name, shell_variables);
    if (!before || !assoc_p(before) || invisible_p(before) || nameref_p(before) || before->dynamic_value || before->assign_func)
        return NULL;
    return assoc_cell(before);
}

static int compare_names(const void *left, const void *right)
{
    return strcmp(*(char *const *)left, *(char *const *)right);
}

/* Names in table that previous lacks, sorted as all_visible_functions and all_aliases order them.
   Checking previous first keeps the walk cheap: almost every name is already there. */
static char **new_names(HASH_TABLE *table, HASH_TABLE *previous, bool functions, size_t *count)
{
    *count = 0;
    if (!table || !table->nentries) return NULL;
    char **names = xmalloc((size_t)table->nentries * sizeof *names);
    for (int bucket = 0; bucket < table->nbuckets; ++bucket)
        for (BUCKET_CONTENTS *entry = table->bucket_array[bucket]; entry; entry = entry->next) {
            if (!entry->data || assoc_reference(previous, entry->key) != NULL) continue;
            const char *name = functions ? ((SHELL_VAR *)entry->data)->name : ((alias_t *)entry->data)->name;
            if (functions && invisible_p((SHELL_VAR *)entry->data)) continue;
            if (tracked(name)) names[(*count)++] = (char *)name;
        }
    qsort(names, *count, sizeof *names, compare_names);
    return names;
}

static void insert_functions(const char *id, HASH_TABLE *previous, SHELL_VAR *target)
{
    HASH_TABLE *owners = assoc_cell(target);
    size_t count;
    char **names = new_names(shell_functions, previous, true, &count);
    const size_t id_length = strlen(id);
    for (size_t i = 0; i < count; ++i) {
        const char *name = names[i];
        if (function_owned(owners, name)) continue;
        SHELL_VAR *function = find_function(name);
        if (!function) continue;
        char *definition = named_function_string((char *)name, function_cell(function), FUNC_MULTILINE | FUNC_EXTERNAL);
        size_t key_size = id_length + strlen(name) + 2;
        char *key = xmalloc(key_size);
        snprintf(key, key_size, "%s:%s", id, name);
        /* assoc_insert owns key and copies the serializer's definition. */
        assoc_insert(owners, key, definition);
        VUNSETATTR(target, att_invisible);
    }
    free(names);
}

static int record_functions(WORD_LIST *args)
{
    if (!args || !args->next || !args->next->next || args->next->next->next)
        return EX_USAGE;
    const char *id = args->word->word;
    SHELL_VAR *target = owner_array(args->next->next->word->word);
    HASH_TABLE *previous = before_names(args->next->word->word);
    if (!valid_id(id) || !target || !previous || previous == assoc_cell(target))
        return EXECUTION_FAILURE;
    /* All validation precedes insertion; existing ownership is never removed. */
    insert_functions(id, previous, target);
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

/* An owned alias keeps its value. Unload compares it with BASH_ALIASES, so later aliases from other
   plugins do not change the ownership signature. */
static void insert_aliases(const char *id, HASH_TABLE *previous, SHELL_VAR *target)
{
    HASH_TABLE *owners = assoc_cell(target);
    size_t count;
    char **names = new_names(aliases, previous, false, &count);
    const size_t id_length = strlen(id);
    for (size_t i = 0; i < count; ++i) {
        const char *name = names[i];
        alias_t *value = find_alias((char *)name);
        if (!value || function_owned(owners, name)) continue;
        size_t key_size = id_length + strlen(name) + 2;
        char *key = xmalloc(key_size);
        snprintf(key, key_size, "%s:%s", id, name);
        /* assoc_insert owns key and copies the value. */
        assoc_insert(owners, key, value->value ? value->value : "");
        VUNSETATTR(target, att_invisible);
    }
    free(names);
}

static int record_aliases(WORD_LIST *args)
{
    if (!args || !args->next || !args->next->next || args->next->next->next) return EX_USAGE;
    if (!plain_context()) return EXECUTION_FAILURE;
    const char *id = args->word->word;
    SHELL_VAR *target = owner_array(args->next->next->word->word);
    HASH_TABLE *previous = before_names(args->next->word->word);
    if (!valid_id(id) || !target || !previous || previous == assoc_cell(target))
        return EXECUTION_FAILURE;
    insert_aliases(id, previous, target);
    return EXECUTION_SUCCESS;
}

/* Entry snapshots of tracked function and alias names for plugin loads. The stack keeps nested loads apart;
   a token is the stack depth after the push, and release drops that entry and anything left above it. */
#define SNAPSHOT_LIMIT 64
static HASH_TABLE *snapshot_functions[SNAPSHOT_LIMIT], *snapshot_aliases[SNAPSHOT_LIMIT];
static int snapshot_depth;

/* Entries share one static marker; hash_flush would otherwise free each item's data. */
static void keep_marker(PTR_T data) {(void)data;}

static void snapshot_drop(int depth)
{
    while (snapshot_depth > depth) {
        --snapshot_depth;
        hash_flush(snapshot_functions[snapshot_depth], keep_marker);
        hash_dispose(snapshot_functions[snapshot_depth]);
        hash_flush(snapshot_aliases[snapshot_depth], keep_marker);
        hash_dispose(snapshot_aliases[snapshot_depth]);
    }
}

static void snapshot_add(HASH_TABLE *table, const char *name)
{
    BUCKET_CONTENTS *item = hash_insert(savestring(name), table, HASH_NOSRCH);
    item->data = (PTR_T)"1";
}

static bool scalar_output(const char *output)
{
    if (!simple_name(output, false)) return false;
    SHELL_VAR *target = find_variable_noref(output);
    const unsigned int transformed = (unsigned int)att_uppercase | (unsigned int)att_lowercase | (unsigned int)att_capcase;
    return target && !readonly_p(target) && !noassign_p(target) && !nameref_p(target) && !array_p(target) &&
        !assoc_p(target) && !integer_p(target) && !((unsigned int)target->attributes & transformed) &&
        !target->assign_func && !target->dynamic_value;
}

static int snapshot_mark(WORD_LIST *args)
{
    if (!args || args->next) return EX_USAGE;
    /* Outside a plain context the caller uses the shell snapshot that trap code can observe. */
    if (!plain_context() || !scalar_output(args->word->word) || snapshot_depth >= SNAPSHOT_LIMIT) return EXECUTION_FAILURE;
    HASH_TABLE *functions = hash_create(128), *names = hash_create(64);
    for (int bucket = 0; shell_functions && bucket < shell_functions->nbuckets; ++bucket)
        for (BUCKET_CONTENTS *entry = shell_functions->bucket_array[bucket]; entry; entry = entry->next) {
            SHELL_VAR *function = (SHELL_VAR *)entry->data;
            if (function && !invisible_p(function) && tracked(function->name)) snapshot_add(functions, function->name);
        }
    for (int bucket = 0; aliases && bucket < aliases->nbuckets; ++bucket)
        for (BUCKET_CONTENTS *entry = aliases->bucket_array[bucket]; entry; entry = entry->next) {
            alias_t *value = (alias_t *)entry->data;
            if (value && tracked(value->name)) snapshot_add(names, value->name);
        }
    snapshot_functions[snapshot_depth] = functions;
    snapshot_aliases[snapshot_depth] = names;
    ++snapshot_depth;
    char token[16];
    snprintf(token, sizeof token, "%d", snapshot_depth);
    if (bind_variable(args->word->word, token, 0)) return EXECUTION_SUCCESS;
    snapshot_drop(snapshot_depth - 1);
    return EXECUTION_FAILURE;
}

static int snapshot_token(const char *text)
{
    char *end = NULL;
    long value = strtol(text, &end, 10);
    return end && *end == '\0' && end != text && value >= 1 && value <= snapshot_depth ? (int)value : 0;
}

/* Records functions and aliases created since the snapshot, then releases it. It needs the plain context of
   record-aliases; without it nothing is recorded and the snapshot stays for names-since. */
static int snapshot_record(WORD_LIST *args)
{
    if (!args || !args->next || !args->next->next || !args->next->next->next || args->next->next->next->next) return EX_USAGE;
    const int token = snapshot_token(args->word->word);
    const char *id = args->next->word->word;
    SHELL_VAR *functions = owner_array(args->next->next->word->word);
    SHELL_VAR *names = owner_array(args->next->next->next->word->word);
    if (!token || !valid_id(id) || !functions || !names || functions == names || !plain_context())
        return EXECUTION_FAILURE;
    insert_functions(id, snapshot_functions[token - 1], functions);
    insert_aliases(id, snapshot_aliases[token - 1], names);
    snapshot_drop(token - 1);
    return EXECUTION_SUCCESS;
}

/* Copies a snapshot's names into an associative array for the shell recorder. */
static int snapshot_names(WORD_LIST *args)
{
    if (!args || !args->next || !args->next->next || args->next->next->next) return EX_USAGE;
    const int token = snapshot_token(args->word->word);
    const char *kind = args->next->word->word;
    SHELL_VAR *target = owner_array(args->next->next->word->word);
    if (!token || !target || (strcmp(kind, "function") != 0 && strcmp(kind, "alias") != 0)) return EXECUTION_FAILURE;
    HASH_TABLE *source = strcmp(kind, "function") == 0 ? snapshot_functions[token - 1] : snapshot_aliases[token - 1];
    assoc_flush(assoc_cell(target));
    for (int bucket = 0; bucket < source->nbuckets; ++bucket)
        for (BUCKET_CONTENTS *entry = source->bucket_array[bucket]; entry; entry = entry->next)
            assoc_insert(assoc_cell(target), savestring(entry->key), "1");
    return EXECUTION_SUCCESS;
}

static int snapshot_release(WORD_LIST *args)
{
    if (!args || args->next) return EX_USAGE;
    const int token = snapshot_token(args->word->word);
    if (!token) return EXECUTION_FAILURE;
    snapshot_drop(token - 1);
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
    if (!plain_context()) return EXECUTION_FAILURE;
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
        const char *inherited = getenv(loader_settings[i]); // NOLINT(concurrency-mt-unsafe): Bash builtins run on the interpreter thread.
        if ((setting && exported_p(setting) && !invisible_p(setting)) || inherited)
            return EXECUTION_FAILURE;
    }
    /* Bash may defer removing TZ from libc's environment after `export -n`.
       Fall back rather than rebuild the environment and evaluate dynamic vars. */
    SHELL_VAR *timezone = var_lookup("TZ", shell_variables);
    if (timezone && (nameref_p(timezone) || array_p(timezone) || assoc_p(timezone) ||
                     timezone->dynamic_value || timezone->assign_func)) return EXECUTION_FAILURE;
    const char *requested_zone = timezone && exported_p(timezone) && !invisible_p(timezone) ? value_cell(timezone) : NULL;
    const char *current_zone = getenv("TZ"); // NOLINT(concurrency-mt-unsafe): Bash builtins run on the interpreter thread.
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
    if (args && !strcmp(args->word->word, "plain")) {
        if (args->next) return EX_USAGE;
        return plain_context() ? EXECUTION_SUCCESS : EXECUTION_FAILURE;
    }
    if (args && !strcmp(args->word->word, "record-functions"))
        return record_functions(args->next);
    if (args && !strcmp(args->word->word, "check-functions"))
        return check_functions(args->next);
    if (args && !strcmp(args->word->word, "record-aliases"))
        return record_aliases(args->next);
    if (args && !strcmp(args->word->word, "mark"))
        return snapshot_mark(args->next);
    if (args && !strcmp(args->word->word, "record-since"))
        return snapshot_record(args->next);
    if (args && !strcmp(args->word->word, "names-since"))
        return snapshot_names(args->next);
    if (args && !strcmp(args->word->word, "release"))
        return snapshot_release(args->next);
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
