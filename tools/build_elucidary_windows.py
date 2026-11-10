"""Create a local, self-contained Windows desktop folder; does not publish or sign."""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("esprima", "tree_sitter", "tree_sitter_typescript", "tree_sitter_cpp")
DIST_PREFIXES = ("esprima-", "tree_sitter-", "tree_sitter_typescript-", "tree_sitter_cpp-")
BOOTSTRAP = """from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / 'app'))
from verifier_lab.elucidary_desktop import main
raise SystemExit(main())
"""


def build(output: Path):
    if sys.platform != "win32":
        raise ValueError("This builder requires a Windows Python runtime")
    output = output.absolute()
    if output.exists():
        raise ValueError("Refusing to overwrite an existing output path")
    compiler = shutil.which("gcc")
    if not compiler:
        raise ValueError("A Windows gcc compiler is required for the desktop launcher")
    base = Path(sys.base_prefix)
    package_dirs = []
    for name in PACKAGES:
        spec = importlib.util.find_spec(name)
        if not spec or not spec.submodule_search_locations:
            raise ValueError(f"Required local package is unavailable: {name}")
        package_dirs.append(Path(next(iter(spec.submodule_search_locations))))
    output.mkdir(parents=True)
    runtime = output / "runtime"; runtime.mkdir()
    runtime_dll = f"python{sys.version_info.major}{sys.version_info.minor}.dll"
    for name in ("python.exe", "pythonw.exe", runtime_dll, "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt"):
        source = base / name
        if source.exists(): shutil.copy2(source, runtime / name)
    shutil.copytree(base / "DLLs", runtime / "DLLs", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(base / "Lib", runtime / "Lib", ignore=shutil.ignore_patterns("site-packages", "__pycache__", "*.pyc", "test", "tests"))
    shutil.copytree(base / "tcl", runtime / "tcl", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    site = runtime / "Lib" / "site-packages"; site.mkdir()
    for source in package_dirs:
        shutil.copytree(source, site / source.name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "test", "tests"))
    for source in {p.parent for p in package_dirs}:
        for entry in source.iterdir():
            if entry.is_dir() and entry.name.endswith(".dist-info") and entry.name.startswith(DIST_PREFIXES):
                shutil.copytree(entry, site / entry.name, dirs_exist_ok=True)
    app = output / "app"; app.mkdir()
    shutil.copytree(ROOT / "src" / "verifier_lab", app / "verifier_lab",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "server.py", "cli.py", "web", "file_exchange.py", "graph_reply_schema.py", "windows_reply_guard.py", "windows_acl_plan.py"))
    shutil.copy2(ROOT / "LICENSE", output / "LICENSE")
    (output / "run.pyw").write_text(BOOTSTRAP, encoding="utf-8")
    subprocess.run([compiler, "-O2", "-static", "-municode", "-mwindows", str(ROOT / "tools" / "elucidary_windows_launcher.c"),
                    "-o", str(output / "Elucidary.exe")], check=True)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(build(args.output))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Desktop build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
