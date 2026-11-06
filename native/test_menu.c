#define _XOPEN_SOURCE 700
#include "menu.h"
#include <assert.h>
#include <limits.h>
#include <locale.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static void append(struct binit_menu *menu, const char *text)
{
    for (const unsigned char *p = (const unsigned char *)text; *p; ++p) {
        assert(binit_menu_append(menu, *p));
    }
}

int main(void)
{
    // The test sets its locale before running any code; it creates no threads.
    // NOLINTNEXTLINE(concurrency-mt-unsafe)
    assert(setlocale(LC_CTYPE, "C.UTF-8") != NULL);
    const char *values[] = {"alpha one", "ALPHA two", "caffè", "other", "fifth", "sixth", "last"};
    struct binit_menu menu;
    assert(binit_menu_init(&menu, values, 7) == 0);
    assert(binit_menu_choice(&menu) == 1);
    binit_menu_grid_move(&menu, 1, 0, 3);
    assert(binit_menu_choice(&menu) == 2);
    binit_menu_grid_move(&menu, 0, 1, 3);
    assert(binit_menu_choice(&menu) == 5);
    binit_menu_grid_move(&menu, 0, 1, 3);
    assert(binit_menu_choice(&menu) == 7);
    binit_menu_grid_move(&menu, 0, -1, 3);
    assert(binit_menu_choice(&menu) == 4);
    binit_menu_grid_move(&menu, 0, 1, SIZE_MAX);
    assert(menu.selected < menu.count);
    binit_menu_move(&menu, PTRDIFF_MIN);
    assert(menu.selected < menu.count);
    append(&menu, "alpha");
    assert(menu.count == 2);
    binit_menu_move(&menu, 1);
    size_t choice = binit_menu_choice(&menu);
    binit_menu_backspace(&menu);
    assert(binit_menu_choice(&menu) == choice);
    binit_menu_clear(&menu);
    append(&menu, "caffè");
    assert(menu.count == 1 && binit_menu_choice(&menu) == 3);
    binit_menu_backspace(&menu);
    assert(strcmp(menu.query, "caff") == 0);
    binit_menu_clear(&menu);
    append(&menu, "does not exist");
    assert(menu.count == 0 && binit_menu_choice(&menu) == 0);
    binit_menu_move(&menu, 7);
    assert(!binit_menu_append(&menu, 27));
    binit_menu_clear(&menu);
    for (size_t i = 0; i < BINIT_QUERY_SIZE - 1; ++i) assert(binit_menu_append(&menu, 'a'));
    assert(!binit_menu_append(&menu, 'a'));
    binit_menu_free(&menu);
    assert(binit_menu_init(&menu, NULL, 0) == 0);
    binit_menu_grid_move(&menu, 0, 1, 3);
    assert(binit_menu_choice(&menu) == 0);
    binit_menu_free(&menu);
    assert(binit_menu_init(&menu, values, BINIT_MENU_LIMIT + 1) == -1);
    const char *invalid[] = {NULL};
    assert(binit_menu_init(&menu, invalid, 1) == -1);

    char output[128];
    assert(binit_text(output, sizeof output, "normal", 30) == 6);
    assert(strcmp(output, "normal") == 0);
    binit_text(output, sizeof output, "\033[31mevil\n\t\001", 100);
    assert(strcmp(output, "\\e[31mevil\\n\\t\\u0001") == 0);
    binit_text(output, sizeof output, "\xc2\x9b\xe2\x80\xae", 100);
    assert(strcmp(output, "\\u009b\\u202e") == 0);
    binit_text(output, sizeof output, "\xff", 100);
    assert(strcmp(output, "\\xff") == 0);
    assert(binit_text(output, sizeof output, "界a", 3) == 3);
    assert(strcmp(output, "界a") == 0);
    assert(binit_text(output, sizeof output, "界a", 2) <= 2);
    assert(strcmp(output, "…") == 0);
    assert(binit_text(output, sizeof output, "e\xcc\x81", 1) == 1);
    assert(strcmp(output, "e\xcc\x81") == 0);
    for (size_t capacity = 1; capacity < 10; ++capacity) {
        memset(output, 0x7f, sizeof output);
        assert(binit_text(output, capacity, "\033[31m界abcdef", 5) <= 5);
        assert(memchr(output, '\0', capacity) != NULL);
        assert((unsigned char)output[capacity] == 0x7f);
    }
    puts("menu model and terminal text: PASS");
    return 0;
}
