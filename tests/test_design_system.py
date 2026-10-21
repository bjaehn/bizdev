"""The design system is defined once, in bd/ui/templates/design.css, and applied globally.

These tests keep it that way: templates carry no styling of their own, every class they use is a documented component, and the
stylesheet itself is built from tokens."""

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parent.parent / "bd" / "ui" / "templates"
CSS = (TEMPLATES / "design.css").read_text()
HTML = sorted(TEMPLATES.glob("*.html"))
JINJA_WORDS = {"if", "else", "elif", "endif", "in", "not", "and", "or", "is", "none", "true", "false"}


def classes_used(text: str) -> set[str]:
    """Class names a template can emit: the static words of class="..." plus the values a {{ 'a' if x else 'b' }} expression can produce."""
    out: set[str] = set()
    for m in re.finditer(r'\bclass="([^"]*)"', text):
        value = m.group(1)
        for expr in re.findall(r"\{\{(.*?)\}\}", value, re.S):
            out |= {w for q in re.findall(r"'([^']*)'\s+if\b", expr) for w in q.split()}
            out |= {w for q in re.findall(r"\belse\s+\(?\s*'([^']*)'", expr) for w in q.split()}
        static = re.sub(r"\{%.*?%\}", " ", re.sub(r"\{\{.*?\}\}", " ", value, flags=re.S), flags=re.S)
        out |= set(static.split())
    return {c for c in out if re.fullmatch(r"[A-Za-z][\w-]*", c) and c not in JINJA_WORDS}


def defined_classes() -> set[str]:
    return set(re.findall(r"\.([A-Za-z][\w-]*)", re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)))


def test_templates_carry_no_style_blocks():
    bad = [p.name for p in HTML if "<style" in p.read_text() and p.name != "base.html"]
    assert not bad, f"style blocks belong in design.css: {bad}"
    base = (TEMPLATES / "base.html").read_text()
    assert base.count("<style") == 1 and '{% include "design.css" %}' in base


def test_templates_carry_no_style_attributes_except_a_data_value_passed_as_a_custom_property():
    bad = []
    for p in HTML:
        for m in re.finditer(r'\bstyle="([^"]*)"', p.read_text()):
            if not re.fullmatch(r"--[a-z]+:[^;]+", m.group(1).strip()):
                bad.append(f"{p.name}: {m.group(1)[:70]}")
    assert not bad, bad


def test_every_class_a_template_uses_is_a_component_defined_in_the_design_system():
    defined = defined_classes()
    missing = {}
    for p in HTML:
        for c in classes_used(p.read_text()) - defined:
            missing.setdefault(c, []).append(p.name)
    assert not missing, f"add these to design.css (or use an existing component): {missing}"


def test_colours_are_tokens_hex_only_in_the_two_token_blocks():
    body = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    tokens = re.search(r"/\* ---------- 1\. tokens ---------- \*/(.*?)/\* ---------- 2\. base", CSS, re.S).group(1)
    rest = body.replace(re.sub(r"/\*.*?\*/", "", tokens, flags=re.S), "")
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", rest), re.findall(r"#[0-9a-fA-F]{3,8}\b", rest)
    assert tokens.count(":root") == 2 and "prefers-color-scheme:dark" in tokens


DEPTH_TOKENS = ("--shadow", "--shadow-2", "--shadow-3", "--hl", "--inset", "--inset-top", "--sheen", "--row-alt", "--glow", "--shadow-btn", "--panel-2", "--bg-glow",
                "--line-soft", "--accent-2", "--hot-2", "--warm-2", "--good-2")


def test_every_depth_token_is_defined_for_light_and_again_for_dark():
    """Depth is drawn from tokens, so dark mode only works if each one is redefined there (a light shadow on a dark page is invisible)."""
    tokens = re.search(r"/\* ---------- 1\. tokens ---------- \*/(.*?)/\* ---------- 2\. base", CSS, re.S).group(1)
    light, dark = tokens.split("@media (prefers-color-scheme:dark)")
    for t in DEPTH_TOKENS:
        assert re.search(re.escape(t) + r":", light), f"{t} is not defined for light"
        assert re.search(re.escape(t) + r":", dark), f"{t} is not redefined for dark"


def test_charts_tables_and_cells_use_the_depth_tokens_and_print_removes_them():
    for needle in (".chart{", "var(--g-well)", "tr:has(th)+tr td", "var(--row-alt)", ".mark:hover", ".cell{", "var(--sheen)"):
        assert needle in CSS, needle
    printed = CSS[CSS.index("/* ---------- 7. print"):]
    assert ".tile,.stat,.chip{" in printed and "box-shadow:none" in printed


def test_the_stylesheet_covers_light_dark_phone_and_print_and_is_documented():
    for needle in ("max-width:640px", "@media print", "/* ---------- 3. layout", "/* ---------- 4. components", "THE ONLY PLACE STYLING IS DEFINED"):
        assert needle in CSS, needle


def test_a_new_page_style_would_be_caught(tmp_path):
    """The checks above fail on a page that brings its own CSS (so the guard is not vacuous)."""
    sample = '<style>.x{color:red}</style><p class="made-up" style="margin:0">hi</p>'
    assert "<style" in sample and "made-up" in classes_used(sample) and "made-up" not in defined_classes()
    assert not re.fullmatch(r"--[a-z]+:[^;]+", "margin:0")


def test_the_rules_are_recorded_in_project_memory():
    memory = (TEMPLATES.parents[2] / "CLAUDE.md").read_text()
    for needle in ("design.css", "No per-page CSS", "Tokens only", "tests/test_design_system.py", "_ui.html"):
        assert needle in memory, needle


@pytest.mark.parametrize("path", ["/opportunities", "/market", "/reports", "/help", "/admin"])
def test_pages_render_with_the_stylesheet_inlined_once_and_no_script(app_env, path):
    body = app_env.get(path).body
    assert body.count("<style>") == 1 and "<script" not in body and "THE ONLY PLACE STYLING IS DEFINED" in body


from tests.test_update import app_env  # noqa: E402,F401  (fixture)
