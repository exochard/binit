#ifndef BINIT_MENU_H
#define BINIT_MENU_H

#include <stdbool.h>
#include <stddef.h>

#define BINIT_MENU_LIMIT 16384
#define BINIT_QUERY_SIZE 256
enum binit_match { BINIT_LITERAL, BINIT_WORDS, BINIT_FUZZY };

struct binit_menu {
    const char *const *values;
    size_t total;
    size_t *visible;
    size_t count;
    size_t selected;
    enum binit_match match;
    char query[BINIT_QUERY_SIZE];
};

bool binit_query_matches(const char *, const char *, enum binit_match);
int binit_menu_init(struct binit_menu *, const char *const *, size_t);
void binit_menu_free(struct binit_menu *);
bool binit_menu_append(struct binit_menu *, unsigned char);
void binit_menu_backspace(struct binit_menu *);
void binit_menu_clear(struct binit_menu *);
void binit_menu_move(struct binit_menu *, ptrdiff_t);
void binit_menu_grid_move(struct binit_menu *, int, int, size_t);
size_t binit_menu_choice(const struct binit_menu *);
int binit_text(char *, size_t, const char *, int);

#endif
