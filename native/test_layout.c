#define _XOPEN_SOURCE 700
#include "layout.h"

#include <assert.h>
#include <locale.h>
#include <string.h>

#define LAYOUT(p, w, text, point, out) binit_layout(p, w, text, strlen(text), point, out)

static void check(int p, int w, const char *text, int row, int column, int rows)
{
    struct binit_layout layout;
    assert(LAYOUT(p, w, text, strlen(text), &layout));
    assert(layout.end.row == row && layout.end.column == column && layout.rows == rows);
    assert(layout.cursor.row == row && layout.cursor.column == column);
}

static void test_wrap(void)
{
    struct binit_layout layout;
    check(7, 20, "abcdefghijkl", 0, 19, 1);
    check(7, 20, "abcdefghijklm", 1, 0, 2);
    check(7, 20, "abcdefghijklmn", 1, 1, 2);
    assert(LAYOUT(7, 20, "abcdefghijklm", 13, &layout) && layout.offset[1] == 13 && layout.column[1] == 0 && layout.column[0] == 7);
    assert(LAYOUT(7, 20, "abcdefghijklmn", 0, &layout) && layout.cursor.row == 0 && layout.cursor.column == 7);
    assert(LAYOUT(7, 20, "abcdefghijklmn", 13, &layout) && layout.cursor.row == 1 && layout.cursor.column == 0);
    assert(layout.offset[1] == 13);
    assert(LAYOUT(0, 5, "", 0, &layout) && layout.rows == 1 && layout.end.column == 0);
}

static void test_wide(void)
{
    struct binit_layout layout;
    check(0, 5, "abc\xe7\x95\x8c", 1, 0, 2);
    assert(LAYOUT(0, 5, "abcd\xe7\x95\x8c", 4, &layout) && layout.cursor.row == 1 && layout.cursor.column == 0);
    assert(layout.end.row == 1 && layout.end.column == 2 && layout.rows == 2 && layout.offset[1] == 4);
    assert(LAYOUT(0, 5, "abcd\xe7\x95\x8c", 7, &layout) && layout.cursor.row == 1 && layout.cursor.column == 2);
    assert(!LAYOUT(0, 5, "abcd\xe7\x95\x8c", 5, &layout));
}

static void test_combining(void)
{
    struct binit_layout layout;
    check(0, 5, "e\xcc\x81", 0, 1, 1);
    assert(LAYOUT(0, 5, "e\xcc\x81x", 3, &layout) && layout.cursor.column == 1 && layout.end.column == 2);
    assert(binit_cells("e\xcc\x81", 3) == 1);
}

/* Readline starts a row at a newline and pads a tab to the next multiple of eight, past the margin if needed. */
static void test_breaks(void)
{
    struct binit_layout layout;
    check(7, 20, "ab\ncd", 1, 2, 2);
    check(7, 20, "ab\n", 1, 0, 2);
    check(0, 5, "abcde\nf", 2, 1, 3); /* a full row and then a newline leave an empty row */
    assert(LAYOUT(7, 20, "ab\ncd", 2, &layout) && layout.cursor.row == 0 && layout.cursor.column == 9);
    assert(LAYOUT(7, 20, "ab\ncd", 3, &layout) && layout.cursor.row == 1 && layout.cursor.column == 0);
    assert(layout.offset[1] == 3 && layout.column[1] == 0);
    check(7, 20, "a\tb", 0, 17, 1);
    check(0, 20, "\tb", 0, 9, 1);
    assert(LAYOUT(7, 20, "a\tb", 1, &layout) && layout.cursor.row == 0 && layout.cursor.column == 8);
    check(7, 16, "a\tb", 1, 1, 2); /* the tab ends at the margin */
    assert(LAYOUT(0, 20, "abcdefghijklmnopq\tb", 19, &layout) && layout.rows == 2);
    assert(layout.offset[1] == 18 && layout.column[1] == 4 && layout.end.column == 5);
    assert(!LAYOUT(2, 3, "\t", 0, &layout)); /* the spill would not fit a row */
}

