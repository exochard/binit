#define _GNU_SOURCE 1
#include <config.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <spawn.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include "builtins.h"
#include "shell.h"
#include "variables.h"
#include "array.h"
#include "unwind_prot.h"
#include "quit.h"
#include <readline/readline.h>
#include "worker.h"

#define OUTPUT_LIMIT ((size_t)1024 * 1024)
#define CONTEXT_LIMIT 65536u
#define CANDIDATE_LIMIT 4096u
#define TIMEOUT_MS 2000

struct request {
    pid_t pid;
    int input, output;
    sigset_t previous_mask;
    char **arguments, **environment;
    char *data, *line, *generation, *context;
    size_t context_size;
};

static bool busy;

static int64_t milliseconds(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return -1;
    return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static void cleanup(void *opaque)
{
    struct request *request = opaque;
    if (request->pid > 0) {
        (void)kill(request->pid, SIGKILL);
        while (waitpid(request->pid, NULL, 0) < 0 && errno == EINTR) {}
    }
    if (request->input >= 0) close(request->input);
    if (request->output >= 0) close(request->output);
    free((void *)request->arguments);
    free((void *)request->environment);
    free(request->data);
    free(request->line);
    free(request->generation);
    free(request->context);
    busy = false;
    (void)pthread_sigmask(SIG_SETMASK, &request->previous_mask, NULL);
}

static bool excluded_environment(const char *value)
{
    return strncmp(value, "LD_", 3) == 0 || strncmp(value, "DYLD_", 5) == 0 ||
        strncmp(value, "GLIBC_TUNABLES=", 15) == 0 || strncmp(value, "GCONV_PATH=", 11) == 0 ||
        strncmp(value, "COMP_", 5) == 0;
}

static bool context_field(struct request *request, const char *value)
{
    if (!value) value = "";
    size_t length = strlen(value) + 1;
    if (length > CONTEXT_LIMIT || request->context_size > CONTEXT_LIMIT - length) return false;
    memcpy(request->context + request->context_size, value, length);
    request->context_size += length;
    return true;
}

static bool completion_context(struct request *request)
{
    SHELL_VAR *variable = find_variable_noref("COMP_WORDS");
    if (!variable || !array_p(variable)) return false;
    ARRAY *words = array_cell(variable);
    arrayind_t count = array_num_elements(words);
    if (count < 0 || count > CONTEXT_LIMIT || (count && array_max_index(words) != count - 1)) return false;
    request->context = malloc(CONTEXT_LIMIT);
    if (!request->context || !context_field(request, "binit-completion-v1")) return false;
    const char *names[] = {"COMP_LINE", "COMP_POINT", "COMP_CWORD", "COMP_TYPE", "COMP_KEY", "COMP_WORDBREAKS"};
    for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); ++i)
        if (!context_field(request, get_string_value(names[i]))) return false;
    char number[32];
    snprintf(number, sizeof(number), "%jd", (intmax_t)count);
    if (!context_field(request, number)) return false;
    for (arrayind_t i = 0; i < count; ++i) {
        const char *word = array_reference(words, i);
        if (!word || !context_field(request, word)) return false;
    }
    return true;
}

static bool pending_input(void)
{
    struct pollfd input = {.fd = fileno(rl_instream), .events = POLLIN};
    int ready = poll(&input, 1, 0);
    return rl_pending_input || rl_executing_macro || ready < 0 || (ready > 0 && input.revents);
}

static bool same_context(const struct request *request, int point, const struct stat *directory)
{
    struct stat current;
    const char *generation = get_string_value("__binit_generation_dir");
    if (!generation) generation = "";
    return rl_point == point && strcmp(rl_line_buffer, request->line) == 0 &&
        strcmp(generation, request->generation) == 0 && stat(".", &current) == 0 &&
        directory->st_dev == current.st_dev && directory->st_ino == current.st_ino && !pending_input();
}

