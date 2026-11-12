#include "highlight.h"
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static void expect(const char *line, const char *digits)
{
    uint8_t colors[4096];
    size_t length = strlen(line);
    assert(strlen(digits) == length);
    assert(binit_highlight(line, length, colors));
    for (size_t n = 0; n < length; ++n)
        if (colors[n] != (uint8_t)(digits[n] - '0')) {
            fprintf(stderr, "%s: byte %zu is %d, want %c\n", line, n, colors[n], digits[n]);
            assert(0);
        }
}

static void expect_incremental(const char *prefix, const char *line)
{
    uint8_t first[4096], second[4096];
    size_t length = strlen(line);
    binit_highlight_clear();
    assert(binit_highlight(prefix, strlen(prefix), first));
    assert(binit_highlight(line, length, first));
    binit_highlight_clear();
    assert(binit_highlight(line, length, second));
    assert(!memcmp(first, second, length));
}

int main(void)
{
    const char *cases[] = {"echo 'hello'", "echo ${ printf x; }", "case x in x) : ;;& esac", "cat <<EOF\nbody\nEOF\n", "echo café"};
    uint8_t colors[4096];
    for (size_t i = 0; i < sizeof(cases) / sizeof(*cases); ++i) {
        size_t length = strlen(cases[i]);
        assert(binit_highlight(cases[i], length, colors));
        assert(colors[0] == (i == 2 ? 5 : 3));
        for (size_t n = 0; n < length; ++n) assert(colors[n] <= 5);
    }
    expect("for f in a b; do echo \"$f\"", "5550" "4" "0" "55" "000000" "55" "0" "3333" "0" "2242");
    expect("while true; do sleep 1", "55555" "0" "3333" "00" "55" "0" "33333" "00");
    expect("if [[ -n $HOME ]]; then ls", "55" "00000000" "4444" "00000" "5555" "0" "33");
    expect("if true; then ec", "55" "0" "3333" "00" "5555" "0" "33");
    expect("echo \"open", "3333" "0" "22222");
    expect("echo 'open", "3333" "0" "22222");
    expect("echo \"open $HOME", "3333" "0" "2222222" "4444");
    expect("echo for", "3333" "0" "000");
    expect("echo hi; ls", "3333" "00000" "33");
    expect_incremental("for f in a b; do ec", "for f in a b; do echo \"$f\"");
    expect_incremental("echo \"op", "echo \"open $HOME");
    binit_highlight_clear();
    return 0;
}
