/* Pure encoding test: no filesystem, AppContainer, ACL or process operations. */
#include "windows_probe_diagnostic.h"

int main(void) {
    if (reply_open_failure(5) != 0x19000005u) return 1;
    if (reply_open_failure(32) != 0x19000020u) return 2;
    if (reply_open_failure(65535) != 0x1900ffffu) return 3;
    if (reply_open_failure(65536) != 0x19010000u) return 4;
    if (reply_open_failure(0xffffffffu) != 0x19010000u) return 5;
    return 0;
}
