/* Synthetic-only AppContainer probe. Optional loopback check targets the test's own listener. */
#define UNICODE
#define _UNICODE
#include <winsock2.h>
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

int wmain(int argc, wchar_t **argv) {
    if (argc != 4 && argc != 5) return 10;
    HANDLE token = NULL;
    DWORD inside = 0, needed = 0;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return 11;
    if (!GetTokenInformation(token, TokenIsAppContainer, &inside, sizeof(inside), &needed) || !inside) {
        CloseHandle(token); return 12;
    }
    GetTokenInformation(token, TokenCapabilities, NULL, 0, &needed);
    if (!needed || needed > 1024 * 1024) { CloseHandle(token); return 13; }
    TOKEN_GROUPS *caps = (TOKEN_GROUPS *)malloc(needed);
    if (!caps || !GetTokenInformation(token, TokenCapabilities, caps, needed, &needed) || caps->GroupCount != 0) {
        free(caps); CloseHandle(token); return 14;
    }
    free(caps); CloseHandle(token);
    HANDLE input = CreateFileW(argv[1], GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    if (input == INVALID_HANDLE_VALUE) return 15;
    char contents[32]; DWORD read = 0;
    if (!ReadFile(input, contents, sizeof(contents), &read, NULL) || read == 0) { CloseHandle(input); return 16; }
    CloseHandle(input);
    HANDLE mutation = CreateFileW(argv[1], GENERIC_WRITE, 0, NULL, OPEN_EXISTING, 0, NULL);
    if (mutation != INVALID_HANDLE_VALUE) { CloseHandle(mutation); return 17; }
    if (GetLastError() != ERROR_ACCESS_DENIED) return 18;
    HANDLE outside = CreateFileW(argv[2], GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    if (outside != INVALID_HANDLE_VALUE) { CloseHandle(outside); return 19; }
    if (GetLastError() != ERROR_ACCESS_DENIED) return 20;
    wchar_t self[32768], command[32768];
    DWORD length = GetModuleFileNameW(NULL, self, 32768);
    if (!length || length >= 32767 || swprintf(command, 32768, L"\"%ls\"", self) < 0) return 23;
    STARTUPINFOW startup = {0}; PROCESS_INFORMATION child = {0}; startup.cb = sizeof(startup);
    if (CreateProcessW(self, command, NULL, NULL, FALSE, CREATE_SUSPENDED | CREATE_NO_WINDOW, NULL, NULL, &startup, &child)) {
        TerminateProcess(child.hProcess, 1); WaitForSingleObject(child.hProcess, 5000);
        CloseHandle(child.hThread); CloseHandle(child.hProcess); return 24;
    }
    int loopback_outcome = 0;
    if (argc == 5) {
        unsigned long port = wcstoul(argv[4], NULL, 10);
        if (!port || port > 65535) return 25;
        WSADATA wsa;
        if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) return 26;
        SOCKET sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (sock == INVALID_SOCKET) { WSACleanup(); return 27; }
        u_long nonblocking = 1;
        if (ioctlsocket(sock, FIONBIO, &nonblocking) != 0) { closesocket(sock); WSACleanup(); return 28; }
        struct sockaddr_in address = {0}; address.sin_family = AF_INET; address.sin_port = htons((u_short)port); address.sin_addr.s_addr = htonl(0x7f000001);
        int outcome = connect(sock, (struct sockaddr *)&address, sizeof(address));
        int error = outcome == 0 ? 0 : WSAGetLastError();
        if (error == WSAEWOULDBLOCK || error == WSAEINPROGRESS) {
            fd_set write_set, error_set; FD_ZERO(&write_set); FD_ZERO(&error_set); FD_SET(sock, &write_set); FD_SET(sock, &error_set);
            struct timeval timeout = {2, 0};
            int ready = select(0, NULL, &write_set, &error_set, &timeout);
            if (ready == 0) error = -1;
            else if (ready == SOCKET_ERROR) error = -2;
            else { int length = sizeof(error); if (getsockopt(sock, SOL_SOCKET, SO_ERROR, (char *)&error, &length) != 0) error = -2; }
        }
        closesocket(sock); WSACleanup();
        if (error == WSAEACCES) loopback_outcome = 1;
        else if (error == -1) loopback_outcome = 2;
        else return error > 0 ? 1000 + error : error == 0 ? 30 : 31;
    }
    HANDLE output = CreateFileW(argv[3], GENERIC_WRITE, 0, NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
    if (output == INVALID_HANDLE_VALUE) return 21;
    const char *response = loopback_outcome == 1 ? "{\"synthetic_probe\":true,\"loopback_result\":\"os_access_denied\"}" :
                           loopback_outcome == 2 ? "{\"synthetic_probe\":true,\"loopback_result\":\"timeout\"}" : "{\"synthetic_probe\":true}";
    DWORD response_length = (DWORD)strlen(response), written = 0;
    BOOL ok = WriteFile(output, response, response_length, &written, NULL);
    CloseHandle(output);
    return ok && written == response_length ? 0 : 22;
}
