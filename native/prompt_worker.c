#define _GNU_SOURCE 1
#include <config.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <spawn.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>
#include "builtins.h"
#include "general.h"
#include "shell.h"
#include "variables.h"
#include <readline/readline.h>
#include "prompt_worker.h"

#define RESULT_LIMIT 512u
#define ARGUMENT_LIMIT 32u
#define CONTEXT_LIMIT 65536u

static int connection = -1;
static bool cancelled, ready;
static size_t used;
static char data[RESULT_LIMIT + 1u];
static char *directory, *generation;
static struct stat cwd;
static rl_getc_func_t *previous_getc;
static int prompt_getc(FILE *stream);
static int path_connection = -1;
static bool path_cancelled;
static char path_data[25];
static size_t path_used;

static const char *value(const char *name)
{
    const char *text = get_string_value(name);
    return text ? text : "";
}

static void update_input_hook(void)
{
    if ((connection >= 0 && !cancelled) || (path_connection >= 0 && !path_cancelled)) {
        if (rl_getc_function != prompt_getc) {
            previous_getc = rl_getc_function;
            rl_getc_function = prompt_getc;
        }
    } else if (rl_getc_function == prompt_getc) {
        rl_getc_function = previous_getc;
    }
}

void binit_prompt_worker_close(void)
{
    if (connection >= 0) close(connection);
    connection = -1;
    ready = cancelled = false;
    used = 0;
    free(directory);
    free(generation);
    directory = generation = NULL;
    update_input_hook();
}

static void collect(void)
{
    while (connection >= 0) {
        ssize_t count = read(connection, data + used, sizeof(data) - used);
        if (count > 0) {
            used += (size_t)count;
            if (used >= sizeof(data)) binit_prompt_worker_close();
        } else if (count == 0) {
            close(connection);
            connection = -1;
            data[used] = '\0';
            ready = !cancelled && used && !memchr(data, '\0', used);
            update_input_hook();
        } else if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) {
            break;
        } else {
            binit_prompt_worker_close();
        }
    }
}

/* Closing the write half wakes the supervisor. Keep the read half until it
 * exits so another prompt cannot overlap the group it is still reaping. */
void binit_prompt_worker_input(void)
{
    int saved_errno = errno;
    if (connection >= 0 && !cancelled) {
        collect();
        if (connection >= 0) {
            (void)shutdown(connection, SHUT_WR);
            cancelled = true;
        }
    }
    if (path_connection >= 0 && !path_cancelled) {
        (void)shutdown(path_connection, SHUT_WR);
        path_cancelled = true;
    }
    update_input_hook();
    errno = saved_errno;
}

static int prompt_getc(FILE *stream)
{
    int byte = previous_getc(stream);
    binit_prompt_worker_input();
    return byte;
}

static bool append_size(size_t *total, const char *word)
{
    size_t length = strnlen(word, CONTEXT_LIMIT) + 1;
    if (length > CONTEXT_LIMIT - *total) return false;
    *total += length;
    return true;
}

static bool allowed(const char *entry)
{
    return strncmp(entry, "LD_", 3) != 0 && strncmp(entry, "DYLD_", 5) != 0 &&
        strncmp(entry, "GLIBC_TUNABLES=", 15) != 0 && strncmp(entry, "GCONV_PATH=", 11) != 0 &&
        strncmp(entry, "BASH_FUNC_", 10) != 0;
}

static int launch(char *const arguments[], char *const environment[])
{
    int sockets[2];
    if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, sockets) < 0) return -1;
    int parent = sockets[0], child = sockets[1];
    if (parent < 3 || child < 3 || fcntl(parent, F_SETFL, O_NONBLOCK) < 0) goto fail;
    posix_spawn_file_actions_t files;
    if (posix_spawn_file_actions_init(&files) != 0) goto fail;
    int error = posix_spawn_file_actions_adddup2(&files, child, STDIN_FILENO);
    if (!error) error = posix_spawn_file_actions_adddup2(&files, child, STDOUT_FILENO);
    if (!error) error = posix_spawn_file_actions_addopen(&files, STDERR_FILENO, "/dev/null", O_WRONLY, 0);
    if (!error) error = posix_spawn_file_actions_addclosefrom_np(&files, 3);
    posix_spawnattr_t attributes;
    if (!error) error = posix_spawnattr_init(&attributes);
    if (!error) {
        sigset_t mask, defaults;
        sigemptyset(&mask);
        sigemptyset(&defaults);
        sigaddset(&defaults, SIGCHLD);
        error = posix_spawnattr_setflags(&attributes, POSIX_SPAWN_SETPGROUP |
                                         POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF);
        if (!error) error = posix_spawnattr_setsigmask(&attributes, &mask);
        if (!error) error = posix_spawnattr_setsigdefault(&attributes, &defaults);
        pid_t pid;
        if (!error) error = posix_spawn(&pid, arguments[0], &files, &attributes, arguments, environment);
        posix_spawnattr_destroy(&attributes);
    }
    posix_spawn_file_actions_destroy(&files);
    if (error) goto fail;
    close(child);
    return parent;