static void test_ascii_widths(void)
{
    for (unsigned int byte = 0; byte < 128; ++byte) {
        char text = (char)byte;
        int expected = byte >= 0x20 && byte <= 0x7e ? 1 : -1;
        assert(binit_cells(&text, 1) == expected);
    }
}

static void test_rejects(void)
{
    struct binit_layout layout;
    assert(binit_cells("a\x01", 2) == -1 && binit_cells("a\t", 2) == -1 && binit_cells("\xff", 1) == -1);
    assert(binit_cells("\xe7\x95", 2) == -1 && binit_cells("a\0b", 3) == -1 && binit_cells("\xc2\x85", 2) == -1);
    assert(binit_cells("\xe2\x80\xae", 3) == -1 && binit_cells("\xe7\x95\x8c" "a", 4) == 3);
    assert(!LAYOUT(0, 20, "a\x01", 0, &layout) && !LAYOUT(0, 20, "a\xff", 0, &layout));
    assert(!LAYOUT(20, 20, "a", 0, &layout) && !LAYOUT(0, 1, "a", 0, &layout) && !LAYOUT(-1, 20, "a", 0, &layout));
    assert(!LAYOUT(0, 20, "a", 2, &layout));
    static char big[4100];
    memset(big, 'a', sizeof big - 1);
    assert(!binit_layout(0, 80, big, 4097, 0, &layout) && binit_layout(0, 80, big, 4096, 0, &layout));
    assert(binit_layout(0, 40, big, (size_t)64 * 40 - 1, 0, &layout) && layout.rows == 64);
    assert(!binit_layout(0, 40, big, (size_t)64 * 40, 0, &layout));
    assert(binit_layout(0, 64, big, (size_t)64 * 64 - 1, 0, &layout) && layout.rows == 64);
}

static void test_prompt(void)
{
    assert(binit_prompt_cells("a\nbc> ") == 4);
    assert(binit_prompt_cells("\001\033[31m\002a> ") == 3);
    assert(binit_prompt_cells("\001\033]0;t\007\002x> ") == 3);
    assert(binit_prompt_cells("abc\n") == 0);
    assert(binit_prompt_cells("\033[1;32mab\033[0m") == 2);
    assert(binit_prompt_cells("\033[1Aab") == -1);
    assert(binit_prompt_cells("\033]0;t\007ab") == -1);
    assert(binit_prompt_cells("\001ab") == -1);
    assert(binit_prompt_cells("\001\n\002x") == -1); /* the cut leaves a stray STX */
    assert(binit_prompt_cells("\001a\002\n\001b\002x") == 1);
    assert(binit_prompt_cells("\xff") == -1 && binit_prompt_cells("\t") == -1 && binit_prompt_cells("") == 0);
    assert(binit_prompt_cells("\xe7\x95\x8c> ") == 4);
    static char big[8200];
    memset(big, 'a', sizeof big - 1);
    assert(binit_prompt_cells(big) == -1);
    big[8192] = '\0';
    assert(binit_prompt_cells(big) == 8192);
}

static void test_fit(void)
{
    int used;
    assert(binit_fit("abcdef", 6, 4, &used) == 4 && used == 4);
    assert(binit_fit("abc", 3, 9, &used) == 3 && used == 3);
    assert(binit_fit("ab\xe7\x95\x8c", 5, 3, &used) == 2 && used == 2);
    assert(binit_fit("ab\xe7\x95\x8c", 5, 4, &used) == 5 && used == 4);
    assert(binit_fit("ab\tcd", 5, 5, &used) == 2 && used == 2);
    assert(binit_fit("abc", 3, 0, &used) == 0 && used == 0);
}

int main(void)
{
    test_ascii_widths();
    // The test sets its locale before running any code; it creates no threads.
    // NOLINTNEXTLINE(concurrency-mt-unsafe)
    assert(setlocale(LC_ALL, "C.UTF-8"));
    test_ascii_widths();
    test_wrap();
    test_wide();
    test_combining();
    test_breaks();
    test_rejects();
    test_prompt();
    test_fit();
    return 0;
}
