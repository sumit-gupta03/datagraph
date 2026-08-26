"""Render the README's Mermaid diagrams to PNG and build a PyPI-friendly README.

Why this exists: GitHub renders ```mermaid fences natively, PyPI does not - it shows the raw
`flowchart LR ...` source, which is unreadable. So:

    README.md        keeps the Mermaid source     -> GitHub renders live diagrams
    README.pypi.md   swaps each block for a PNG   -> PyPI shows pictures  (pyproject: readme=...)
    docs/images/diagram-*.png   the rendered images, committed and served from raw.githubusercontent

Run after changing any diagram:

    python scripts/build_pypi_readme.py            # re-render + rebuild
    python scripts/build_pypi_readme.py --check    # fail if README.pypi.md is stale (for CI)

Rendering uses local Chrome/Edge headless with mermaid from a CDN - network is needed only when
re-rendering, never at install or view time.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
PYPI_README = ROOT / "README.pypi.md"
IMAGE_DIR = ROOT / "docs" / "images"
RAW_BASE = "https://raw.githubusercontent.com/sumit-gupta03/datagraph/main/docs/images/"

BLOCK = re.compile(r"```mermaid\n(.*?)```\n", re.S)

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<style>
  html,body{margin:0;padding:0;background:#ffffff}
  #wrap{display:inline-block;padding:18px}
  .mermaid{font-family:"Segoe UI",system-ui,sans-serif}
</style></head><body>
<div id="wrap"><pre class="mermaid">__SRC__</pre></div>
<script>
  mermaid.initialize({startOnLoad:true, theme:"neutral", flowchart:{useMaxWidth:false}});
</script></body></html>
"""


def _browser() -> str:
    for candidate in BROWSERS:
        if Path(candidate).exists():
            return candidate
    sys.exit("no Chrome/Edge found - install one, or edit BROWSERS in this script")


def render(source: str, out: Path) -> None:
    """Render one Mermaid diagram to a trimmed PNG."""
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "d.html"
        page.write_text(PAGE.replace("__SRC__", source.strip()), encoding="utf-8")
        shot = Path(tmp) / "shot.png"
        subprocess.run(
            [_browser(), "--headless", "--disable-gpu", "--hide-scrollbars",
             "--virtual-time-budget=15000", "--window-size=1800,1400",
             f"--screenshot={shot}", page.as_uri()],
            check=True, capture_output=True, timeout=180,
        )
        data = shot.read_bytes()
    try:                                   # trim the empty canvas when Pillow is available
        from io import BytesIO

        from PIL import Image

        image = Image.open(BytesIO(data)).convert("RGB")
        white = Image.new("RGB", image.size, (255, 255, 255))
        from PIL import ImageChops

        box = ImageChops.difference(image, white).getbbox()
        if box:
            pad = 12
            box = (max(0, box[0] - pad), max(0, box[1] - pad),
                   min(image.width, box[2] + pad), min(image.height, box[3] + pad))
            image = image.crop(box)
        image.save(out, "PNG", optimize=True)
        return
    except ImportError:
        out.write_bytes(data)


def build(check: bool = False) -> int:
    text = README.read_text(encoding="utf-8")
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    blocks = BLOCK.findall(text)
    if not blocks:
        print("no mermaid blocks found")
        return 0

    names = []
    for index, source in enumerate(blocks, start=1):
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:8]
        name = f"diagram-{index}-{digest}.png"
        names.append(name)
        target = IMAGE_DIR / name
        if target.exists():
            print(f"  {name}: unchanged")
            continue
        if check:
            print(f"  {name}: MISSING (diagram changed - re-run without --check)")
            return 1
        print(f"  {name}: rendering ...")
        render(source, target)
        for stale in IMAGE_DIR.glob(f"diagram-{index}-*.png"):   # drop the previous hash
            if stale.name != name:
                stale.unlink()

    def swap(match: re.Match) -> str:
        swap.i += 1                                              # type: ignore[attr-defined]
        alt = blocks[swap.i - 1].strip().splitlines()[0].strip()  # type: ignore[attr-defined]
        return f"![{alt}]({RAW_BASE}{names[swap.i - 1]})\n\n"     # type: ignore[attr-defined]

    swap.i = 0                                                    # type: ignore[attr-defined]
    pypi = BLOCK.sub(swap, text)
    pypi = ("<!-- Generated from README.md by scripts/build_pypi_readme.py - do not edit. "
            "Mermaid diagrams are replaced by images because PyPI does not render Mermaid. -->\n\n") + pypi

    if check:
        current = PYPI_README.read_text(encoding="utf-8") if PYPI_README.exists() else ""
        if current != pypi:
            print("README.pypi.md is stale - run: python scripts/build_pypi_readme.py")
            return 1
        print("README.pypi.md is up to date")
        return 0

    PYPI_README.write_text(pypi, encoding="utf-8")
    print(f"wrote {PYPI_README.relative_to(ROOT)} ({len(blocks)} diagram(s) -> images)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify the generated README is current")
    raise SystemExit(build(parser.parse_args().check))
