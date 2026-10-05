"""Renders a Part-Time Quant Academy card as a PNG.

One renderer, seven cards. The member gets the PNG in ~/quant and drags it into
Skool, and the tutorials show the same file, so the two cannot drift apart.

A card is a list of blocks:
  ("check", label, value, detail, passed)   a PASS/FAIL row
  ("kv",    label, value)                   a key/value row
  ("text",  paragraph)                      a wrapped paragraph
  ("rule",)                                 a divider
"""
import os
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import numpy as np

W = 1600
SERIF_I = os.path.expanduser("~/Library/Fonts/InstrumentSerif-Italic.ttf")
SANS    = "/System/Library/Fonts/Supplemental/Arial.ttf"
SANS_B  = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
MONO    = "/System/Library/Fonts/Menlo.ttc"

INK, MUTED = (28, 26, 46), (108, 104, 132)
GREEN, RED, AMBER = (22, 132, 90), (200, 52, 52), (183, 119, 20)

def _font_candidates(path):
    # Packaged fonts, when present, travel with the skill. Never fetch fonts.
    here = Path(__file__).resolve().parent
    name = Path(path).name
    yield here / "fonts" / name
    yield here.parent / "fonts" / name
    yield path
    windows_name = {
        SERIF_I: "georgiai.ttf", SANS: "arial.ttf",
        SANS_B: "arialbd.ttf", MONO: "consola.ttf",
    }.get(path)
    if windows_name:
        yield Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / windows_name


def _f(p, s, i=0):
    for candidate in _font_candidates(p):
        try:
            return ImageFont.truetype(str(candidate), s, index=i)
        except (OSError, ValueError):
            continue
    try:
        return ImageFont.load_default(size=s)
    except (TypeError, ImportError) as exc:
        raise RuntimeError("Card fonts need Pillow 10.1+ with FreeType support.") from exc


def _width(text, font):
    left, _, right, _ = font.getbbox(text)
    return max(right - left, font.getlength(text))


def _wrap(text, font, width):
    """Wrap by actual glyph bounds, including long tokens and explicit newlines."""
    lines = []
    for paragraph in str(text).split("\n"):
        line = ""
        for word in paragraph.split():
            candidate = (line + " " + word) if line else word
            if _width(candidate, font) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
                line = ""
            # Split an overlong filename/URL/answer without discarding characters.
            for char in word:
                if _width(char, font) > width:
                    raise ValueError("Text column is narrower than a single glyph")
                if line and _width(line + char, font) > width:
                    lines.append(line)
                    line = ""
                line += char
        lines.append(line)
    return lines


def _text(ops, text, font, color, x, y, width, spacing=8):
    lines = _wrap(text, font, width)
    ascent, descent = font.getmetrics()
    heights = [max(ascent + descent, font.getbbox(line)[3] - font.getbbox(line)[1])
               for line in lines]
    for line, height in zip(lines, heights):
        if line:
            left, top, right, bottom = font.getbbox(line)
            # Place the ink inside its measured box, including italic overhangs.
            ops.append(("text", (x-left, y-top), line, font, color,
                        (x, y, x+right-left, y+bottom-top)))
        y += height + spacing
    return y - spacing

def _grad(w, h):
    stops=[(0.00,(239,184,155)),(0.28,(163,144,242)),(0.66,(108,84,240)),(1.00,(83,52,201))]
    # Bound temporary arrays when long answers make a tall card.
    image = Image.new("RGB", (w, h))
    for start in range(0, h, 128):
        yy,xx=np.mgrid[start:min(start+128,h),0:w]
        t=np.clip((xx/w)*0.72+(yy/h)*0.55-0.16,0,1)
        o=np.zeros((*t.shape,3))
        for i in range(len(stops)-1):
            p0,c0=stops[i]; p1,c1=stops[i+1]; m=(t>=p0)&(t<=p1)
            if not m.any(): continue
            k=((t-p0)/(p1-p0))[m][:,None]
            o[m]=np.array(c0)*(1-k)+np.array(c1)*k
        image.paste(Image.fromarray(o.astype(np.uint8)), (0,start))
    return image


