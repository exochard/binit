#define _XOPEN_SOURCE 700
#include "menu.h"

#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

static unsigned char fold(unsigned char c)
{
    return c >= 'A' && c <= 'Z' ? (unsigned char)(c + ('a' - 'A')) : c;
}

static void filter(struct binit_menu *menu)
{
    unsigned char query[BINIT_QUERY_SIZE];
    size_t prefix[BINIT_QUERY_SIZE] = {0};
    size_t length = strlen(menu->query);
    size_t previous = binit_menu_choice(menu);
    for (size_t i = 0; i < length; ++i) query[i] = fold((unsigned char)menu->query[i]);
    for (size_t i = 1, j = 0; i < length; ++i) {
        while (j > 0 && query[i] != query[j]) j = prefix[j - 1];
        if (query[i] == query[j]) ++j;
        prefix[i] = j;
    }
    menu->count = menu->selected = 0;
    for (size_t i = 0; i < menu->total; ++i) {
        bool matched = length == 0;
        size_t j = 0;
        for (const unsigned char *p = (const unsigned char *)menu->values[i]; !matched && *p; ++p) {
            unsigned char c = fold(*p);
            while (j > 0 && c != query[j]) j = prefix[j - 1];
            if (c == query[j]) ++j;
            matched = j == length;
        }
        if (matched) {
            if (i + 1 == previous) menu->selected = menu->count;
            menu->visible[menu->count++] = i;
        }
    }
}

int binit_menu_init(struct binit_menu *menu, const char *const *values, size_t count)
{
    if (menu == NULL) return -1;
    memset(menu, 0, sizeof *menu);
    if (count > BINIT_MENU_LIMIT || (count != 0 && values == NULL)) return -1;
    size_t remaining = (size_t)8 * 1024 * 1024;
    for (size_t i = 0; i < count; ++i) {
        if (values[i] == NULL || remaining == 0) return -1;
        size_t length = strnlen(values[i], remaining);
        if (length >= remaining) return -1;
        remaining -= length + 1;
    }
    menu->visible = count ? malloc(count * sizeof *menu->visible) : NULL;
    if (count && menu->visible == NULL) return -1;
    menu->values = values;
    menu->total = count;
    filter(menu);
    return 0;
}

void binit_menu_free(struct binit_menu *menu)
{
    if (menu == NULL) return;
    free(menu->visible);
    memset(menu, 0, sizeof *menu);
}

bool binit_menu_append(struct binit_menu *menu, unsigned char byte)
{
    size_t length = strlen(menu->query);
    if (byte < 32 || byte == 127 || length + 1 >= sizeof menu->query) return false;
    menu->query[length] = (char)byte;
    menu->query[length + 1] = '\0';
    filter(menu);
    return true;
}

void binit_menu_backspace(struct binit_menu *menu)
{
    size_t length = strlen(menu->query);
    if (length == 0) return;
    --length;
    while (length > 0 && ((unsigned char)menu->query[length] & 0xc0u) == 0x80u) --length;
    menu->query[length] = '\0';
    filter(menu);
}

void binit_menu_clear(struct binit_menu *menu)
{
    menu->query[0] = '\0';
    filter(menu);
}

void binit_menu_move(struct binit_menu *menu, ptrdiff_t distance)
{
    if (menu->count == 0) return;
    ptrdiff_t count = (ptrdiff_t)menu->count;
    ptrdiff_t position = (ptrdiff_t)menu->selected + distance % count;
    menu->selected = (size_t)((position + count) % count);
}

void binit_menu_grid_move(struct binit_menu *menu, int horizontal, int vertical, size_t columns)
{
    if (menu->count == 0 || columns == 0) return;
    if (columns > menu->count) columns = menu->count;
    size_t rows = (menu->count + columns - 1) / columns;
    size_t row = menu->selected / columns;
    size_t column = menu->selected % columns;
    if (vertical) {
        row = vertical > 0 ? (row + 1) % rows : (row + rows - 1) % rows;
    }
    size_t length = menu->count - row * columns;
    if (length > columns) length = columns;
    if (column >= length) column = length - 1;
    if (horizontal) column = horizontal > 0 ? (column + 1) % length : (column + length - 1) % length;
    menu->selected = row * columns + column;
}

size_t binit_menu_choice(const struct binit_menu *menu)
{
    return menu->count ? menu->visible[menu->selected] + 1 : 0;
}

static bool hidden(wchar_t c)
{
    return (c >= 0x80 && c <= 0x9f) || (c >= 0x202a && c <= 0x202e) ||
           (c >= 0x2066 && c <= 0x2069);
}

int binit_text(char *output, size_t capacity, const char *input, int maximum)
{
    if (output == NULL || capacity == 0) return 0;
    output[0] = '\0';
    if (input == NULL || maximum <= 0) return 0;
    size_t used = 0, last = 0;
    int columns = 0, before_last = 0;
    mbstate_t state = {0};
    while (*input) {
        wchar_t code;
        // Bash calls this on its main thread; decoding uses a private state.
        // NOLINTNEXTLINE(concurrency-mt-unsafe)
        size_t bytes = mbrtowc(&code, input, MB_LEN_MAX, &state);
        char escaped[16];
        const char *text = input;
        size_t length = bytes;
        int width;
        if (bytes == (size_t)-1 || bytes == (size_t)-2) {
            memset(&state, 0, sizeof state);
            bytes = 1;
            snprintf(escaped, sizeof escaped, "\\x%02x", (unsigned int)(unsigned char)*input);
            text = escaped;
            length = 4;
            width = 4;
        } else {
            width = wcwidth(code);
            if (code < 32 || code == 127 || hidden(code) || width < 0 || (width == 0 && columns == 0)) {
                if (code == '\n' || code == '\r' || code == '\t' || code == 27) {
                    escaped[0] = '\\';
                    escaped[1] = (char)(code == '\n' ? 'n' : code == '\r' ? 'r' : code == '\t' ? 't' : 'e');
                    escaped[2] = '\0';
                } else {
                    snprintf(escaped, sizeof escaped, code <= 0xffff ? "\\u%04x" : "\\U%08x", (unsigned int)code);
                }
                text = escaped;
                length = strlen(escaped);
                width = (int)length;
            }
        }
        if (width > maximum - columns || length >= capacity - used) break;
        if (width > 0) {last = used; before_last = columns;}
        memcpy(output + used, text, length);
        used += length;
        columns += width;
        input += bytes;
    }
    if (*input) {
        if (columns >= maximum) {used = last; columns = before_last;}
        if (capacity - used >= 4 && columns < maximum) {
            memcpy(output + used, "\xe2\x80\xa6", 3);
            used += 3;
            ++columns;
        }
    }
    output[used] = '\0';
    return columns;
}
