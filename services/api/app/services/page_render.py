"""Pages drawn as they look in the editor, in headless Chrome, for checking: a picture of the page and the measured
position of every text frame's actual lines.

The HTML follows the rules of apps/web/src/components/magazine (page-view.tsx, text-style.ts): frame geometry, type,
columns, literal text, PressFlow markup, headline shrink-to-fit. Only the browser knows where lines really break, so
a page is judged on this rendering rather than on estimates. Without a Chrome/Edge binary `find_browser` returns None.
"""

from __future__ import annotations

import html
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from app.services.storage import get_storage

SCALE = 2
PREVIEW_STACKS = {
    "Source Serif 4": "'Source Serif 4', Georgia, serif", "Newsreader": "Newsreader, Georgia, serif",
    "Playfair Display": "'Playfair Display', Georgia, serif", "Inter": "Inter, Helvetica, Arial, sans-serif",
    "JetBrains Mono": "'JetBrains Mono', monospace",
}
CLASS_STACK = {"sans": "Inter", "display": "Playfair Display", "mono": "JetBrains Mono"}
PREVIEW_CSS = ("https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Source+Serif+4:ital,wght@"
               "0,400;0,600;0,700;1,400&family=Playfair+Display:wght@400;700&family=Newsreader:ital,wght@0,400;1,400"
               "&family=JetBrains+Mono&display=swap")
CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

# Measures after fonts settle: headlines shrink as in the editor, then each frame reports where its text really is.
MEASURE = """
<script>
document.fonts.ready.then(async () => {
  await new Promise(r => setTimeout(r, 300));
  await document.fonts.ready;
  const report = [];
  const page = document.getElementById('pf-page').getBoundingClientRect();
  document.querySelectorAll('.pf-frame').forEach(n => {
    const fits = () => n.scrollHeight <= n.clientHeight + 1 && n.scrollWidth <= n.clientWidth + 1;
    let shrink = 1;
    if (n.dataset.fit) {
      const size = parseFloat(n.style.fontSize), lead = parseFloat(n.style.lineHeight);
      while (!fits() && shrink > 0.1) {
        shrink *= 0.97;
        n.style.fontSize = size * shrink + 'px';
        n.style.lineHeight = lead * shrink + 'px';
      }
    }
    const range = document.createRange();
    range.selectNodeContents(n);
    const rects = [...range.getClientRects()].filter(r => r.width > 0.5 && r.height > 0.5);
    let ink = null;
    if (rects.length) {
      const s = __SCALE__;
      ink = [Math.min(...rects.map(r => r.left)), Math.min(...rects.map(r => r.top)),
             Math.max(...rects.map(r => r.right)), Math.max(...rects.map(r => r.bottom))]
        .map((v, i) => (v - (i % 2 === 0 ? page.left : page.top)) / s);
    }
    report.push({id: n.dataset.id, fits: fits(), shrink, ink, clientH: n.clientHeight, scrollH: n.scrollHeight,
                 lines: new Set(rects.map(r => Math.round(r.top))).size});
  });
  document.getElementById('pf-report').textContent = JSON.stringify(report);
});
</script>""".replace("__SCALE__", str(SCALE))


def find_browser() -> str | None:
    configured = os.environ.get("CHROME_PATH")
    if configured and Path(configured).exists():
        return configured
    for c in CANDIDATES:
        if Path(c).exists():
            return c
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


@dataclass
class Rendered:
    png: bytes
    frames: dict[str, dict] = field(default_factory=dict)  # frame id -> measurement


def effective_style(profile: dict, el: dict) -> dict:
    typo = profile.get("typography") or {}
    base = typo.get(el.get("styleRole") or "body") or typo.get("body") or {}
    return {**base, **{k: v for k, v in (el.get("style") or {}).items() if v is not None}}


def _font_stack(style: dict) -> str:
    preview = style.get("previewFont") or CLASS_STACK.get(style.get("classification") or "", "Source Serif 4")
    stack = PREVIEW_STACKS.get(preview, PREVIEW_STACKS["Source Serif 4"])
    own = style.get("fontFamily")
    return f"'{own}', {stack}" if own else stack


