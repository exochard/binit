#ifndef BINIT_PROMPT_WORKER_H
#define BINIT_PROMPT_WORKER_H
#include <stddef.h>

int binit_prompt_worker(WORD_LIST *arguments);
void binit_prompt_worker_input(void);
void binit_prompt_worker_close(void);
int binit_path_worker_start(const char *manager, const char *request, size_t size);
int binit_path_worker_take(char *result, size_t size);
void binit_path_worker_close(void);

#endif
