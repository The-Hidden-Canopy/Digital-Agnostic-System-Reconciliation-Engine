"""WCAG 2.x AA contrast for every text/background token pairing the workbench stylesheet uses.

axe-core leaves colour contrast "incomplete" where backgrounds are layered or elements are pre-formatted;
this computes the ratios straight from the design tokens, so the pairs are decided rather than queued for review.
"""
import re
import unittest
from pathlib import Path

CSS = (Path(__file__).resolve().parents[2] / "src" / "verifier_lab" / "web" / "style.css").read_text(encoding="utf-8")

# (foreground token, background token) pairs that appear as text on a surface in style.css / app.js
TEXT_ON = [
    ("fg", "bg"), ("fg", "card"), ("fg", "infobg"), ("fg", "neutralbg"), ("fg", "mark"),
    ("muted", "bg"), ("muted", "card"), ("muted", "infobg"),
    ("accent", "bg"), ("accent", "card"), ("accent-fg", "accent"),
    ("ok", "okbg"), ("bad", "badbg"), ("warn", "warnbg"), ("info", "infobg"), ("neutral", "neutralbg"),
    ("fg", "okbg"), ("fg", "badbg"), ("fg", "warnbg"),
    ("bad", "card"), ("ok", "card"), ("warn", "card"),
]


def tokens(block: str) -> dict[str, str]:
    out = {}
    for name, value in re.findall(r"--([a-z-]+):(#[0-9a-fA-F]{3,6})(?![0-9a-fA-F])", block):
        if len(value) == 4:
            value = "#" + "".join(c * 2 for c in value[1:])
        out[name] = value
    return out


def themes() -> dict[str, dict[str, str]]:
    root = re.search(r":root\{(.*?)\}\s*@media", CSS, re.S).group(1)
    dark = re.search(r"prefers-color-scheme:dark\)\{:root\{(.*?)\}\}", CSS, re.S).group(1)
    light = tokens(root)
    return {"light": light, "dark": {**light, **tokens(dark)}}


def luminance(hex_color: str) -> float:
    def lin(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def ratio(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class Contrast(unittest.TestCase):
    def test_reference_values(self):
        self.assertAlmostEqual(ratio("#000000", "#ffffff"), 21.0, places=1)
        self.assertAlmostEqual(ratio("#777777", "#ffffff"), 4.48, places=1)

    def test_both_themes_define_every_token(self):
        t = themes()
        for name, pairs in t.items():
            for fg, bg in TEXT_ON:
                self.assertIn(fg, pairs, (name, fg))
                self.assertIn(bg, pairs, (name, bg))

    def test_every_text_pairing_meets_aa_in_both_themes(self):
        failures = []
        for name, t in themes().items():
            for fg, bg in TEXT_ON:
                r = ratio(t[fg], t[bg])
                if r < 4.5:
                    failures.append(f"{name}: {fg} {t[fg]} on {bg} {t[bg]} = {r:.2f}")
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