def _paragraphs(el: dict, s: dict, text: str) -> str:
    out = []
    justify = s.get("align") == "justify"
    gap = el.get("paragraphGap") or 0
    for i, raw in enumerate(p for p in text.split("\n\n")):
        style = f"margin:{gap if i else 0}px 0 0"
        if el.get("literal"):
            sub = raw.startswith("## ")
            body = raw[3:] if sub else raw
            if sub:
                out.append(f'<p style="{style};font-weight:700;text-align:left">{html.escape(body)}</p>')
            elif justify:
                lines = body.split("\n")
                spans = "".join(f'<span style="display:block;text-align-last:{"justify" if j < len(lines) - 1 else "auto"}">'
                                f"{html.escape(ln)}</span>" for j, ln in enumerate(lines))
                out.append(f'<p style="{style}">{spans}</p>')
            else:
                out.append(f'<p style="{style}">{html.escape(body)}</p>')
            continue
        margin = "margin:0"
        if raw.startswith("^ "):
            raw = raw[2:]
            first, rest = raw[:1], raw[1:]
            lead = float(s.get("leading") or 12)
            out.append(f'<p style="{margin};text-indent:0"><span style="float:left;font-weight:700;font-size:{lead * 3.05}px;'
                       f'line-height:{lead * 2.6}px;margin-right:4px;margin-top:2px">{html.escape(first)}</span>'
                       f"{html.escape(rest)}</p>")
        elif raw.startswith("## "):
            out.append(f'<p style="{margin};margin-top:.55em;font-weight:700">{html.escape(raw[3:])}</p>')
        elif raw.startswith("Q: "):
            out.append(f'<p style="{margin};margin-top:.6em;font-weight:700">{html.escape(raw[3:])}</p>')
        elif raw.startswith("> "):
            out.append(f'<p style="margin:4px 0;font-style:italic">{html.escape(raw[2:])}</p>')
        else:
            indent = "text-indent:.9em" if i and (el.get("styleRole") or "body") == "body" else ""
            top = "margin-top:3px" if i and (el.get("styleRole") or "body") != "body" else ""
            out.append(f'<p style="{margin};{indent};{top}">{html.escape(raw)}</p>')
    return "".join(out)


