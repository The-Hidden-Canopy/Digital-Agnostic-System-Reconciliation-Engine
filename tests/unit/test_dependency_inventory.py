"""The documented dependency inventory must match what the release tooling actually pins."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def inventory() -> dict[str, str]:
    """name (lower case) -> version column of the RELEASE.md dependency table."""
    rows = {}
    for line in read("docs/RELEASE.md").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and not line.startswith("|---") and cells[0] != "Dependency":
            rows[cells[0].lower()] = cells[2]
    return rows


def python_pins() -> dict[str, tuple[str, bool]]:
    pins = {}
    for rel in ("tools/release-build-requirements.txt", "tools/release-pdf-requirements.txt"):
        for entry in re.split(r"\n(?=[A-Za-z0-9_.-]+==)", read(rel).strip()):
            m = re.match(r"([A-Za-z0-9_.-]+)==([0-9][0-9A-Za-z.]*)", entry)
            if m:
                pins[m.group(1).lower()] = (m.group(2), "--hash=sha256:" in entry)
    return pins


class DependencyInventory(unittest.TestCase):
    def test_every_python_pin_has_a_hash_and_matches_the_inventory(self):
        pins, table = python_pins(), inventory()
        self.assertEqual(set(pins), {"setuptools", "wheel", "pypdf"})
        for name, (version, hashed) in pins.items():
            self.assertTrue(hashed, f"{name} is not hash-pinned")
            self.assertEqual(table.get(name), version, f"{name}: RELEASE.md differs from the requirement file")

    def test_browser_test_tools_match_the_workflow(self):
        table, ci = inventory(), read(".github/workflows/ci.yml")
        for name in ("playwright", "axe-core"):
            versions = set(re.findall(rf"{name}@([0-9.]+)", ci))
            self.assertEqual(versions, {table[name]}, name)

    def test_shipped_package_has_no_required_runtime_dependency(self):
        pyproject = read("pyproject.toml")
        self.assertRegex(pyproject, r"(?m)^dependencies = \[\]\s*$")
        self.assertEqual(re.findall(r'(?m)^pdf = \["([^"]+)"\]', pyproject), ["pypdf>=5"])

    def test_inventory_states_its_review_limits(self):
        text = read("docs/RELEASE.md")
        self.assertIn("not a completed provenance, notice, vulnerability or legal review", text)


if __name__ == "__main__":
    unittest.main()