def _layout(card):
    ops = []
    L, R = 160, W-160
    fonts = {"eyebrow": _f(SANS,21), "title": _f(SERIF_I,78),
             "subtitle": _f(SANS,25), "badge": _f(SANS_B,30),
             "label": _f(SANS_B,20), "value": _f(SANS,23),
             "mono": _f(MONO,26), "small": _f(SANS,20),
             "check": _f(SANS_B,19)}
    y = _text(ops, card["eyebrow"], fonts["eyebrow"], MUTED, L, 146, R-L) + 15
    title_width = R-L
    badge_bottom = y
    if card.get("badge"):
        badge = card["badge"].upper()
        col = GREEN if badge in ("PASS","APPROVE") else (RED if badge in ("FAIL","BLOCK") else AMBER)
        badge_width = min(_width(badge, fonts["badge"])+56, 400)
        badge_ops = []
        badge_bottom = _text(badge_ops, badge, fonts["badge"], (255,255,255),
                             R-badge_width+28, y+16, badge_width-56) + 16
        ops.append(("box", (R-badge_width, y, R, badge_bottom), 32, col))
        ops.extend(badge_ops)
        title_width -= badge_width + 30
    y = max(_text(ops, card["title"], fonts["title"], INK, L, y, title_width),
            badge_bottom) + 24
    if card.get("subtitle"):
        y = _text(ops, card["subtitle"], fonts["subtitle"], MUTED, L, y, R-L) + 34
    y = max(356, y)
    for block in card["blocks"]:
        kind = block[0]
        start = y
        if kind == "rule":
            ops.append(("rule", (L,y,R,y)))
            y += 40
        elif kind == "kv":
            _, label, value = block
            label_end = _text(ops, label, fonts["label"], MUTED, L, y, 270)
            value_end = _text(ops, value, fonts["value"], INK, L+300, y, R-L-300)
            y = max(start+44, max(label_end,value_end)+16)
        elif kind == "text":
            y = _text(ops, block[1], fonts["value"], INK, L, y, R-L) + 16
        elif kind == "check":
            _, label, value, detail, state = block
            word = "WARN" if state == "warn" else ("PASS" if state else "FAIL")
            color = AMBER if state == "warn" else (GREEN if state else RED)
            ops.append(("box", (L,y,L+86,y+34), 8, color))
            _text(ops, word, fonts["check"], (255,255,255), L+13, y+7, 70)
            y = _text(ops, label, fonts["label"], INK, L+110, y+4, R-L-110)+10
            y = _text(ops, value, fonts["mono"], INK, L+110, y, R-L-110)+10
            if detail:
                y = _text(ops, detail, fonts["small"], MUTED, L+110, y, R-L-110)+16
            y = max(start+122, y)
    footer_y = max(y+30, 726)
    end = _text(ops, card["footer"], fonts["small"], MUTED, L, footer_y, R-L)
    return max(900, end+150), ops

def render(card, out_path):
    H, ops = _layout(card)
    img = _grad(W,H).convert("RGBA"); d = ImageDraw.Draw(img)
    x0,y0,x1,y1 = 90,90,W-90,H-90
    sh = Image.new("RGBA",(W,H),(0,0,0,0))
    ImageDraw.Draw(sh).rounded_rectangle([x0+6,y0+14,x1+6,y1+16],34,fill=(40,20,80,70))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(22)))
    d.rounded_rectangle([x0,y0,x1,y1],34,fill=(255,254,251,255))
    for op in ops:
        if op[0] == "text":
            _, position, text, font, color, _ = op
            d.text(position, text, font=font, fill=color)
        elif op[0] == "box":
            _, bounds, radius, color = op
            d.rounded_rectangle(bounds, radius, fill=color)
        elif op[0] == "rule":
            d.line(op[1], fill=(226,224,236), width=2)
    img.convert("RGB").save(out_path, quality=95)
    return out_path