def _toc(entries: list[dict], profile: dict) -> str:
    rule = (profile.get("colors") or {}).get("rule") or "#ccc"
    rows = []
    for e in entries:
        rows.append(f'<div style="display:grid;grid-template-columns:34px minmax(0,1fr);gap:10px;padding:7px 0;'
                    f'border-bottom:.5px solid {rule}"><div style="font-weight:700;font-size:19px;line-height:20px">'
                    f'{int(e["page"]):02d}</div><div><div style="font-size:7.5px;line-height:11px;text-transform:uppercase">'
                    f'{html.escape(e.get("kicker") or "")}</div><div style="font-weight:600;font-size:11.5px;line-height:14px;'
                    f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{html.escape(e["title"])}</div></div></div>')
    return "".join(rows)


def _element(el: dict, profile: dict, assets: dict[str, str], page_number: int, toc: list[dict]) -> str:
    if el.get("hidden"):
        return ""
    rotate = f"transform:rotate({el['rotation']}deg);" if el.get("rotation") else ""
    opacity = f"opacity:{el['opacity']};" if el.get("opacity") is not None else ""
    box = f"position:absolute;left:{el['x']}px;top:{el['y']}px;width:{el['w']}px;height:{el['h']}px;{rotate}{opacity}"
    kind = el.get("kind")
    if kind in ("shape", "rule"):
        radius = f"border-radius:{el['radius']}px;" if el.get("radius") else ""
        return f'<div style="{box}background:{el.get("fill") or "transparent"};{radius}"></div>'
    if kind == "image":
        src = assets.get(el.get("assetId") or "")
        if not src:
            return f'<div style="{box}background:repeating-linear-gradient(45deg,#eef0f3 0 6px,#e4e7ec 6px 12px)"></div>'
        crop = el.get("crop") or {}
        ox, oy = 50 + float(crop.get("offsetX") or 0) * 50, 50 + float(crop.get("offsetY") or 0) * 50
        scale = crop.get("scale") or 1
        return (f'<div style="{box}overflow:hidden"><img src="{html.escape(src)}" style="width:100%;height:100%;'
                f'object-fit:cover;object-position:{ox}% {oy}%;transform:scale({scale});transform-origin:{ox}% {oy}%"></div>')
    s = effective_style(profile, el)
    role = el.get("styleRole") or "body"
    align = s.get("align") or ("justify" if role == "body" else "left")
    s = {**s, "align": align}
    size, leading = float(s.get("size") or 10), float(s.get("leading") or 12)
    columns = int(el.get("columns") or 1)
    css = (f"width:100%;height:100%;overflow:hidden;font-family:{_font_stack(s)};font-size:{size}px;"
           f"line-height:{leading}px;font-weight:{s.get('weight') or 400};letter-spacing:{(s.get('tracking') or 0) / 1000}em;"
           f"color:{s.get('color') or '#000'};text-align:{align};"
           + ("font-style:italic;" if s.get("italic") else "") + ("text-transform:uppercase;" if s.get("uppercase") else "")
           + ("white-space:pre-line;" if el.get("literal") else "")
           + (f"column-count:{columns};column-gap:{el.get('gutter') or (profile.get('grid') or {}).get('gutterPt') or 12}px;"
              if columns > 1 else ""))
    if el.get("dynamic") == "folio":
        content = f'<p style="margin:0;white-space:nowrap">{page_number}</p>'
    elif el.get("dynamic") == "toc":
        content = _toc(toc, profile)
    else:
        content = _paragraphs(el, s, el.get("text") or "")
    fit = ' data-fit="1"' if role == "headline" and columns <= 1 else ""
    return (f'<div style="{box}"><div class="pf-frame" data-id="{html.escape(el["id"])}"{fit} '
            f'style="{css}">{content}</div></div>')


class Renderer:
    """Renders a project's pages. Pictures and fonts are copied out of storage once, into a working folder."""

    def __init__(self, project_id: str, org_id: str, assets: dict[str, dict], profile: dict, browser: str,
                 toc: list[dict] | None = None):
        self.browser, self.profile, self.toc = browser, profile, toc or []
        self.keys = {i: a.get("storageKey") for i, a in assets.items()}
        self.dir = tempfile.TemporaryDirectory(prefix="pf-render-")
        self.root = Path(self.dir.name)
        self.files: dict[str, str] = {}
        self._fonts(project_id, org_id)

    def close(self) -> None:
        self.dir.cleanup()

    def _fonts(self, project_id: str, org_id: str) -> None:
        from app.services.reference_import import fonts_css, fonts_prefix

        storage = get_storage()
        prefix = fonts_prefix(org_id, project_id)
        css = f'@import url("{PREVIEW_CSS}");\n'
        if storage.exists(f"{prefix}/manifest.json"):
            manifest = json.loads(storage.get_bytes(f"{prefix}/manifest.json"))
            for f in manifest.get("faces") or []:
                (self.root / f"{f['id']}.woff").write_bytes(storage.get_bytes(f"{prefix}/{f['id']}.woff"))
            css += fonts_css(manifest, project_id).replace(f"/api/v1/projects/{project_id}/fonts/", "")
        (self.root / "fonts.css").write_text(css, encoding="utf-8")

    def _asset(self, asset_id: str) -> str:
        if asset_id not in self.files:
            key = self.keys.get(asset_id)
            storage = get_storage()
            if not key or not storage.exists(key):
                self.files[asset_id] = ""
            else:
                name = f"{asset_id}{Path(key).suffix}"
                (self.root / name).write_bytes(storage.get_bytes(key))
                self.files[asset_id] = name
        return self.files[asset_id]

    def _html(self, elements: list[dict], page_number: int) -> str:
        W, H = self.profile["page"]["widthPt"], self.profile["page"]["heightPt"]
        assets = {e["assetId"]: self._asset(e["assetId"]) for e in elements if e.get("assetId")}
        paper = (self.profile.get("colors") or {}).get("paper") or "#fff"
        body = "".join(_element(e, self.profile, assets, page_number, self.toc)
                       for e in sorted(elements, key=lambda e: e.get("z", 0)))
        return (f'<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="fonts.css">'
                f"<style>html,body{{margin:0;background:#fff}}</style>"
                f'<div id="pf-page" style="position:relative;width:{W}px;height:{H}px;overflow:hidden;background:{paper};'
                f'transform:scale({SCALE});transform-origin:0 0">{body}</div><pre id="pf-report" hidden></pre>{MEASURE}')

    def _chrome(self, url: str, *args: str) -> subprocess.CompletedProcess:
        W, H = self.profile["page"]["widthPt"], self.profile["page"]["heightPt"]
        with tempfile.TemporaryDirectory(prefix="pf-chrome-") as profile_dir:
            return subprocess.run(
                [self.browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                 "--allow-file-access-from-files", f"--user-data-dir={profile_dir}",
                 *os.environ.get("CHROME_FLAGS", "").split(),  # containers need --no-sandbox
                 f"--window-size={round(W * SCALE)},{round(H * SCALE)}", "--virtual-time-budget=10000", *args, url],
                capture_output=True, timeout=120)

    def render(self, elements: list[dict], page_number: int, key: str) -> Rendered:
        path = self.root / f"page-{key}.html"
        path.write_text(self._html(elements, page_number), encoding="utf-8")
        url = path.as_uri()
        dom = self._chrome(url, "--dump-dom").stdout.decode("utf-8", "replace")
        shot = self.root / f"page-{key}.png"
        self._chrome(url, f"--screenshot={shot}")
        start = dom.find('id="pf-report"')
        frames: list[dict] = []
        if start >= 0:
            text = dom[dom.index(">", start) + 1: dom.index("</pre>", start)]
            try:
                frames = json.loads(html.unescape(text)) if text.strip() else []
            except json.JSONDecodeError:
                frames = []
        return Rendered(shot.read_bytes() if shot.exists() else b"", {f["id"]: f for f in frames})