fail:
    close(parent);
    close(child);
    return -1;
}

void binit_path_worker_close(void)
{
    if (path_connection >= 0) close(path_connection);
    path_connection = -1;
    path_used = 0;
    path_cancelled = false;
    update_input_hook();
}

int binit_path_worker_start(const char *manager, const char *request, size_t size)
{
    if (!manager || manager[0] != '/' || path_connection >= 0 || !rl_getc_function)
        return -1;
    char *argv[] = {(char *)manager, "__highlight-worker", NULL};
    char *empty_environment[] = {NULL};
    path_connection = launch(argv, empty_environment);
    if (path_connection < 0) return -1;
    if (send(path_connection, request, size, MSG_NOSIGNAL) != (ssize_t)size) {
        binit_path_worker_close();
        return -1;
    }
    update_input_hook();
    return 0;
}

/* Returns 0 while the supervisor is still running, or -1 for an unusable result. */
int binit_path_worker_take(char *result, size_t size)
{
    if (path_connection < 0 || size >= sizeof(path_data)) return -1;
    for (;;) {
        ssize_t count = read(path_connection, path_data + path_used, sizeof(path_data) - path_used);
        if (count > 0) {
            path_used += (size_t)count;
            if (path_used >= sizeof(path_data)) break;
        } else if (count == 0) {
            bool valid = !path_cancelled && path_used == size;
            if (valid) memcpy(result, path_data, size);
            binit_path_worker_close();
            return valid ? 1 : -1;
        } else if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) {
            return 0;
        } else {
            break;
        }
    }
    binit_path_worker_close();
    return -1;
}

#define STOP_WAIT_MS 50

static struct timespec deadline_after(long milliseconds)
{
    struct timespec deadline;
    clock_gettime(CLOCK_MONOTONIC, &deadline);
    deadline.tv_sec += milliseconds / 1000;
    deadline.tv_nsec += (milliseconds % 1000) * 1000000L;
    if (deadline.tv_nsec >= 1000000000L) {
        deadline.tv_sec += 1;
        deadline.tv_nsec -= 1000000000L;
    }
    return deadline;
}

static long milliseconds_left(const struct timespec *deadline)
{
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (deadline->tv_sec - now.tv_sec) * 1000L + (deadline->tv_nsec - now.tv_nsec) / 1000000L;
}

/* A prompt hook runs before Readline prepares the terminal, so it is still in canonical mode: a key
   stays unreadable until a newline, and the line discipline consumes editing keys such as Ctrl-R. While
   waiting, the terminal is held without canonical input or echo, so the first key ends the wait and
   reaches Readline unchanged. Signal and flow-control keys keep their terminal meaning, as they do
   while a command runs. */
static bool hold_input(int input, struct termios *saved)
{
    if (input < 0 || !isatty(input) || tcgetattr(input, saved) != 0 || !(saved->c_lflag & ICANON)) return false;
    struct termios held = *saved;
    held.c_lflag &= ~(tcflag_t)(ICANON | ECHO);
    held.c_cc[VMIN] = 1;
    held.c_cc[VTIME] = 0;
    return tcsetattr(input, TCSANOW, &held) == 0;
}

/* Polls the request until it ends or the deadline passes. Terminal input ends the wait when
   stop_on_input is set, and a signal always ends it. */
static void await_connection(const struct timespec *deadline, bool stop_on_input)
{
    collect();
    if (connection < 0) return;
    int input = fileno(rl_instream ? rl_instream : stdin);
    struct termios saved;
    bool held = hold_input(input, &saved);
    while (connection >= 0 && !interrupt_state && !terminating_signal) {
        long left = milliseconds_left(deadline);
        if (left <= 0) break;
        struct pollfd watched[2] = {{connection, POLLIN, 0}, {input, POLLIN, 0}};
        if (poll(watched, stop_on_input ? 2 : 1, (int)left) < 0 && errno != EINTR) break;
        if (stop_on_input && watched[1].revents) break;
        if (watched[0].revents) collect();
        if (stop_on_input && cancelled) break;
    }
    // The saved settings come back without flushing, so keys typed meanwhile stay queued for Readline.
    if (held) (void)tcsetattr(input, TCSANOW, &saved);
}

