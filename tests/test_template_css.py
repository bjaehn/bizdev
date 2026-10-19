import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "bd" / "ui" / "templates"
HEADER_CLASSES = ("bar", "brand", "right", "adminlink", "adminnav", "shell", "top", "area", "hero", "tiles", "tile")


def test_no_page_restyles_the_classes_the_page_header_is_built_from():
    """A page's own <style> once defined `.bar` (a score bar), which collapsed the header on that page. The shared layout owns these class
    names; a page must use its own."""
    bad = []
    for path in sorted(TEMPLATES.glob("*.html")):
        if path.name == "base.html":
            continue
        for css in re.findall(r"<style[^>]*>(.*?)</style>", path.read_text(), re.S):
            for sel in re.findall(r"(?:^|[},])\s*([^{}@]+?)\s*\{", css):
                for part in sel.split(","):
                    for cls in HEADER_CLASSES:
                        if re.search(rf"\.{cls}(?![\w-])", part):
                            bad.append(f"{path.name}: {part.strip()}")
    assert not bad, bad