int binit_worker_request(WORD_LIST *arguments)
{
    if (busy || !arguments || !arguments->next || arguments->word->word[0] != '/' ||
        arguments->next->word->word[0] != '/' || !rl_instream || !rl_line_buffer ||
        !(rl_readline_state & RL_STATE_COMPLETING) || !isatty(fileno(rl_instream))) return EXECUTION_FAILURE;
    SHELL_VAR *replies = find_variable_noref("COMPREPLY");
    if (!replies || !array_p(replies) || readonly_p(replies) || noassign_p(replies) || integer_p(replies))
        return EXECUTION_FAILURE;
    array_flush(array_cell(replies));
    if (pending_input()) return EXECUTION_FAILURE;
    struct request request = {.pid = 0, .input = -1, .output = -1};
    sigset_t blocked;
    sigemptyset(&blocked);
    sigaddset(&blocked, SIGCHLD);
    if (pthread_sigmask(SIG_BLOCK, &blocked, &request.previous_mask) != 0) return EXECUTION_FAILURE;
    busy = true;
    begin_unwind_frame("binit-worker");
    add_unwind_protect(cleanup, &request);
    int result = EXECUTION_FAILURE;
    int64_t started = milliseconds();
    struct stat directory;
    int point = rl_point;
    if (started < 0 || stat(".", &directory) != 0 || strlen(rl_line_buffer) > CONTEXT_LIMIT) goto done;
    request.line = strdup(rl_line_buffer);
    const char *generation = get_string_value("__binit_generation_dir");
    if (generation && strlen(generation) > CONTEXT_LIMIT - strlen(rl_line_buffer)) goto done;
    request.generation = strdup(generation ? generation : "");
    request.data = malloc(OUTPUT_LIMIT + 1u);
    if (!request.line || !request.generation || !request.data) goto done;
    size_t count = 0, context = strlen(request.line) + strlen(request.generation);
    if (!completion_context(&request) || context > CONTEXT_LIMIT - request.context_size) goto done;
    context += request.context_size;
    for (WORD_LIST *word = arguments; word; word = word->next) {
        size_t length = strlen(word->word->word) + 1;
        if (length > CONTEXT_LIMIT || context > CONTEXT_LIMIT - length || ++count > CANDIDATE_LIMIT) goto done;
        context += length;
    }
    request.arguments = (char **)calloc(count + 3, sizeof(char *));
    if (!request.arguments) goto done;
    char parent[32];
    snprintf(parent, sizeof(parent), "%ld", (long)getpid());
    request.arguments[0] = arguments->word->word;
    request.arguments[1] = "__completion-worker";
    request.arguments[2] = parent;
    size_t index = 3;
    for (WORD_LIST *word = arguments->next; word; word = word->next)
        request.arguments[index++] = word->word->word;
    maybe_make_export_env();
    count = 0;
    for (char **entry = export_env; entry && *entry; ++entry) {
        if (excluded_environment(*entry)) continue;
        size_t length = strlen(*entry) + 1;
        if (length > CONTEXT_LIMIT || context > CONTEXT_LIMIT - length) goto done;
        context += length;
        ++count;
    }
    request.environment = (char **)calloc(count + 1, sizeof(char *));
    if (!request.environment) goto done;
    index = 0;
    for (char **entry = export_env; entry && *entry; ++entry) {
        if (!excluded_environment(*entry)) {
            if (index >= count) goto done;
            request.environment[index++] = *entry;
        }
    }
    int pipe_fds[2];
    if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, pipe_fds) != 0) goto done;
    request.input = pipe_fds[0];
    request.output = pipe_fds[1];
    int *descriptors[] = {&request.input, &request.output};
    for (size_t i = 0; i < 2; ++i) {
        if (*descriptors[i] < 3) {
            int moved = fcntl(*descriptors[i], F_DUPFD_CLOEXEC, 3);
            if (moved < 0) goto done;
            close(*descriptors[i]);
            *descriptors[i] = moved;
        }
    }
    if (fcntl(request.input, F_SETFL, O_NONBLOCK) != 0) goto done;
    posix_spawnattr_t attributes;
    posix_spawn_file_actions_t files;
    if (posix_spawnattr_init(&attributes) != 0) goto done;
    if (posix_spawn_file_actions_init(&files) != 0) {
        posix_spawnattr_destroy(&attributes);
        goto done;
    }
    int error = posix_spawnattr_setflags(&attributes, POSIX_SPAWN_SETSIGMASK);
    if (!error) error = posix_spawnattr_setsigmask(&attributes, &request.previous_mask);
    if (!error) error = posix_spawn_file_actions_adddup2(&files, request.output, STDIN_FILENO);
    if (!error) error = posix_spawn_file_actions_adddup2(&files, request.output, STDOUT_FILENO);
    if (!error) error = posix_spawn_file_actions_addopen(&files, STDERR_FILENO, "/dev/null", O_WRONLY, 0);
    if (!error) error = posix_spawn_file_actions_addclosefrom_np(&files, 3);
    pid_t child = 0;
    if (!error) error = posix_spawn(&child, request.arguments[0], &files, &attributes,
                                   request.arguments, request.environment);
    posix_spawn_file_actions_destroy(&files);
    posix_spawnattr_destroy(&attributes);
    if (error) goto done;
    request.pid = child;
    close(request.output);
    request.output = -1;
    size_t used = 0, sent = 0;
    bool eof = false, successful = false;
    while (!eof || request.pid > 0) {
        QUIT;
        int64_t now = milliseconds();
        if (now < 0 || now - started >= TIMEOUT_MS || pending_input()) goto done;
        struct pollfd events[] = {{.fd = fileno(rl_instream), .events = POLLIN},
                                 {.fd = request.input, .events = (short)(POLLIN | (sent < request.context_size ? POLLOUT : 0))}};
        int ready = poll(events, 2, 1);
        if (ready < 0) goto done;
        if (events[0].revents) goto done;
        unsigned returned = (unsigned short)events[1].revents;
        if (request.input >= 0 && sent < request.context_size && (returned & POLLOUT)) {
            ssize_t bytes = send(request.input, request.context + sent, request.context_size - sent, MSG_NOSIGNAL);
            if (bytes > 0) {
                sent += (size_t)bytes;
                if (sent == request.context_size && shutdown(request.input, SHUT_WR) != 0) goto done;
            } else if (bytes < 0 && errno != EAGAIN && errno != EINTR) goto done;
        }
        const unsigned readable = (unsigned)POLLIN | (unsigned)POLLHUP | (unsigned)POLLERR | (unsigned)POLLNVAL;
        if (request.input >= 0 && !eof && (returned & readable)) {
            ssize_t bytes = read(request.input, request.data + used, OUTPUT_LIMIT + 1u - used);
            if (bytes > 0) {
                used += (size_t)bytes;
                if (used > OUTPUT_LIMIT) goto done;
            } else if (bytes == 0) {
                close(request.input); request.input = -1; eof = true;
            } else if (errno != EAGAIN && errno != EINTR) goto done;
        }
        if (request.pid > 0) {
            int status;
            pid_t reaped = waitpid(request.pid, &status, WNOHANG);
            if (reaped == request.pid) {
                request.pid = 0;
                successful = WIFEXITED(status) && WEXITSTATUS(status) == 0;
                if (!successful) goto done;
            } else if (reaped < 0 && errno != EINTR) {
                if (errno == ECHILD) request.pid = 0;
                goto done;
            }
        }
    }
    if (!successful || sent != request.context_size || (used && request.data[used - 1] != '\0')) goto done;
    count = 0;
    size_t offset = 0;
    while (offset < used) {
        size_t length = strlen(request.data + offset);
        if (length > CANDIDATE_LIMIT || ++count > CANDIDATE_LIMIT) goto done;
        offset += length + 1;
    }
    if (!same_context(&request, point, &directory)) goto done;
    replies = find_variable_noref("COMPREPLY");
    if (!replies || !array_p(replies) || readonly_p(replies) || noassign_p(replies) || integer_p(replies)) goto done;
    offset = 0;
    for (size_t i = 0; i < count; ++i) {
        array_insert(array_cell(replies), (arrayind_t)i, request.data + offset);
        offset += strlen(request.data + offset) + 1;
    }
    result = EXECUTION_SUCCESS;
done:
    discard_unwind_frame("binit-worker");
    cleanup(&request);
    return result;
}
