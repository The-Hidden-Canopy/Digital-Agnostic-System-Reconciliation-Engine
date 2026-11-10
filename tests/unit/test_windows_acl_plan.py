import os
import ctypes
import tempfile
import unittest
from ctypes import wintypes
from pathlib import Path

from verifier_lab.ingestion.windows_acl_plan import current_token_identity, exchange_acl_plan, precreated_reply_acl_plan, synthetic_acl_exchange, validate_descriptor_syntax
from verifier_lab.ingestion.windows_reply_guard import read_reply

USER = "S-1-5-21-10-20-30-1001"
WORKER = "S-1-15-2-101-102-103-104-105-106-107"
PARENT = "S-1-15-2-201-202-203-204-205-206-207"


class WindowsAclPlanTests(unittest.TestCase):
    def test_precreated_reply_policy_gives_directory_traverse_and_file_write(self):
        plan = precreated_reply_acl_plan(USER, WORKER, PARENT)
        self.assertIn(f"(A;;GX;;;{WORKER})", plan["directory"])
        self.assertIn(f"(A;;GW;;;{WORKER})", plan["reply"])
        self.assertNotIn(f"(A;OICI;GRGWGX;;;{WORKER})", plan["directory"])
        if os.name == "nt":
            self.assertTrue(validate_descriptor_syntax(plan["directory"])["dacl_protected"])
            self.assertTrue(validate_descriptor_syntax(plan["reply"])["sacl_present"])
        with self.assertRaises(ValueError): precreated_reply_acl_plan(USER, WORKER, WORKER)

    def test_plan_separates_worker_traversal_read_and_write(self):
        plan = exchange_acl_plan(USER, WORKER, PARENT)
        self.assertIn(f"(A;;GX;;;{WORKER})", plan["root"])
        self.assertIn(f"(A;OICI;GRGX;;;{WORKER})", plan["input"])
        self.assertIn(f"(A;OICI;GRGWGX;;;{WORKER})", plan["output"])
        self.assertIn("S:(ML;OICI;NW;;;LW)", plan["output"])
        for value in plan.values():
            self.assertTrue(value.startswith("D:P"))
            self.assertIn(f"(A;OICI;FA;;;{PARENT})", value)
            for broad in (";;;WD)", ";;;AU)", ";;;AC)", ";;;BU)"):
                self.assertNotIn(broad, value)

    def test_rejects_sid_injection_broad_groups_and_same_parent(self):
        invalid = ["S-1-1-0", "S-1-15-2-1", WORKER + ")(A;;FA;;;WD", "S-1-15-2-4294967296-2-3-4-5-6-7"]
        for sid in invalid:
            with self.subTest(sid=sid), self.assertRaises(ValueError): exchange_acl_plan(USER, sid)
        with self.assertRaises(ValueError):
            exchange_acl_plan(USER, "S-1-15-2-01-2-3-4-5-6-7", "S-1-15-2-1-2-3-4-5-6-7")
        with self.assertRaises(ValueError): exchange_acl_plan("S-1-5-18", WORKER)

    @unittest.skipUnless(os.name == "nt", "Windows descriptor parser")
    def test_windows_parses_candidate_descriptors_in_memory(self):
        plan = exchange_acl_plan(USER, WORKER, PARENT)
        for name, sddl in plan.items():
            with self.subTest(name=name):
                result = validate_descriptor_syntax(sddl)
                self.assertTrue(result["dacl_present"]); self.assertTrue(result["dacl_protected"])
                self.assertEqual(result["sacl_present"], name == "output")

    @unittest.skipUnless(os.name == "nt", "Windows synthetic exchange")
    def test_candidate_acls_and_file_transport_connect_for_fixed_synthetic_fixture(self):
        result = synthetic_acl_exchange()
        self.assertTrue(result["parser_ok"]); self.assertTrue(result["function_found"])
        self.assertFalse(result["fixture_executed"]); self.assertFalse(result["worker_isolated"])

    @unittest.skipUnless(os.name == "nt", "Windows security descriptors")
    def test_synthetic_directories_receive_protected_candidate_dacls(self):
        class SecurityAttributes(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("descriptor", wintypes.LPVOID), ("inherit", wintypes.BOOL)]
        class AclSize(ctypes.Structure):
            _fields_ = [("count", wintypes.DWORD), ("used", wintypes.DWORD), ("free", wintypes.DWORD)]
        class AceHeader(ctypes.Structure):
            _fields_ = [("type", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("size", wintypes.WORD)]
        class AllowedAce(ctypes.Structure):
            _fields_ = [("header", AceHeader), ("mask", wintypes.DWORD), ("sid_start", wintypes.DWORD)]
        advapi = ctypes.WinDLL("advapi32", use_last_error=True); kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.DWORD)]
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        advapi.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        advapi.GetFileSecurityW.restype = wintypes.BOOL
        advapi.GetSecurityDescriptorControl.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
        advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
        acl_args = [wintypes.LPVOID, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.BOOL)]
        advapi.GetSecurityDescriptorDacl.argtypes = acl_args; advapi.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        advapi.GetSecurityDescriptorSacl.argtypes = acl_args; advapi.GetSecurityDescriptorSacl.restype = wintypes.BOOL
        advapi.GetAclInformation.argtypes = [wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.c_int]; advapi.GetAclInformation.restype = wintypes.BOOL
        advapi.GetAce.argtypes = [wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID)]; advapi.GetAce.restype = wintypes.BOOL
        advapi.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]; advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
        kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes)]; kernel.CreateDirectoryW.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [wintypes.HLOCAL]; kernel.LocalFree.restype = wintypes.HLOCAL

        def create(path, sddl):
            descriptor = wintypes.LPVOID(); size = wintypes.DWORD()
            if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
                if not kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                kernel.LocalFree(descriptor)

        def descriptor_for(path, information):
            size = wintypes.DWORD()
            advapi.GetFileSecurityW(str(path), information, None, 0, ctypes.byref(size))
            if not size.value: raise ctypes.WinError(ctypes.get_last_error())
            descriptor = ctypes.create_string_buffer(size.value)
            if not advapi.GetFileSecurityW(str(path), information, descriptor, size.value, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            control, revision = wintypes.WORD(), wintypes.DWORD()
            if not advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
                raise ctypes.WinError(ctypes.get_last_error())
            present, defaulted, acl = wintypes.BOOL(), wintypes.BOOL(), wintypes.LPVOID()
            getter = advapi.GetSecurityDescriptorDacl if information == 0x4 else advapi.GetSecurityDescriptorSacl
            if not getter(descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)) or not present.value or not acl.value:
                raise AssertionError("expected a non-null ACL")
            size_info = AclSize()
            if not advapi.GetAclInformation(acl, ctypes.byref(size_info), ctypes.sizeof(size_info), 2):
                raise ctypes.WinError(ctypes.get_last_error())
            entries = []
            for index in range(size_info.count):
                raw = wintypes.LPVOID()
                if not advapi.GetAce(acl, index, ctypes.byref(raw)): raise ctypes.WinError(ctypes.get_last_error())
                ace = ctypes.cast(raw, ctypes.POINTER(AllowedAce)).contents
                sid_ptr = wintypes.LPVOID(raw.value + AllowedAce.sid_start.offset); sid = wintypes.LPWSTR()
                if not advapi.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid)): raise ctypes.WinError(ctypes.get_last_error())
                try: entries.append((sid.value, ace.mask, ace.header.type, ace.header.flags))
                finally: kernel.LocalFree(ctypes.cast(sid, wintypes.HLOCAL))
            return control.value, entries

        identity = current_token_identity()
        plan = exchange_acl_plan(identity["user_sid"], WORKER, identity["appcontainer_sid"])
        with tempfile.TemporaryDirectory(prefix="elucidary-acl-fixture-") as tmp:
            root = Path(tmp) / "exchange"
            create(root, plan["root"])
            create(root / "input", plan["input"])
            create(root / "output", plan["output"])
            expected_sids = {"S-1-5-18", identity["user_sid"], WORKER}
            if identity["appcontainer_sid"]: expected_sids.add(identity["appcontainer_sid"])
            # The filesystem maps generic rights to file-specific access masks on creation.
            for folder, expected_worker_mask in ((root, 0x1200a0), (root / "input", 0x1200a9), (root / "output", 0x1201bf)):
                control, entries = descriptor_for(folder, 0x4)  # DACL_SECURITY_INFORMATION
                self.assertTrue(control & 0x0004); self.assertTrue(control & 0x1000)
                self.assertEqual({entry[0] for entry in entries}, expected_sids)
                self.assertTrue(all(entry[2] == 0 and not entry[3] & 0x10 for entry in entries))
                self.assertEqual(next(entry[1] for entry in entries if entry[0] == WORKER), expected_worker_mask)
            label_control, labels = descriptor_for(root / "output", 0x10)  # LABEL_SECURITY_INFORMATION
            self.assertTrue(label_control & 0x0010)
            self.assertIn(("S-1-16-4096", 1, 0x11), [(sid, mask, typ) for sid, mask, typ, flags in labels])
            (root / "input" / "synthetic.py").write_bytes(b"def f(): pass\n")
            (root / "output" / "result.json").write_bytes(b"{}")
            self.assertEqual(read_reply(root / "output", 2), b"{}")


if __name__ == "__main__":
    unittest.main()
