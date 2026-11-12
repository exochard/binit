#include "highlight.h"
#include <assert.h>
#include <stdint.h>
#include <string.h>

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
    binit_highlight_clear();
    return 0;
}
