#!/usr/bin/env python3
"""Build the profile README's images and refresh its feed sections.

Run from anywhere:  python scripts/build.py

- Draws assets/*.svg (banner, project cards, video cards) in a light and a dark
  version. All text is converted to outlines, because GitHub shows SVGs as
  images and images can't load web fonts.
- Rewrites the parts of README.md between <!-- VIDEOS:START/END --> and
  <!-- WRITING:START/END --> from the YouTube and Medium feeds.

If a feed or the GitHub API can't be reached, the parts that depend on it are
left as they were, so a failed run never blanks the profile.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html import escape
from pathlib import Path

import uharfbuzz as hb
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
ASSETS = ROOT / "assets"
README = ROOT / "README.md"
UA = {"User-Agent": "profile-readme-builder/1.0"}

THEMES = {
    "light": {
        "bg": "#ffffff", "card": "#f6f8fa", "border": "#d1d9e0", "fg": "#1f2328", "muted": "#59636e",
        "accent": "#0b857a", "accent_soft": "#e2f1ef",
        "tok": ["#dbe8fd", "#d8f3ea", "#fde6d2", "#ebe3fb"], "tok_fg": "#1d2433",
        "wordmark": "dowse-wordmark.svg",
    },
    "dark": {
        "bg": "#0d1117", "card": "#151b23", "border": "#3d444d", "fg": "#f0f6fc", "muted": "#9198a1",
        "accent": "#3fcabd", "accent_soft": "#12302d",
        "tok": ["#1f3354", "#163d35", "#45301c", "#33284f"], "tok_fg": "#e6ebf5",
        "wordmark": "dowse-wordmark-dark.svg",
    },
}
# Colours inside the small photo thumbnails read as photos in either theme.
PHOTO = {"sky": "#8fb3d1", "tram": "#f0c341", "street": "#c9ced8", "park": "#5c8f6b", "roof": "#d77a52", "paper": "#eef1f5", "ink": "#a3acba"}

REDUCED_MOTION = "@media (prefers-reduced-motion:reduce){*{animation:none!important}}"
# Every image gets a transparent margin, so two images placed at 50% width sit
# with an even gutter and line up with the full-width images above them.
PAD_X, PAD_Y = 5, 3


# ---------------------------------------------------------------- text to outlines

class Face:
    """One static font: shapes text with HarfBuzz and hands out glyph outlines."""

    def __init__(self, filename: str, prefix: str):
        path = SCRIPTS / "fonts" / filename
        self.tt = TTFont(path)
        self.upem = self.tt["head"].unitsPerEm
        self.hb = hb.Font(hb.Face(hb.Blob.from_file_path(str(path))))
        self.glyphset = self.tt.getGlyphSet()
        self.prefix = prefix
        self._paths: dict[int, str] = {}

    def shape(self, text: str):
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(self.hb, buf, {"kern": True, "liga": True})
        glyphs, x = [], 0
        for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
            glyphs.append((info.codepoint, x + pos.x_offset, pos.y_offset))
            x += pos.x_advance
        return glyphs, x

    def width(self, text: str, size: float, tracking: float = 0) -> float:
        glyphs, adv = self.shape(text)
        return adv * size / self.upem + tracking * max(len(glyphs) - 1, 0)

    def path(self, gid: int) -> str:
        if gid not in self._paths:
            pen = SVGPathPen(self.glyphset)
            self.glyphset[self.tt.getGlyphName(gid)].draw(pen)
            self._paths[gid] = pen.getCommands()
        return self._paths[gid]


FACES: dict[str, Face] = {}


def face(name: str) -> Face:
    files = {
        "body": ("Inter-Regular.ttf", "r"), "bold": ("Inter-SemiBold.ttf", "s"),
        "mono": ("JetBrainsMono-Regular.ttf", "m"), "monob": ("JetBrainsMono-Medium.ttf", "n"),
        "display": ("BricolageGrotesque-Bold.ttf", "d"),
    }
    if name not in FACES:
        FACES[name] = Face(*files[name])
    return FACES[name]


def fmt(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s if s != "-0" else "0"


class Svg:
    def __init__(self, w: int, h: int, label: str):
        self.w, self.h, self.label = w, h, label
        self.glyphs: dict[str, str] = {}
        self.defs: list[str] = []
        self.parts: list[str] = []
        self.css = ""

    def add(self, s: str) -> None:
        self.parts.append(s)

    def text(self, font: str, s: str, x: float, y: float, size: float, fill: str,
             tracking: float = 0, anchor: str = "start", attrs: str = "") -> float:
        f = face(font)
        glyphs, adv = f.shape(s)
        sc = size / f.upem
        tr = tracking / sc
        total = (adv + tr * max(len(glyphs) - 1, 0)) * sc
        if anchor == "end":
            x -= total
        elif anchor == "middle":
            x -= total / 2
        uses = []
        for i, (gid, gx, gy) in enumerate(glyphs):
            d = f.path(gid)
            if not d:
                continue
            key = f"{f.prefix}{gid}"
            self.glyphs[key] = d
            y_attr = f' y="{-gy}"' if gy else ""
            uses.append(f'<use href="#{key}" x="{round(gx + tr * i)}"{y_attr}/>')
        self.add(f'<g transform="translate({fmt(x)} {fmt(y)}) scale({sc:.5f} {-sc:.5f})" fill="{fill}"{attrs}>'
                 + "".join(uses) + "</g>")
        return total

    def render(self) -> str:
        glyph_defs = "".join(f'<path id="{k}" d="{v}"/>' for k, v in sorted(self.glyphs.items()))
        style = f"<style>{self.css}{REDUCED_MOTION}</style>" if self.css else ""
        w, h = self.w + 2 * PAD_X, self.h + 2 * PAD_Y
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
                f'viewBox="0 0 {w} {h}" role="img" aria-label="{escape(self.label)}">'
                f"<title>{escape(self.label)}</title>{style}<defs>{glyph_defs}{''.join(self.defs)}</defs>"
                f'<g transform="translate({PAD_X} {PAD_Y})">{"".join(self.parts)}</g></svg>\n')


def wrap(font: str, text: str, size: float, max_w: float, max_lines: int) -> list[str]:
    lines = _greedy(font, text, size, max_w)
    if 1 < len(lines) <= max_lines:
        lo, hi = max_w / 2, max_w
        while hi - lo > 1:
            mid = (lo + hi) / 2
            if len(_greedy(font, text, size, mid)) == len(lines):
                hi = mid
            else:
                lo = mid
        return _greedy(font, text, size, hi)
    return _truncate(font, lines, size, max_w, max_lines)


def _truncate(font: str, lines: list[str], size: float, max_w: float, max_lines: int) -> list[str]:
    f = face(font)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and f.width(last + "…", size) > max_w:
            last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
        lines[-1] = last.rstrip(",:;") + "…"
    return lines


def _greedy(font: str, text: str, size: float, max_w: float) -> list[str]:
    f = face(font)
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if f.width(trial, size) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def star_path(cx: float, cy: float, r: float) -> str:
    import math
    pts = []
    for i in range(10):
        rad = r if i % 2 == 0 else r * 0.45
        a = -math.pi / 2 + i * math.pi / 5
        pts.append(f"{fmt(cx + rad * math.cos(a))} {fmt(cy + rad * math.sin(a))}")
    return "M" + " L".join(pts) + "Z"


def chip_row(svg: Svg, t: dict, x: float, y: float, chips: list[str], max_x: float, stars: int | None = None) -> None:
    """Pill-shaped labels, 22px tall, top edge at y."""
    size = 11.5
    if stars is not None:
        label = str(stars)
        w = face("monob").width(label, size) + 34
        svg.add(f'<rect x="{fmt(x + .5)}" y="{fmt(y + .5)}" width="{fmt(w - 1)}" height="21" rx="10.5" fill="none" stroke="{t["border"]}"/>')
        svg.add(f'<path d="{star_path(x + 15, y + 11, 6)}" fill="{t["accent"]}"/>')
        svg.text("monob", label, x + 25, y + 15, size, t["fg"])
        x += w + 6
    for c in chips:
        w = face("mono").width(c, size) + 20
        if x + w > max_x:
            break
        svg.add(f'<rect x="{fmt(x + .5)}" y="{fmt(y + .5)}" width="{fmt(w - 1)}" height="21" rx="10.5" fill="none" stroke="{t["border"]}"/>')
        svg.text("mono", c, x + 10, y + 15, size, t["muted"])
        x += w + 6


# ---------------------------------------------------------------- the images

def banner(cfg: dict, theme: str) -> str:
    t, b = THEMES[theme], cfg["banner"]
    sentence = "".join(b["tokens"]).strip()
    best = b["next_tokens"][0][0]
    svg = Svg(830, 200, f'{b["name"]}. {b["eyebrow"].title()}. "{sentence} {best}." {b["footer"]}.')
    svg.css = ("@keyframes blink{50%{opacity:0}}.caret{animation:blink 1.1s steps(1) infinite}"
               "@keyframes grow{from{transform:scaleX(0)}}.bar{transform-box:fill-box;transform-origin:left center;"
               "animation:grow .7s cubic-bezier(.2,.7,.2,1) .4s backwards}"
               "@keyframes show{from{opacity:0}}.pred{animation:show .35s ease 1.15s backwards}")
    svg.add(f'<rect x=".5" y=".5" width="829" height="199" rx="12" fill="{t["card"]}" stroke="{t["border"]}"/>')
    svg.text("monob", b["eyebrow"], 40, 50, 12, t["muted"], tracking=2)
    svg.text("display", b["name"], 38, 104, 46, t["fg"], tracking=-0.5)

    x, y, size = 40.0, 128, 15
    mono = face("monob")
    for i, tok in enumerate(b["tokens"]):
        word = tok.lstrip(" ")
        if i:
            x += 6 if tok.startswith(" ") else 1
        w = mono.width(word, size) + 14
        svg.add(f'<rect x="{fmt(x)}" y="{y}" width="{fmt(w)}" height="28" rx="5" fill="{t["tok"][i % 4]}"/>')
        svg.text("monob", word, x + 7, y + 19.5, size, t["tok_fg"])
        x += w
    x += 6
    w = mono.width(best, size) + 14
    svg.add(f'<g class="pred"><rect x="{fmt(x + .75)}" y="{y + .75}" width="{fmt(w - 1.5)}" height="26.5" rx="5" '
            f'fill="none" stroke="{t["accent"]}" stroke-width="1.5" stroke-dasharray="4 3"/>')
    svg.text("monob", best, x + 7, y + 19.5, size, t["accent"])
    svg.add("</g>")
    svg.add(f'<rect class="caret" x="{fmt(x + w + 4)}" y="{y + 4}" width="2" height="20" fill="{t["accent"]}"/>')
    pred_center = x + w / 2

    px, py = 640, 26
    svg.add(f'<path d="M{fmt(px + 12)} {py + 88} L{fmt(pred_center)} {y - 1}" stroke="{t["border"]}" stroke-width="1.5" fill="none"/>')
    svg.add(f'<rect x="{px + .5}" y="{py + .5}" width="159" height="87" rx="8" fill="{t["bg"]}" stroke="{t["border"]}"/>')
    svg.text("monob", "NEXT TOKEN", px + 14, py + 20, 10, t["muted"], tracking=1.5)
    for i, (tok, p) in enumerate(b["next_tokens"][:3]):
        ry = py + 42 + i * 18
        svg.text("mono", tok, px + 14, ry, 12, t["fg"])
        fill, op = (t["accent"], "") if i == 0 else (t["muted"], ' opacity=".45"')
        svg.add(f'<rect class="bar" x="{px + 66}" y="{ry - 7}" width="{fmt(max(p * 60, 2))}" height="7" rx="2" fill="{fill}"{op}/>')
        svg.text("mono", f"{p:.2f}", px + 148, ry, 12, t["muted"], anchor="end")
    svg.text("mono", b["footer"], 40, 182, 12.5, t["muted"])
    return svg.render()


def wordmark(theme: str, x: float, y: float, height: float) -> str:
    raw = (SCRIPTS / "brand" / THEMES[theme]["wordmark"]).read_text(encoding="utf-8")
    inner = re.search(r"<svg[^>]*>(.*)</svg>", raw, re.S).group(1)
    inner = re.sub(r"<title>.*?</title>|<desc>.*?</desc>", "", inner, flags=re.S)
    inner = re.sub(r'\s(font-family|font-size|font-weight|font-style)="[^"]*"', "", inner)
    inner = inner.replace('id="gradient', 'id="wm-gradient').replace("url(#gradient", "url(#wm-gradient")
    inner = re.sub(r">\s+<", "><", inner).strip()
    return f'<g transform="translate({fmt(x)} {fmt(y)}) scale({fmt(height / 160)})">{inner}</g>'


def thumbs(svg: Svg, x: float, y: float, kind: int) -> None:
    """Tiny stand-ins for a photo or a screenshot, 46x30."""
    cid = f"th{kind}"
    svg.defs.append(f'<clipPath id="{cid}"><rect x="{x}" y="{y}" width="46" height="30" rx="4"/></clipPath>')
    p = PHOTO
    if kind == 0:  # yellow tram on a street
        body = (f'<rect x="{x}" y="{y}" width="46" height="30" fill="{p["sky"]}"/>'
                f'<rect x="{x}" y="{y + 21}" width="46" height="9" fill="{p["street"]}"/>'
                f'<rect x="{x + 9}" y="{y + 9}" width="26" height="14" rx="3" fill="{p["tram"]}"/>'
                f'<rect x="{x + 12}" y="{y + 12}" width="5" height="4" fill="{p["sky"]}"/>'
                f'<rect x="{x + 19}" y="{y + 12}" width="5" height="4" fill="{p["sky"]}"/>'
                f'<rect x="{x + 26}" y="{y + 12}" width="5" height="4" fill="{p["sky"]}"/>')
    elif kind == 1:  # roofs and a hill
        body = (f'<rect x="{x}" y="{y}" width="46" height="30" fill="{p["sky"]}"/>'
                f'<path d="M{x} {y + 30} L{x} {y + 17} L{x + 14} {y + 11} L{x + 30} {y + 18} L{x + 46} {y + 13} L{x + 46} {y + 30}Z" fill="{p["park"]}"/>'
                f'<rect x="{x + 8}" y="{y + 19}" width="10" height="11" fill="{p["roof"]}"/>'
                f'<rect x="{x + 28}" y="{y + 15}" width="6" height="15" fill="{p["tram"]}"/>')
    else:  # screenshot of a map
        body = (f'<rect x="{x}" y="{y}" width="46" height="30" fill="{p["paper"]}"/>'
                f'<rect x="{x}" y="{y}" width="46" height="5" fill="{p["ink"]}"/>'
                f'<path d="M{x + 4} {y + 25} C{x + 14} {y + 12} {x + 26} {y + 24} {x + 42} {y + 10}" stroke="{p["tram"]}" stroke-width="2.5" fill="none"/>'
                f'<circle cx="{x + 4}" cy="{y + 25}" r="2" fill="{p["roof"]}"/><circle cx="{x + 42}" cy="{y + 10}" r="2" fill="{p["roof"]}"/>')
    svg.add(f'<g clip-path="url(#{cid})">{body}</g>')


def flagship(cfg: dict, theme: str, stars: int | None) -> str:
    t, f = THEMES[theme], cfg["flagship"]
    svg = Svg(830, 250, f'Dowse. {f["tagline"]} {f["text"]}')
    q = f["query"]
    qw = face("body").width(q, 14)
    n = len(q)
    svg.css = (f"@keyframes type{{from{{transform:translateX(0)}}to{{transform:translateX({fmt(qw + 2)}px)}}}}"
               f".cover{{animation:type 1.5s steps({n}) .5s backwards}}"
               f"@keyframes caret{{from{{transform:translateX(-{fmt(qw + 2)}px)}}to{{transform:translateX(0)}}}}"
               "@keyframes blink{50%{opacity:0}}"
               f".caret{{animation:caret 1.5s steps({n}) .5s backwards,blink 1.1s steps(1) 2s infinite}}"
               "@keyframes rise{from{opacity:0;transform:translateY(6px)}}"
               ".r{animation:rise .45s ease backwards}.r0{animation-delay:2.15s}.r1{animation-delay:2.3s}.r2{animation-delay:2.45s}")
    svg.add(f'<rect x=".5" y=".5" width="829" height="249" rx="14" fill="{t["card"]}" stroke="{t["border"]}"/>')
    svg.add(wordmark(theme, 28, 26, 40))
    y = 102
    for line in wrap("bold", f["tagline"], 18, 372, 2):
        svg.text("bold", line, 28, y, 18, t["fg"])
        y += 24
    y += 6
    for line in wrap("body", f["text"], 14, 360, 3):
        svg.text("body", line, 28, y, 14, t["muted"])
        y += 21
    chip_row(svg, t, 28, 204, f["chips"], 400, stars=stars)

    # The search window, typing the query, then showing results.
    svg.add(f'<rect x="420.5" y="22.5" width="387" height="205" rx="10" fill="{t["bg"]}" stroke="{t["border"]}"/>')
    svg.add(f'<rect x="434.5" y="36.5" width="359" height="37" rx="8" fill="{t["card"]}" stroke="{t["border"]}"/>')
    svg.add(f'<circle cx="452" cy="54" r="5.5" fill="none" stroke="{t["muted"]}" stroke-width="1.8"/>'
            f'<path d="M456.2 58.2 L460.5 62.5" stroke="{t["muted"]}" stroke-width="1.8" stroke-linecap="round"/>')
    svg.defs.append('<clipPath id="qclip"><rect x="470" y="42" width="318" height="26"/></clipPath>')
    svg.add('<g clip-path="url(#qclip)">')
    svg.text("body", q, 470, 60, 14, t["fg"])
    svg.add(f'<rect class="cover" x="470" y="42" width="318" height="26" fill="{t["card"]}" transform="translate({fmt(qw + 2)} 0)"/>')
    svg.add("</g>")
    svg.add(f'<rect class="caret" x="{fmt(470 + qw + 2)}" y="46" width="1.6" height="17" fill="{t["accent"]}"/>')
    for i, (name, meta) in enumerate(f["results"][:3]):
        ry = 84 + i * 46
        svg.add(f'<g class="r r{i}">')
        if i == 0:
            svg.add(f'<rect x="428" y="{ry}" width="372" height="42" rx="7" fill="{t["accent_soft"]}"/>')
        thumbs(svg, 438, ry + 6, i)
        svg.text("bold", name, 496, ry + 18, 13.5, t["fg"])
        svg.text("body", meta, 496, ry + 34, 12, t["muted"])
        svg.add("</g>")
    return svg.render()


def card(c: dict, theme: str, stars: int | None) -> str:
    t = THEMES[theme]
    svg = Svg(410, 190, f'{c["title"]}. {c["text"]}')
    svg.add(f'<rect x=".5" y=".5" width="409" height="189" rx="12" fill="{t["card"]}" stroke="{t["border"]}"/>')
    svg.text("monob", c["kind"].upper(), 20, 32, 11, t["muted"], tracking=1)
    if stars is not None:
        w = svg.text("monob", str(stars), 390, 32, 12, t["fg"], anchor="end")
        svg.add(f'<path d="{star_path(390 - w - 9, 28, 6)}" fill="{t["accent"]}"/>')
    elif c.get("label"):
        svg.text("monob", c["label"], 390, 32, 12, t["accent"], anchor="end")
    svg.text("bold", c["title"], 20, 64, 19, t["fg"])
    y = 92
    for line in wrap("body", c["text"], 14, 370, 3):
        svg.text("body", line, 20, y, 14, t["muted"])
        y += 20
    chip_row(svg, t, 20, 150, c.get("chips", []), 390)
    return svg.render()


def video_card(v: dict, theme: str, channel: str) -> str:
    t = THEMES[theme]
    svg = Svg(410, 302, f'Video: {v["title"]}')
    svg.defs.append('<clipPath id="thumb"><rect x="0" y="0" width="410" height="231" rx="10"/></clipPath>')
    if v.get("thumb"):
        svg.add(f'<image clip-path="url(#thumb)" x="0" y="0" width="410" height="231" preserveAspectRatio="xMidYMid slice" '
                f'href="data:image/jpeg;base64,{v["thumb"]}"/>')
    else:
        svg.add(f'<rect x="0" y="0" width="410" height="231" rx="10" fill="{t["card"]}"/>')
    svg.add(f'<rect x=".5" y=".5" width="409" height="230" rx="10" fill="none" stroke="{t["border"]}"/>')
    y = 256
    for line in wrap("bold", v["title"], 15.5, 404, 2):
        svg.text("bold", line, 2, y, 15.5, t["fg"])
        y += 21
    svg.text("mono", f'{v["date"]} · {channel}', 2, y + 2, 12, t["muted"])
    return svg.render()


# ---------------------------------------------------------------- data

def get(url: str, timeout: int = 20, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def repo_stars(user: str) -> dict[str, int] | None:
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    try:
        data = json.loads(get(f"https://api.github.com/users/{user}/repos?per_page=100", headers=headers))
        return {r["name"]: r["stargazers_count"] for r in data}
    except (urllib.error.URLError, ValueError, KeyError, TimeoutError) as e:
        print(f"warning: GitHub API unavailable, keeping current cards ({e})", file=sys.stderr)
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def is_short(video_id: str) -> bool:
    """YouTube serves /shorts/<id> for Shorts and redirects regular videos to /watch."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(f"https://www.youtube.com/shorts/{video_id}", headers=UA, method="HEAD")
    try:
        with opener.open(req, timeout=15) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False
    except (urllib.error.URLError, TimeoutError):
        return False


