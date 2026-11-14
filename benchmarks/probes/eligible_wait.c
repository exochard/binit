/* Feasibility only: one known sleep child, no candidate protocol or public API. */
#define _GNU_SOURCE 1
#include <config.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <spawn.h>
#include <stdbool.h>
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>
#include "builtins.h"
#include "variables.h"
#include "unwind_prot.h"
#include "quit.h"
#include <readline/readline.h>

struct request {
    pid_t pid;
    sigset_t previous;
    FILE *receipt;
};

static void cleanup(void *opaque)
{
    struct request *request = opaque;
    if (request->pid > 0) {
        /* SIGCHLD stays blocked until this owned PID has been reaped. */
        int killed = kill(-request->pid, SIGKILL);
        int status = 0;
        pid_t reaped;
        do { reaped = waitpid(request->pid, &status, 0); } while (reaped < 0 && errno == EINTR);
        if (request->receipt)
            fprintf(request->receipt, "cancelled %ld %d %ld %d\n", (long)request->pid,
                    killed, (long)reaped, WIFSIGNALED(status) ? WTERMSIG(status) : 0);
        request->pid = 0;
    }
    if (request->receipt) fclose(request->receipt);
    (void)sigprocmask(SIG_SETMASK, &request->previous, NULL);
}

__attribute__((visibility("default")))
int binit_wait_probe_builtin(WORD_LIST *arguments)
{
    if (!arguments || !arguments->next || arguments->next->next ||
        arguments->next->word->word[0] != '/' ||
        !rl_instream || !isatty(fileno(rl_instream))) return 2;
    struct request request = {.pid = 0};
    sigset_t blocked;
    sigemptyset(&blocked);
    sigaddset(&blocked, SIGCHLD);
    if (sigprocmask(SIG_BLOCK, &blocked, &request.previous) != 0) return 1;
    begin_unwind_frame("binit-wait-probe");
    add_unwind_protect(cleanup, &request);
    posix_spawnattr_t attributes;
    posix_spawn_file_actions_t files;
    int result = 1;
    request.receipt = fopen(arguments->word->word, "w");
    if (!request.receipt) goto done;
    if (posix_spawnattr_init(&attributes) != 0) goto done;
    if (posix_spawn_file_actions_init(&files) != 0) {
        posix_spawnattr_destroy(&attributes);
        goto done;
    }
    int error = posix_spawnattr_setflags(&attributes, POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_SETSIGMASK);
    if (!error) error = posix_spawnattr_setpgroup(&attributes, 0);
    if (!error) error = posix_spawnattr_setsigmask(&attributes, &request.previous);
    for (int fd = 0; !error && fd < 3; ++fd)
        error = posix_spawn_file_actions_addopen(&files, fd, "/dev/null", O_RDWR, 0);
    if (!error) error = posix_spawn_file_actions_addclosefrom_np(&files, 3);
    maybe_make_export_env();
    char *worker = arguments->next->word->word;
    char *argv[] = {worker, "0.2", NULL};
    pid_t spawned = 0;
    if (!error) error = posix_spawn(&spawned, worker, &files, &attributes, argv, export_env);
    posix_spawn_file_actions_destroy(&files);
    posix_spawnattr_destroy(&attributes);
    if (error) goto done;
    request.pid = spawned;
    fprintf(request.receipt, "spawned %ld\n", (long)request.pid);
    fflush(request.receipt);
    struct pollfd input = {.fd = fileno(rl_instream), .events = POLLIN};
    for (;;) {
        QUIT;
        int ready = poll(&input, 1, 1);
        if (ready < 0 && errno != EINTR) break;
        if (rl_pending_input || (ready > 0 && input.revents)) break;
        int status;
        pid_t done = waitpid(request.pid, &status, WNOHANG);
        if (done == request.pid) {
            fprintf(request.receipt, "completed %ld %d\n", (long)request.pid, status);
            request.pid = 0;
            result = WIFEXITED(status) && WEXITSTATUS(status) == 0 ? 0 : 1;
            break;
        }
        if (done < 0 && errno != EINTR) break;
    }
done:
    discard_unwind_frame("binit-wait-probe");
    cleanup(&request);
    return result;
}

static char *documentation[] = {"Private feasibility probe; no product API.", NULL};
__attribute__((visibility("default")))
struct builtin binit_wait_probe_struct = {
    "binit_wait_probe", binit_wait_probe_builtin, BUILTIN_ENABLED,
    documentation, "binit_wait_probe RECEIPT WORKER", NULL
};
