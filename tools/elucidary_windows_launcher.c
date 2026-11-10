#define UNICODE
#define _UNICODE
#include <windows.h>
#include <wchar.h>

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR unused, int show) {
    wchar_t folder[32768], python[32768], script[32768], command[32768];
    DWORD length = GetModuleFileNameW(NULL, folder, 32768);
    if (!length || length >= 32767) return 1;
    wchar_t *slash = wcsrchr(folder, L'\\');
    if (!slash) return 1;
    *slash = L'\0';
    if (swprintf(python, 32768, L"%ls\\runtime\\pythonw.exe", folder) < 0 ||
        swprintf(script, 32768, L"%ls\\run.pyw", folder) < 0) return 1;
    const wchar_t *tail = GetCommandLineW();
    if (*tail == L'"') { tail++; while (*tail && *tail != L'"') tail++; if (*tail) tail++; }
    else while (*tail && *tail != L' ' && *tail != L'\t') tail++;
    if (swprintf(command, 32768, L"\"%ls\" -I \"%ls\"%ls", python, script, tail) < 0) return 1;
    STARTUPINFOW start = {0}; PROCESS_INFORMATION child = {0}; start.cb = sizeof(start);
    if (!CreateProcessW(python, command, NULL, NULL, FALSE, 0, NULL, folder, &start, &child)) {
        MessageBoxW(NULL, L"The Elucidary runtime could not be started. Keep the application folder together.", L"Elucidary", MB_OK | MB_ICONERROR);
        return 1;
    }
    WaitForSingleObject(child.hProcess, INFINITE);
    DWORD status = 1; GetExitCodeProcess(child.hProcess, &status);
    CloseHandle(child.hThread); CloseHandle(child.hProcess);
    return (int)status;
}
