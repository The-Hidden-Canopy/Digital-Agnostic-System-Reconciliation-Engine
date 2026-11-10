import os
import ctypes
import tempfile
import unittest
from pathlib import Path

from verifier_lab.ingestion.windows_reply_guard import (
    FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT, ReplyGuardError, _Info, _open_relative_reply, _same_parent, _validate,
    close_reply_directory, open_reply_directory, read_reply, read_reply_from_handle,
)


class ReplyMetadataTests(unittest.TestCase):
    def info(self, attributes=0, links=1, size=0):
        value = _Info(); value.attributes = attributes; value.links = links
        value.size_high, value.size_low = size >> 32, size & 0xffffffff
        return value

    def test_reparse_points_hardlinks_wrong_type_and_oversize_are_rejected(self):
        invalid = [(self.info(attributes=FILE_ATTRIBUTE_REPARSE_POINT), False, "worker_protocol_error"),
                   (self.info(links=2), False, "worker_protocol_error"),
                   (self.info(attributes=FILE_ATTRIBUTE_DIRECTORY), False, "worker_protocol_error"),
                   (self.info(), True, "worker_protocol_error"),
                   (self.info(size=1 << 32), False, "output_limit")]
        for info, directory, code in invalid:
            with self.subTest(attributes=info.attributes, links=info.links, directory=directory):
                with self.assertRaises(ReplyGuardError) as error:
                    _validate(info, directory=directory, limit=30)
                self.assertEqual(error.exception.code, code)

    def test_final_parent_requires_same_directory_not_a_prefix(self):
        self.assertTrue(_same_parent(r"\\?\C:\Work\Scratch", r"\\?\c:\work\scratch\result.json"))
        self.assertFalse(_same_parent(r"\\?\C:\Work\Scratch", r"\\?\C:\Work\Scratch-other\result.json"))
        self.assertFalse(_same_parent(r"\\?\C:\Work\Scratch", r"\\?\C:\Work\Scratch\child\result.json"))

    @unittest.skipUnless(os.name == "nt", "Windows handle checks")
    def test_reads_synthetic_reply_and_refuses_oversize(self):
        with tempfile.TemporaryDirectory(prefix="elucidary-reply-test-") as tmp:
            root = Path(tmp); (root / "result.json").write_bytes(b"synthetic")
            self.assertEqual(read_reply(root, 9), b"synthetic")
            with self.assertRaises(ReplyGuardError) as error: read_reply(root, 8)
            self.assertEqual(error.exception.code, "output_limit")

    @unittest.skipUnless(os.name == "nt", "Windows handle checks")
    def test_missing_reply_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="elucidary-reply-test-") as tmp:
            with self.assertRaises(ReplyGuardError) as error: read_reply(Path(tmp), 100)
            self.assertEqual(error.exception.code, "worker_protocol_error")

    @unittest.skipUnless(os.name == "nt", "Windows sharing checks")
    def test_existing_writer_must_close_before_reply_can_be_read(self):
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        with tempfile.TemporaryDirectory(prefix="elucidary-reply-test-") as tmp:
            root = Path(tmp); reply = root / "result.json"; reply.write_bytes(b"synthetic")
            handle = kernel.CreateFileW(str(reply), 0x40000000, 3, None, 3, 0, None)
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
            try:
                with self.assertRaises(ReplyGuardError) as error:
                    read_reply(root, 100)
                self.assertEqual(error.exception.code, "worker_protocol_error")
            finally:
                kernel.CloseHandle(handle)
            self.assertEqual(read_reply(root, 100), b"synthetic")

    @unittest.skipUnless(os.name == "nt", "Windows relative handle checks")
    def test_relative_open_uses_the_supplied_directory_handle(self):
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        with tempfile.TemporaryDirectory(prefix="elucidary-relative-test-") as tmp:
            roots = [Path(tmp) / "first", Path(tmp) / "second"]
            for root, content in zip(roots, (b"first fixture", b"second fixture")):
                root.mkdir(); (root / "result.json").write_bytes(content)
            for root, expected in zip(roots, (b"first fixture", b"second fixture")):
                directory = kernel.CreateFileW(str(root), 0x80, 3, None, 3, 0x02000000 | 0x00200000, None)
                self.assertNotEqual(directory, ctypes.c_void_p(-1).value)
                try:
                    reply = _open_relative_reply(directory)
                    fd = msvcrt.open_osfhandle(reply, os.O_RDONLY | os.O_BINARY)
                    try: self.assertEqual(os.read(fd, 100), expected)
                    finally: os.close(fd)
                finally:
                    kernel.CloseHandle(directory)

    @unittest.skipUnless(os.name == "nt", "Windows directory handle checks")
    def test_retained_directory_handle_reads_its_own_reply(self):
        with tempfile.TemporaryDirectory(prefix="elucidary-retained-test-") as tmp:
            roots = [Path(tmp) / "first", Path(tmp) / "second"]
            for root, payload in zip(roots, (b"first", b"second")):
                root.mkdir(); (root / "result.json").write_bytes(payload)
            first, second = open_reply_directory(roots[0]), open_reply_directory(roots[1])
            try:
                self.assertEqual(read_reply_from_handle(first, 100), b"first")
                self.assertEqual(read_reply_from_handle(second, 100), b"second")
                with self.assertRaises(ReplyGuardError) as error:
                    read_reply_from_handle(first, 4)
                self.assertEqual(error.exception.code, "output_limit")
            finally:
                close_reply_directory(second); close_reply_directory(first)


if __name__ == "__main__":
    unittest.main()
