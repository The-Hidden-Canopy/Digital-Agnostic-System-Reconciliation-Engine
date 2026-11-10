/* Fixed synthetic mock, not a language model. Reads a prompt file and writes a fixed reply. */
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include "windows_probe_diagnostic.h"

int wmain(int argc, wchar_t **argv) {
    if (argc != 5) return 10;
    HANDLE token = NULL; DWORD inside = 0, needed = 0;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return 11;
    if (!GetTokenInformation(token, TokenIsAppContainer, &inside, sizeof(inside), &needed) || !inside) { CloseHandle(token); return 12; }
    GetTokenInformation(token, TokenCapabilities, NULL, 0, &needed);
    if (!needed || needed > 1024 * 1024) { CloseHandle(token); return 21; }
    TOKEN_GROUPS *caps = (TOKEN_GROUPS *)malloc(needed);
    if (!caps || !GetTokenInformation(token, TokenCapabilities, caps, needed, &needed) || caps->GroupCount != 0) {
        free(caps); CloseHandle(token); return 22;
    }
    free(caps);
    CloseHandle(token);
    HANDLE prompt = CreateFileW(argv[1], GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_EXISTING, 0, NULL);
    if (prompt == INVALID_HANDLE_VALUE) return 13;
    char input[256]; DWORD read = 0;
    if (!ReadFile(prompt, input, sizeof(input), &read, NULL) || !read) { CloseHandle(prompt); return 14; }
    CloseHandle(prompt);
    HANDLE mutation = CreateFileW(argv[1], GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_EXISTING, 0, NULL);
    if (mutation != INVALID_HANDLE_VALUE) { CloseHandle(mutation); return 15; }
    if (GetLastError() != ERROR_ACCESS_DENIED) return 16;
    HANDLE outside = CreateFileW(argv[2], GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    if (outside != INVALID_HANDLE_VALUE) { CloseHandle(outside); return 17; }
    if (GetLastError() != ERROR_ACCESS_DENIED) return 18;
    if (argv[4][0] == L'p') {
        const wchar_t *slash = wcsrchr(argv[3], L'\\');
        wchar_t extra[32768];
        if (!slash || swprintf(extra, 32768, L"%.*ls\\extra.tmp", (int)(slash - argv[3]), argv[3]) < 0) return 23;
        HANDLE created = CreateFileW(extra, GENERIC_WRITE, 0, NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
        if (created != INVALID_HANDLE_VALUE) { CloseHandle(created); return 24; }
        if (GetLastError() != ERROR_ACCESS_DENIED) return 25;
        HANDLE deleted = CreateFileW(argv[3], DELETE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_EXISTING, 0, NULL);
        if (deleted != INVALID_HANDLE_VALUE) { CloseHandle(deleted); return 26; }
        if (GetLastError() != ERROR_ACCESS_DENIED) return 27;
    }
    HANDLE result = CreateFileW(argv[3], GENERIC_WRITE, 0, NULL, argv[4][0] == L'p' ? OPEN_EXISTING : CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
    if (result == INVALID_HANDLE_VALUE) {
        DWORD error = GetLastError();
        return (int)reply_open_failure(error);
    }
    char oversized[2048]; memset(oversized, 'x', sizeof(oversized));
    const char *reply = (argv[4][0] == L'1' || argv[4][0] == L'p') ? "The source writes a return [1]." :
                        argv[4][0] == L'd' ? "{\"ok\":true,\"ok\":false,\"extraction\":{}}" :
                        argv[4][0] == L'o' ? oversized : "Invalid citation [2].";
    DWORD length = argv[4][0] == L'o' ? (DWORD)sizeof(oversized) : (DWORD)strlen(reply), written = 0;
    BOOL ok = WriteFile(result, reply, length, &written, NULL);
    CloseHandle(result);
    if (argv[4][0] == L's') Sleep(30000);
    return ok && written == length ? 0 : 20;
}
