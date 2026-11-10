/* Synthetic probe exit codes. No paths or fixture content are included. */
#ifndef ELUCIDARY_WINDOWS_PROBE_DIAGNOSTIC_H
#define ELUCIDARY_WINDOWS_PROBE_DIAGNOSTIC_H
#include <stdint.h>

static inline uint32_t reply_open_failure(uint32_t error) {
    /* Keep the exact common Win32 error, and explicitly report overflow. */
    return error <= 0xffffu ? 0x19000000u | error : 0x19010000u;
}
#endif