def short_date(dt) -> str:
    return f"{dt.day} {dt.strftime('%b %Y')}"


def latest_videos(channel_id: str, count: int) -> list[dict] | None:
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    try:
        root = ET.fromstring(get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"))
    except (urllib.error.URLError, ET.ParseError, TimeoutError) as e:
        print(f"warning: YouTube feed unavailable, keeping current videos ({e})", file=sys.stderr)
        return None
    from datetime import datetime
    videos = []
    for entry in root.findall("a:entry", ns):
        vid = entry.findtext("yt:videoId", namespaces=ns)
        links = [l.get("href", "") for l in entry.findall("a:link", ns)]
        # The feed links a Short to /shorts/<id>; fall back to asking YouTube only if it doesn't say.
        if not vid or any("/shorts/" in h for h in links) or (not any("/watch" in h for h in links) and is_short(vid)):
            continue
        published = datetime.fromisoformat(entry.findtext("a:published", namespaces=ns))
        thumb = None
        try:
            thumb = base64.b64encode(get(f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg")).decode()
        except (urllib.error.URLError, TimeoutError):
            pass
        videos.append({"id": vid, "title": entry.findtext("a:title", namespaces=ns).strip(),
                       "date": short_date(published), "thumb": thumb})
        if len(videos) == count:
            break
    return videos


def latest_posts(feed: str, count: int) -> list[dict] | None:
    try:
        root = ET.fromstring(get(feed))
    except (urllib.error.URLError, ET.ParseError, TimeoutError) as e:
        print(f"warning: Medium feed unavailable, keeping current posts ({e})", file=sys.stderr)
        return None
    posts = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").split("?")[0]
        posts.append({"title": (item.findtext("title") or "").strip(), "url": link,
                      "date": short_date(parsedate_to_datetime(item.findtext("pubDate")))})
    return posts[:count] or None


# ---------------------------------------------------------------- README sections

def picture(path_stem: str, alt: str, width: str) -> str:
    return (f'<picture><source media="(prefers-color-scheme: dark)" srcset="{path_stem}-dark.svg">'
            f'<img alt="{escape(alt)}" src="{path_stem}-light.svg" width="{width}"></picture>')


def replace_section(text: str, name: str, body: str) -> str:
    pattern = re.compile(rf"(<!-- {name}:START -->).*?(<!-- {name}:END -->)", re.S)
    if not pattern.search(text):
        raise SystemExit(f"README.md has no <!-- {name}:START --> / <!-- {name}:END --> markers")
    return pattern.sub(lambda m: f"{m.group(1)}\n{body}\n{m.group(2)}", text)


def md_escape(s: str) -> str:
    return re.sub(r"([\[\]\\`*_])", r"\\\1", s)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        path.write_text(content, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")


def main() -> None:
    cfg = tomllib.loads((SCRIPTS / "profile.toml").read_text(encoding="utf-8"))
    stars = repo_stars(cfg["user"])

    for theme in THEMES:
        write(ASSETS / f"banner-{theme}.svg", banner(cfg, theme))
        if stars is not None:
            write(ASSETS / "cards" / f"dowse-{theme}.svg", flagship(cfg, theme, stars.get(cfg["flagship"]["repo"])))
            for c in cfg["cards"]:
                s = stars.get(c["repo"]) if c.get("repo") else None
                write(ASSETS / "cards" / f'{c["file"]}-{theme}.svg', card(c, theme, s))

    readme = README.read_text(encoding="utf-8")

    videos = latest_videos(cfg["youtube_channel_id"], cfg["videos_shown"])
    if videos:
        links = []
        for i, v in enumerate(videos, 1):
            for theme in THEMES:
                write(ASSETS / "videos" / f"video-{i}-{theme}.svg", video_card(v, theme, cfg["youtube_channel_name"]))
            links.append(f'<a href="https://www.youtube.com/watch?v={v["id"]}">'
                         + picture(f"assets/videos/video-{i}", v["title"], "50%") + "</a>")
        rows = ["".join(links[i:i + 2]) for i in range(0, len(links), 2)]
        readme = replace_section(readme, "VIDEOS", "<p>\n" + "\n".join(rows) + "\n</p>")

    posts = latest_posts(cfg["medium_feed"], cfg["posts_shown"])
    if posts:
        lines = [f'- <samp>{p["date"]}</samp> &nbsp;[{md_escape(p["title"])}]({p["url"]})' for p in posts]
        readme = replace_section(readme, "WRITING", "\n" + "\n".join(lines) + "\n")

    write(README, readme)


if __name__ == "__main__":
    main()
