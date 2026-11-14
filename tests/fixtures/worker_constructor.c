#define _GNU_SOURCE 1
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

__attribute__((constructor)) static void before_main(void)
{
    const char *path = getenv("BINIT_WORKER_CONSTRUCTOR_MARKER");
    if (!path) return;
    pid_t child = fork();
    if (child == 0) _exit(0);
    if (child > 0) {
        int marker = open(path, O_WRONLY | O_CREAT | O_CLOEXEC, 0600);
        if (marker >= 0) close(marker);
        while (waitpid(child, NULL, 0) < 0 && errno == EINTR) {}
    }
}