static int start(WORD_LIST *arguments)
{
    if (!interactive_shell || !rl_getc_function || !arguments || !arguments->next ||
        arguments->word->word[0] != '/' || arguments->next->word->word[0] != '/') return EXECUTION_FAILURE;
    collect();
    // A request cancelled moments ago, as by a quickly typed command, is still stopping its group.
    if (connection >= 0 && cancelled) {
        struct timespec deadline = deadline_after(STOP_WAIT_MS);
        await_connection(&deadline, false);
    }
    if (connection >= 0 || ready) return EXECUTION_FAILURE;
    binit_prompt_worker_close();
    char *argv[ARGUMENT_LIMIT + 1u] = {arguments->word->word, "__prompt-worker"};
    size_t count = 2, size = 0;
    if (!append_size(&size, argv[0])) return EXECUTION_FAILURE;
    for (WORD_LIST *word = arguments->next; word; word = word->next) {
        if (count == ARGUMENT_LIMIT || !append_size(&size, word->word->word)) return EXECUTION_FAILURE;
        argv[count++] = word->word->word;
    }
    maybe_make_export_env();
    count = 0;
    for (char **entry = export_env; entry && *entry; ++entry) {
        if (!allowed(*entry)) continue;
        if (!append_size(&size, *entry)) return EXECUTION_FAILURE;
        ++count;
    }
    char **environment = (char **)calloc(count + 1u, sizeof(*environment));
    if (!environment) return EXECUTION_FAILURE;
    size_t index = 0;
    for (char **entry = export_env; entry && *entry; ++entry) {
        if (!allowed(*entry)) continue;
        if (index >= count) {
            free((void *)environment);
            return EXECUTION_FAILURE;
        }
        environment[index++] = *entry;
    }
    directory = strdup(value("PWD"));
    generation = strdup(value("__binit_generation_dir"));
    if (directory && generation && stat(".", &cwd) == 0) connection = launch(argv, environment);
    free((void *)environment);
    if (connection < 0) {
        binit_prompt_worker_close();
        return EXECUTION_FAILURE;
    }
    update_input_hook();
    return EXECUTION_SUCCESS;
}

/* Waits up to the given milliseconds for the running worker to finish. Pending input or a signal ends
 * the wait early. The worker keeps running on timeout so a later prompt can still take its result. */
static int await(const char *text)
{
    char *end = NULL;
    errno = 0;
    long milliseconds = strtol(text, &end, 10);
    if (!*text || *end || errno || strspn(text, "0123456789") != strlen(text) || milliseconds > 1000) return EX_USAGE;
    struct timespec deadline = deadline_after(milliseconds);
    if (!cancelled) await_connection(&deadline, true);
    return ready ? EXECUTION_SUCCESS : EXECUTION_FAILURE;
}

static int take(const char *name)
{
    SHELL_VAR *target = legal_identifier(name) ? find_variable_noref(name) : NULL;
    if (!target || readonly_p(target) || noassign_p(target) || nameref_p(target) ||
        integer_p(target) || array_p(target) || assoc_p(target) || specialvar_p(target)) return EXECUTION_FAILURE;
    binit_prompt_worker_input();
    collect();
    if (connection >= 0) return EXECUTION_FAILURE;
    int status = EXECUTION_FAILURE;
    struct stat current;
    if (ready && strcmp(value("PWD"), directory) == 0 &&
        strcmp(value("__binit_generation_dir"), generation) == 0 &&
        stat(".", &current) == 0 && current.st_dev == cwd.st_dev && current.st_ino == cwd.st_ino &&
        bind_variable(name, data, 0)) status = EXECUTION_SUCCESS;
    binit_prompt_worker_close();
    return status;
}

int binit_prompt_worker(WORD_LIST *arguments)
{
    if (!arguments) return EX_USAGE;
    const char *action = arguments->word->word;
    WORD_LIST *options = arguments->next;
    if (strcmp(action, "available") == 0 && !options) return EXECUTION_SUCCESS;
    if (strcmp(action, "start") == 0) return start(options);
    if (strcmp(action, "take") == 0 && options && !options->next) return take(options->word->word);
    if (strcmp(action, "wait") == 0 && options && !options->next) return await(options->word->word);
    return EX_USAGE;
}
