"""WCAG 2.1 AA contrast for the GUI panel palette (tanglebrain/gui/static/index.html).

#115 asked for the panel's contrast ratios to be measured. A measurement is a fact about one
afternoon, so this module asserts them instead: the palette is parsed out of the stylesheet at test
time and every pair a reader actually sees is checked against the ratio its content type requires.
Changing a colour in `index.html` reds this suite and names the pair that broke.

Two structural assertions keep the pair table from silently falling behind the stylesheet, which is
the failure mode a hand-written table like this normally has:

* every colour token declared in ``:root`` must appear in at least one checked pair, so adding a
  token without measuring it fails; and
* every colour-bearing declaration outside ``:root`` must go through ``var(--token)``, so a colour
  cannot be introduced somewhere this module is not looking.

**What they do not cover, stated because the alternative is a false sense of coverage:** a new
*pairing* of two tokens that both already exist. Putting ``--danger`` on a surface no row names
leaves the suite green. :data:`PAIRS` is a hand-maintained model of which token is read against
which surface, and only reading the stylesheet keeps it true — deriving the pairs would mean
resolving CSS cascade and DOM nesting, which is a great deal of machinery to protect a 60-line
stylesheet. **If you add a rule that puts an existing colour on a new surface, add its row.**

What is deliberately *not* asserted, with the specific WCAG text that exempts it, is in
:data:`EXEMPT` — recorded rather than left as a silent gap, per the issue's own scope note.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

PANEL = Path(__file__).resolve().parents[1] / "tanglebrain" / "gui" / "static" / "index.html"

#: Any CSS colour syntax, so a token written as rgba()/hsl()/a bare name is *seen* rather than
#: silently skipped by a hex-only pattern and left unmeasured.
COLOUR_LITERAL = re.compile(
    r"#[0-9a-fA-F]{3,8}|(?:rgba?|hsla?|color|oklch|lab)\([^)]*\)|[a-z]{3,20}", re.IGNORECASE
)

# WCAG 2.1 thresholds. 1.4.3 Contrast (Minimum) for text, 1.4.11 Non-text Contrast for the visual
# information required to identify a control.
NORMAL_TEXT = 4.5
LARGE_TEXT = 3.0
UI_COMPONENT = 3.0


def relative_luminance(hex_colour: str) -> float:
    """Return the WCAG relative luminance of a hex colour.

    Args:
        hex_colour: ``#RGB`` or ``#RRGGBB``, with or without the leading ``#``.

    Returns:
        Relative luminance in ``[0, 1]``, per WCAG 2.1's definition.
    """
    raw = hex_colour.lstrip("#")
    if len(raw) == 3:
        raw = "".join(c * 2 for c in raw)
    channels = [int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(fg: str, bg: str) -> float:
    """Return the WCAG contrast ratio between two hex colours (1.0 to 21.0)."""
    a, b = relative_luminance(fg), relative_luminance(bg)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


def load_palette() -> dict[str, str]:
    """Parse the ``--token: #hex`` custom properties out of the panel's ``:root`` block.

    Returns:
        Token name (without the leading ``--``) mapped to its hex value.

    Raises:
        AssertionError: If the ``:root`` block cannot be located.
    """
    css = PANEL.read_text(encoding="utf-8")
    root = re.search(r":root\s*\{(.*?)\}", css, re.DOTALL)
    assert root, f"no :root block found in {PANEL}"
    declarations = re.findall(r"--([\w-]+):\s*([^;}]+)", root.group(1))
    palette = {}
    for name, value in declarations:
        value = value.strip()
        if not COLOUR_LITERAL.fullmatch(value):
            continue
        assert value.startswith("#"), (
            f"--{name} is {value!r}; this module only computes ratios for hex colours. "
            "Convert it to hex, or teach relative_luminance() the other syntax."
        )
        palette[name] = value
    return palette


#: (description, foreground token, background token, required ratio). One row per pair a reader
#: actually sees. Where one colour appears on several surfaces it gets a row per surface: on this
#: dark ground the *lightest* surface is the limiting one, and it is not always the obvious one —
#: --text-muted failed on the elevated tiles while passing on the page behind them.
PAIRS = [
    ("body text on the page", "text", "bg", NORMAL_TEXT),
    ("body text inside a card", "text", "card-bg", NORMAL_TEXT),
    ("text typed into a field", "text", "elevated-bg", NORMAL_TEXT),
    ("header subtitle (muted)", "text-muted", "bg", NORMAL_TEXT),
    ("section heading (muted)", "text-muted", "bg", NORMAL_TEXT),
    ("checkbox label (muted)", "text-muted", "card-bg", NORMAL_TEXT),
    ("table column header (muted)", "text-muted", "card-bg", NORMAL_TEXT),
    ("served-by line and .muted text", "text-muted", "card-bg", NORMAL_TEXT),
    ("stat label (muted, on the elevated tile)", "text-muted", "elevated-bg", NORMAL_TEXT),
    # header h1 is 1.5rem = 24px = 18pt exactly, which is the large-scale threshold. Shrink it
    # and this row needs NORMAL_TEXT.
    ("wordmark accent", "primary", "bg", LARGE_TEXT),
    # 1.3rem = 20.8px at normal weight, which is NOT WCAG large-scale text (that starts at
    # 24px, or 18.67px bold). Asserting 3:1 here would have passed a sub-AA palette.
    ("big stat value", "primary", "elevated-bg", NORMAL_TEXT),
    ("served-by backend name", "primary", "card-bg", NORMAL_TEXT),
    ("button label at rest", "primary", "card-bg", NORMAL_TEXT),
    ("button label on hover", "primary", "elevated-bg", NORMAL_TEXT),
    ("local / orchestrator pill", "primary", "card-bg", NORMAL_TEXT),
    ("subscription pill", "primary-bright", "card-bg", NORMAL_TEXT),
    ("paid-api pill", "amber", "card-bg", NORMAL_TEXT),
    ("cost caveat", "amber", "card-bg", NORMAL_TEXT),
    ("error text in a card", "danger", "card-bg", NORMAL_TEXT),
    ("error text in the output pane", "danger", "elevated-bg", NORMAL_TEXT),
    ("enabled button outline", "primary-dark", "card-bg", UI_COMPONENT),
    ("button outline on hover", "primary-dark", "elevated-bg", UI_COMPONENT),
    ("field outline at rest", "field-border", "elevated-bg", UI_COMPONENT),
    ("field outline, focused", "primary-bright", "elevated-bg", UI_COMPONENT),
    # A focus ring is only a focus ring if it reads as a *change*. Raising the rest-state outline
    # to meet 1.4.11 is what made this pair worth asserting: the old focus colour sat at 1.40:1
    # against the brighter rest state, which would have satisfied 1.4.11 while quietly failing
    # 2.4.7 Focus Visible.
    ("focused vs. unfocused field outline", "primary-bright", "field-border", UI_COMPONENT),
]

#: Pairs deliberately not asserted, each with the WCAG text that exempts it. Recorded here rather
#: than omitted, because an unexplained absence from the table above is indistinguishable from an
#: oversight — and "disabled and muted states are the usual excuse" is #115's own warning.
EXEMPT = {
    "border": (
        "Decorative only — card edges, table rules and pill outlines. SC 1.4.11 governs the visual "
        "information required to *identify a user interface component or state*; none of these "
        "identify anything, and the content they enclose carries its own contrast. Raising them "
        "would restyle the panel without helping anyone read it."
    ),
    "disabled button label and outline": (
        "SC 1.4.3 and 1.4.11 both exempt inactive components explicitly. Asserted nowhere, but "
        "worth recording that it passes anyway: the disabled label uses --text-muted, which clears "
        "4.5:1 on every surface it appears on."
    ),
}


class PaletteParsingTest(unittest.TestCase):
    """The palette is read from the stylesheet, so these tests cannot drift from what ships."""

    def test_every_token_used_by_a_pair_exists_in_the_stylesheet(self):
        palette = load_palette()
        for _, fg, bg, _ in PAIRS:
            for token in (fg, bg):
                with self.subTest(token=token):
                    self.assertIn(token, palette, f"--{token} is asserted below but not declared")

    def test_every_declared_token_is_measured_or_explicitly_exempt(self):
        # Without this, adding a colour to :root would ship an unmeasured pair and the suite would
        # stay green — the exact way a hand-written table like PAIRS goes stale.
        measured = {token for _, fg, bg, _ in PAIRS for token in (fg, bg)}
        for token in load_palette():
            with self.subTest(token=token):
                self.assertTrue(
                    token in measured or token in EXEMPT,
                    f"--{token} is declared but neither measured in PAIRS nor listed in EXEMPT",
                )

    def test_no_colour_literal_outside_the_root_block(self):
        # PAIRS reasons about tokens, so a hard-coded colour further down the stylesheet would be
        # invisible to every assertion in this module.
        css = PANEL.read_text(encoding="utf-8")
        root = re.search(r":root\s*\{.*?\}", css, re.DOTALL)
        assert root
        outside = css[: root.start()] + css[root.end() :]
        # Strip comments first: the rationale comments in this stylesheet name colours in prose.
        outside = re.sub(r"/\*.*?\*/", "", outside, flags=re.DOTALL)
        # Every declaration whose *property* is colour-bearing must be a var() reference. Checking
        # the property rather than hunting colour syntaxes is what makes this total: rgba(), hsl()
        # and bare CSS names like `white` all fail it, and a new syntax needs no new pattern.
        offenders = [
            f"{prop}: {value.strip()}"
            for prop, value in re.findall(
                r"\b(color|background|background-color|border|border-color|border-top|"
                r"border-left|outline|outline-color|box-shadow|fill|stroke)\s*:\s*([^;{}]+)",
                outside,
            )
            if "var(" not in value and value.strip() not in {"none", "inherit", "transparent"}
        ]
        self.assertEqual(
            [], offenders,
            "colour-bearing declarations outside :root must use var(--token) so the contrast "
            "assertions can see them",
        )


class ContrastTest(unittest.TestCase):
    """Every pair a reader sees meets WCAG 2.1 AA."""

    def test_all_pairs_meet_aa(self):
        palette = load_palette()
        for label, fg, bg, required in PAIRS:
            with self.subTest(pair=label):
                ratio = contrast_ratio(palette[fg], palette[bg])
                self.assertGreaterEqual(
                    round(ratio, 2), required,
                    f"{label}: --{fg} ({palette[fg]}) on --{bg} ({palette[bg]}) is "
                    f"{ratio:.2f}:1, below the {required}:1 WCAG 2.1 AA requires",
                )

    def test_the_ratio_maths_matches_the_wcag_worked_examples(self):
        # Pins the formula itself, so a regression here cannot quietly pass the palette.
        self.assertAlmostEqual(contrast_ratio("#FFFFFF", "#000000"), 21.0, places=2)
        self.assertAlmostEqual(contrast_ratio("#000000", "#000000"), 1.0, places=2)
        self.assertAlmostEqual(contrast_ratio("#777777", "#000000"), 4.69, places=2)
        # Order must not matter: the ratio is defined on lighter/darker, not fg/bg.
        self.assertEqual(contrast_ratio("#8BC34A", "#000000"), contrast_ratio("#000000", "#8BC34A"))


if __name__ == "__main__":
    unittest.main()
