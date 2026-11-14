#define _GNU_SOURCE 1
#include <errno.h>
#include <linux/sched.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

static int denied_child(long child, const char *name)
{
    if (child == 0) _exit(0);
    if (child > 0) {
        while (waitpid((pid_t)child, NULL, 0) < 0 && errno == EINTR) {}
        fprintf(stderr, "%s created a child\n", name);
        return 1;
    }
    if (errno != EPERM) {
        fprintf(stderr, "%s returned errno %d\n", name, errno);
        return 1;
    }
    return 0;
}

int main(int argc, char **argv)
{
#ifdef __x86_64__
    if (argc == 2 && strcmp(argv[1], "compat") == 0) {
        /* i386 getpid is harmless if the architecture check is broken. */
        __asm__ volatile("int $0x80" : : "a" (20) : "memory");
        return 1;
    }
    if (syscall(SYS_getpid | 0x40000000L) != -1 || errno != EPERM) return 2;
    if (denied_child(syscall(SYS_fork), "fork")) return 3;
#else
    (void)argc;
    (void)argv;
#endif
    /* vfork's child must exit before returning from the wrapper's stack frame. */
    pid_t child = vfork();
    if (child == 0) _exit(0);
    if (denied_child(child, "vfork")) return 4;
    if (denied_child(syscall(SYS_clone, SIGCHLD, 0, 0, 0, 0), "clone")) return 5;
    struct clone_args clone = {.exit_signal = SIGCHLD};
    if (denied_child(syscall(SYS_clone3, &clone, sizeof(clone)), "clone3")) return 6;
    if (prctl(PR_SET_PDEATHSIG, 0, 0, 0, 0) != -1 || errno != EPERM) return 7;
    return 0;
}
