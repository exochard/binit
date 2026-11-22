#ifndef BINIT_WORKER_H
#define BINIT_WORKER_H
#include "command.h"
#include <stdbool.h>
int binit_worker_request(WORD_LIST *);
bool binit_worker_preview(char output[1025]);
bool binit_worker_has_provider(void);
#endif
