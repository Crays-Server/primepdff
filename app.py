"""Local Tamil magazine generator: python -m streamlit run app.py."""
from __future__ import annotations
import base64
import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from html import escape
from io import BytesIO
from pathlib import Path
import re
import unicodedata
from uuid import uuid4
from zoneinfo import ZoneInfo
import fitz
from fontTools.ttLib import TTFont
from PIL import Image
from photos import draw_photos
from template_layouts import TEMPLATES, render as render_template

ROOT = Path(__file__).resolve().parent
FRONT_TEMPLATE = ROOT / "templates/only front.pdf"
CONTENT_TEMPLATE = ROOT / "templates/blank_front_template_no_date_no_photo.pdf"
FONT_01 = next((ROOT / "fonts" / n for n in ("Uni Ila.Sundaram-01.ttf", "Uni Ila.Sundaram-01.ttf.orig")
                if (ROOT / "fonts" / n).exists()), ROOT / "fonts/Uni Ila.Sundaram-01.ttf")
FONT_03 = ROOT / "fonts/Uni Ila.Sundaram-03.ttf"
FONT_006 = ROOT / "fonts/TAM-Tamil006 Normal.ttf"
FONT_TITLE = ROOT / "fonts/TAM-Tamil013 Normal.ttf"
OUTPUT_DIR = ROOT / "output"
MAX_PDF_BYTES = 600_000

def compact_pdf(document: fitz.Document) -> bytes:
    """Keep vector text; progressively compress images only when necessary."""
    data = document.tobytes(garbage=4, deflate=True)
    if len(data) < MAX_PDF_BYTES:
        return data
    document.subset_fonts()
    original = document.tobytes(garbage=4, deflate=True, use_objstms=1)
    if len(original) < MAX_PDF_BYTES:
        return original
    for dpi, quality in [(150,82),(120,72),(100,60),(85,45),(70,30),(50,18)]:
        with fitz.open(stream=original,filetype="pdf") as candidate:
            # The small shared publication label must remain lossless. Image
            # rewriting can downsample its reused resource again on each page.
            # Remove it before rewriting and restore the original artwork after.
            header_pages, header_xrefs = [], set()
            for page in candidate:
                matches = [image for image in page.get_image_info(xrefs=True)
                           if all(abs(a-b)<0.5 for a,b in zip(image["bbox"],PUBLICATION_RECT))]
                if matches:
                    header_pages.append(page.number)
                    header_xrefs.update(image["xref"] for image in matches if image["xref"])
            for xref in header_xrefs:
                candidate[0].delete_image(xref)
            candidate.rewrite_images(dpi_threshold=dpi+1,dpi_target=dpi,quality=quality)
            for number in header_pages:
                candidate[number].insert_image(PUBLICATION_RECT,filename=str(ROOT / "assets/publication.png"))
            data = candidate.tobytes(garbage=4,deflate=True,use_objstms=1)
            if len(data) < MAX_PDF_BYTES:
                return data
    raise ValueError("This article cannot fit under 600 KB while preserving its text. "
                     "Shorten the article or use simpler photos and try again.")
# PDF point coordinates measured against the six reference pages. Backgrounds
# are placed in the reference media box; original PDF artwork is not repainted.
PAGE_RECT = fitz.Rect(0, 0, 595.2000122070312, 841.9199829101562)
CONTENT_RECT = fitz.Rect(14.0, 61.2, 581.2, 539.0)
TITLE_BASELINE, TITLE_LEADING = 687.0, 60.0
TITLE_LEFT, TITLE_RIGHT = 32.4728, 584.0653
TITLE_RECT = fitz.Rect(24.0, 626.0, 571.2, 810.0)
DATE_RECT = fitz.Rect(1.5, 21.0, 147.5, 49.0)
NUMBER_RECT = fitz.Rect(497.0, 21.0, 592.0, 49.0)
PUBLICATION_RECT = fitz.Rect(240,26,364,49)
BODY_SIZE, BODY_LEADING = 22.0, 35.001
SUB_SIZE, TITLE_SIZE = 24.0008, 42.0
BLACK = (0, 0, 0)
BLUE = (32 / 255, 66 / 255, 155 / 255)
BROWN = (159 / 255, 73 / 255, 50 / 255)
MONTHS = ("ஜனவரி", "பிப்ரவரி", "மார்ச்", "ஏப்ரல்", "மே", "ஜூன்", "ஜூலை", "ஆகஸ்ட்",
          "செப்டம்பர்", "அக்டோபர்", "நவம்பர்", "டிசம்பர்")

# Pasted articles often contain typographic quotes absent from the supplied
# font. Use supported punctuation without switching fonts or changing Tamil.
QUOTE_TRANSLATION = str.maketrans({"“": '"', "”": '"', "„": '"',
                                  "‘": "'", "’": "'", "‚": "'"})

def normalize_article_text(text: str) -> str:
    return unicodedata.normalize("NFC", text).translate(QUOTE_TRANSLATION)

def complete_font(path: Path) -> bool:
    if not path.is_file():
        return False
    with TTFont(path) as font:
        cmap = font.getBestCmap() or {}
        if not all(ord(c) in cmap for c in "அகஙசஞடணதநபமயரலவழளறன"):
            return False
        if "glyf" in font:
            empty = sum(font["glyf"][g].numberOfContours == 0 for g in font.getGlyphOrder())
            if empty > len(font.getGlyphOrder()) // 4:
                return False
    return True

def font_paths() -> tuple[Path, Path]:
    if not complete_font(FONT_01):
        raise ValueError(f"A complete Unicode Sundaram font is required: {FONT_01}")
    return (FONT_03 if complete_font(FONT_03) else FONT_01,
            FONT_006 if complete_font(FONT_006) else FONT_01)

def font_notices() -> list[str]:
    body, sub = font_paths()
    notices = []
    if body == FONT_01:
        notices.append("Complete Sundaram-03 is missing. Body text uses your supplied Sundaram-01; "
                       "its weight and widths differ from full.pdf.")
    if sub == FONT_01:
        notices.append("Complete Tamil006 is missing. Subheadings use your supplied Sundaram-01 "
                       "in the reference blue.")
    return notices

# TAM byte positions from Unicode Technical Note 18. A longest-match conversion
# handles combined letters; Windows-1252 characters address the legacy cmap.
# https://www.unicode.org/notes/tn18/
def tam_map() -> dict[str, str]:
    result = {}
    def glyph(*values):
        return bytes(values).decode("cp1252")
    for offset, letter in enumerate("அஆஇஈஉஊஎஏஐஒஓ"):
        result[letter] = glyph(0xDC + offset)
    result["ஔ"], result["ஃ"] = glyph(0xE5, 0xF7), glyph(0xE7)
    consonants = list("கஙசஞடணதநபமயரலவழளறனஸஷஜஹ") + ["க்ஷ"]
    i_order = list("கஙசஞணதநபமயரலவழளறனஸஷஜஹ") + ["க்ஷ"]
    i_codes = list(range(0x41, 0x57))
    ii_codes = list(range(0x57, 0x5B)) + list(range(0x61, 0x73))
    u_codes = list(range(0xB0, 0xB7)) + list(range(0xB8, 0xC3))
    uu_codes = list(range(0xC3, 0xCA)) + list(range(0xCB, 0xD0)) + list(range(0xD6, 0xDC))
    pulli_codes = list(range(0x82, 0x8D)) + list(range(0x98, 0x9D)) + [0x9F, 0xA1]
    for index, letter in enumerate(consonants):
        base = 0xE8 + index
        result[letter] = glyph(base)
        for sign, values in {"ா": (base, 0xA3), "ெ": (0xAA, base), "ே": (0xAB, base),
            "ை": (0xAC, base), "ொ": (0xAA, base, 0xA3), "ோ": (0xAB, base, 0xA3),
            "ௌ": (0xAA, base, 0xF7)}.items():
            result[letter + sign] = glyph(*values)
        if index < 18:
            result[letter + "ு"] = glyph(u_codes[index])
            result[letter + "ூ"] = glyph(uu_codes[index])
            result[letter + "்"] = glyph(pulli_codes[index])
        else:
            result[letter + "ு"] = glyph(base, 0xA7)
            result[letter + "ூ"] = glyph(base, 0xA8)
            result[letter + "்"] = glyph(0x76 + index - 18)
    for letter, ci, cii in zip(i_order, i_codes, ii_codes):
        result[letter + "ி"], result[letter + "ீ"] = glyph(ci), glyph(cii)
    result["டி"], result["டீ"], result["ஸ்ரீ"] = glyph(0xAE), glyph(0xAF), glyph(0xFF)
    return result

TAM_MAP = tam_map()
TAM_KEYS = sorted(TAM_MAP, key=len, reverse=True)

def unicode_to_tam(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    converted, index = [], 0
    while index < len(text):
        key = next((k for k in TAM_KEYS if text.startswith(k, index)), None)
        if key:
            converted.append(TAM_MAP[key]); index += len(key)
        elif "\u0b80" <= text[index] <= "\u0bff":
            raise ValueError(f"Tamil013 cannot encode the title character {text[index]!r}.")
        else:
            converted.append(text[index]); index += 1
    return "".join(converted)

def validate_text(text: str, path: Path) -> None:
    with TTFont(path) as font:
        cmap = font.getBestCmap() or {}
        missing = sorted({c for c in text if not c.isspace() and ord(c) not in cmap})
    if missing:
        raise ValueError(f"{path.name} does not support: {' '.join(missing)}. "
                         "Supply a complete compatible font; no silent substitution is performed.")

def clean_blocks(raw: list[dict]) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("The editor must return structured content.")
    blocks = []
    for source_index, item in enumerate(raw):
        kind = item.get("type", "body")
        if kind not in {"heading", "subheading", "body", "bullet", "number"}:
            raise ValueError("Unsupported editor block type.")
        runs = item.get("runs") or [{"text": item.get("text", ""), "bold": False}]
        runs = [{"text": normalize_article_text(str(r.get("text", ""))),
                 "bold": bool(r.get("bold", False)),
                 **({"color":str(r["color"]).lower()} if r.get("color") else {})} for r in runs]
        for run in runs:
            if run.get("color") and not re.fullmatch(r"#[0-9a-f]{6}",run["color"]):
                raise ValueError("Choose a valid text color.")
        text = "".join(r["text"] for r in runs)
        if text.strip():
            alignment = item.get("align", "justify" if kind == "body" else "left")
            if alignment not in {"left", "center", "justify"}:
                alignment = "left"
            style = {}
            if item.get("align_explicit"):
                style["align_explicit"] = True
            if item.get("font_size") not in (None, ""):
                size = float(item["font_size"])
                if not 8 <= size <= 96:
                    raise ValueError("Font size must be between 8 and 96 points.")
                style["font_size"] = size
            if item.get("color"):
                color = str(item["color"]).lower()
                if not re.fullmatch(r"#[0-9a-f]{6}",color):
                    raise ValueError("Choose a valid text color.")
                style["color"] = color
            blocks.append({"type": kind, "runs": runs, "text": text, "align": alignment, **style,
                           "source_index": source_index,
                           "number": max(1, int(item.get("number", 1)))})
    if not blocks:
        raise ValueError("Please enter article content.")
    return blocks

def inline_html(block: dict) -> str:
    parts = []
    for run in block["runs"]:
        content = escape(run["text"]).replace("\n", "<br>")
        if run.get("bullet_marker"):
            content = f'<span style="vertical-align:-0.35em">{content}</span>'
        if run["bold"]: content = f"<b>{content}</b>"
        if run.get("color"): content = f'<span style="color:{run["color"]}">{content}</span>'
        parts.append(content)
    return "".join(parts)

def story_css(body: Path, sub: Path) -> str:
    return f'''
    @font-face {{font-family: Body; src: url("{body.relative_to(ROOT).as_posix()}");}}
    @font-face {{font-family: Sub; src: url("{sub.relative_to(ROOT).as_posix()}");}}
    @font-face {{font-family: Bold; src: url("assets/body-bold.ttf");}}
    body {{font-family:Body; font-size:{BODY_SIZE}pt; margin:0; color:#000;}}
    p {{margin:0; padding:0; line-height:{BODY_LEADING}pt;}}
    b {{font-family:Bold; font-weight:normal;}}
    .heading {{font-family:Sub; font-size:{SUB_SIZE}pt; color:#000;}}
    .subheading {{font-family:Sub; font-size:{SUB_SIZE}pt; color:#20429b;}}
    .align-center {{text-align:center;}}
    .align-left {{text-align:left;}}
    .align-justify {{text-align:justify;}}
    '''

def ensure_bold_font(body: Path) -> None:
    target, stamp = ROOT / "assets/body-bold.ttf", ROOT / "assets/body-bold.source"
    identity = f"{body}:{body.stat().st_mtime_ns}"
    if target.exists() and stamp.exists() and stamp.read_text() == identity:
        return
    target.parent.mkdir(exist_ok=True)
    with TTFont(body) as font:
        for record in font["name"].names:
            if record.nameID in (1, 3, 4, 6):
                record.string = "MagazineBold".encode(record.getEncoding())
        font.save(target)
    stamp.write_text(identity)

def make_story(block: dict, css: str) -> fitz.Story:
    styles = []
    if block.get("font_size"):
        styles.extend([f'font-size:{block["font_size"]}pt',f'line-height:{block_line_height(block)}pt'])
    if block.get("color"):
        styles.append(f'color:{block["color"]}')
    return fitz.Story(f'<p class="{block["type"]} align-{block.get("align", "left")}" style="{";".join(styles)}">{inline_html(block)}</p>',
                      user_css=css, archive=fitz.Archive(str(ROOT)))

def block_font_size(block: dict) -> float:
    return block.get("font_size") or (SUB_SIZE if block["type"] in {"heading","subheading"} else BODY_SIZE)

def block_line_height(block: dict) -> float:
    default = SUB_SIZE if block["type"] in {"heading","subheading"} else BODY_SIZE
    return BODY_LEADING * block_font_size(block) / default

def embolden_overlay(document: fitz.Document) -> None:
    # Dedicated font resource lets us synthesize bold without switching glyphs.
    for page in document:
        bold = {f[4] for f in page.get_fonts() if "MagazineBold" in f[3]}
        for xref in page.get_contents():
            stream = document.xref_stream(xref)
            def replace(match):
                mode = b" 0.45 w 2 Tr" if match.group(1).decode("ascii") in bold else b" 0 Tr"
                return match.group(0) + mode
            stream = re.sub(rb"/([A-Za-z0-9_-]+)\s+[0-9.]+\s+Tf", replace, stream)
            document.update_stream(xref, stream)

def story_document(story: fitz.Story) -> fitz.Document:
    buffer = BytesIO()
    writer = fitz.DocumentWriter(buffer)
    device = writer.begin_page(PAGE_RECT)
    story.draw(device)
    writer.end_page(); writer.close()
    overlay = fitz.open(stream=buffer.getvalue(), filetype="pdf")
    embolden_overlay(overlay)
    return overlay

def draw_story(page: fitz.Page, story: fitz.Story) -> None:
    with story_document(story) as overlay:
        page.show_pdf_page(PAGE_RECT, overlay, 0)

def centered_story(page: fitz.Page, story: fitz.Story, target: fitz.Rect, align="center") -> None:
    measure = fitz.Rect(0, 0, target.width, max(80,target.height))
    more, _ = story.place(measure)
    if more:
        raise ValueError("The header text exceeds its fixed area.")
    with story_document(story) as overlay:
        pix = overlay[0].get_pixmap(clip=measure, matrix=fitz.Matrix(3, 3), alpha=True)
        bounds = Image.frombytes("RGBA", (pix.width, pix.height), pix.samples).getchannel("A").getbbox()
        if not bounds:
            return
        ink = fitz.Rect(*[(bounds[i] + (pix.x if i % 2 == 0 else pix.y)) / 3 for i in range(4)])
        if ink.height > target.height or ink.width > target.width:
            raise ValueError("The header text exceeds its fixed area.")
        cx, cy = (target.x0+target.x1)/2, (target.y0+target.y1)/2
        destination = fitz.Rect(cx-ink.width/2, cy-ink.height/2, cx+ink.width/2, cy+ink.height/2)
        if align=="left":
            destination=fitz.Rect(target.x0,destination.y0,target.x0+ink.width,destination.y1)
        page.show_pdf_page(destination, overlay, 0, clip=ink, keep_proportion=False)

def add_template(output: fitz.Document, template: fitz.Document) -> fitz.Page:
    page = output.new_page(width=PAGE_RECT.width, height=PAGE_RECT.height)
    page.show_pdf_page(PAGE_RECT, template, 0, keep_proportion=False)
    return page

def title_segments(text: str):
    for match in re.finditer(r"[\u0b80-\u0bff]+|[^\u0b80-\u0bff]+", text):
        tamil = "\u0b80" <= match.group()[0] <= "\u0bff"
        yield (unicode_to_tam(match.group()) if tamil else match.group()), tamil

def title_layout(text: str, font_size=None) -> tuple[list[str], float, float]:
    fonts = {True: fitz.Font(fontfile=str(FONT_TITLE)), False: fitz.Font(fontfile=str(FONT_01))}
    used = {kind for _, kind in title_segments(text)} or {True}
    font_height = max(fonts[k].ascender for k in used) - min(fonts[k].descender for k in used)
    # Preserve designer line breaks. Fit longer titles inside the same artwork
    # area automatically, using the chosen size as the upper bound.
    maximum = round(font_size or TITLE_SIZE)
    for size in range(maximum,min(maximum,24)-1,-1):
        def width(value):
            return sum(fonts[t].text_length(s, fontsize=size) for s, t in title_segments(value))
        lines, too_wide = [], False
        for explicit_line in text.split("\n"):
            current = ""
            for word in explicit_line.split():
                if width(word) > TITLE_RECT.width:
                    too_wide = True
                    break
                candidate = f"{current} {word}".strip()
                if current and width(candidate) > TITLE_RECT.width:
                    lines.append(current); current = word
                else:
                    current = candidate
            lines.append(current)
        leading = size * TITLE_LEADING / TITLE_SIZE
        height = (len(lines)-1)*leading + size*font_height
        if not too_wide and height <= TITLE_RECT.height:
            return lines, float(size), leading
    raise ValueError("The title is too long for its front-page area. Edit it or its line breaks in the preview.")

def draw_title(page: fitz.Page, text: str, font_size=None, color=None, runs=None) -> None:
    fonts = {True: fitz.Font(fontfile=str(FONT_TITLE)), False: fitz.Font(fontfile=str(FONT_01))}
    page.insert_font(fontname="TitleTam", fontfile=str(FONT_TITLE))
    page.insert_font(fontname="TitleLatin", fontfile=str(FONT_01))
    lines, size, leading = title_layout(text,font_size)
    chosen_color = tuple(int(color[i:i+2],16)/255 for i in (1,3,5)) if color else None
    used = {kind for _, kind in title_segments(text)} or {True}
    ascent = max(fonts[k].ascender for k in used)
    descent = min(fonts[k].descender for k in used)
    height = (len(lines)-1)*leading + size*(ascent-descent)
    baseline = TITLE_RECT.y0 + (TITLE_RECT.height-height)/2 + size*ascent
    def width(value):
        return sum(fonts[t].text_length(s, fontsize=size) for s, t in title_segments(value))
    colors = [run.get("color") for run in (runs or [{"text":text}]) for _ in run["text"]]
    cursor = 0
    for index, line in enumerate(lines):
        x = TITLE_RECT.x0 + (TITLE_RECT.width - width(line)) / 2
        groups = []
        for char in line:
            while cursor < len(text) and text[cursor].isspace() and not char.isspace(): cursor += 1
            shade = colors[cursor] if cursor < len(colors) else None
            if groups and groups[-1][1] == shade: groups[-1] = (groups[-1][0]+char,shade)
            else: groups.append((char,shade))
            cursor += 1
        segments = [(segment,tamil,shade) for value,shade in groups for segment,tamil in title_segments(value)]
        for segment, tamil, shade in segments:
            validate_text(segment, FONT_TITLE if tamil else FONT_01)
            page.insert_text((x, baseline + index * leading), segment,
                             fontname="TitleTam" if tamil else "TitleLatin", fontsize=size,
                             color=tuple(int(shade[i:i+2],16)/255 for i in (1,3,5)) if shade else chosen_color if chosen_color is not None else BLACK if index == 0 else BLUE)
            x += fonts[tamil].text_length(segment, fontsize=size)

def draw_header(page: fitz.Page, selected: date, number: int, body: Path) -> None:
    value = f"{selected.day:02d}-{MONTHS[selected.month - 1]}-{selected.year}"
    for text, rect in [(value, DATE_RECT), (f"Page-{number}", NUMBER_RECT)]:
        block = {"type": "body", "runs": [{"text": text, "bold": True}]}
        # The visible raster header is 16 pt; the hidden OCR layer reports 18.
        css = story_css(body, body) + "body{font-size:16pt;} p{line-height:22pt;}"
        story = make_story(block, css)
        centered_story(page, story, rect)
    page.insert_image(PUBLICATION_RECT, filename=str(ROOT / "assets/publication.png"))

@dataclass
class Generation:
    path: Path | None
    page_count: int
    notices: list[str]
    data: bytes
    placements: list[dict]

def generate_pdf(article: list[dict], selected: date, uploads=None, small_crop=None, save=True, photo_crops=None,
                 oval_photos=None, oval_crops=None, enforce_size=True, template_id="original") -> Generation:
    if template_id not in TEMPLATES:
        raise ValueError("Unknown magazine template.")
    if template_id != "original":
        return render_template(globals(),article,selected,uploads,photo_crops,oval_photos,oval_crops,save,enforce_size,template_id)
    blocks = clean_blocks(article)
    body, sub = font_paths()
    ensure_bold_font(body)
    title_index = next((i for i, b in enumerate(blocks) if b["type"] == "heading"), 0)
    title_block = blocks.pop(title_index)
    title = title_block["text"]
    for block in blocks:
        validate_text(block["text"], sub if block["type"] in {"subheading", "heading"} else body)
    css = story_css(body, sub)
    placements = [{"page": 0, "source_index": title_block["source_index"],
                   "type": "heading", "rect": list(TITLE_RECT),
                   "font_size": title_layout(title,title_block.get("font_size"))[1],
                   "color":title_block.get("color")}]
    with fitz.open(FRONT_TEMPLATE) as front, fitz.open(CONTENT_TEMPLATE) as content, fitz.open() as output:
        if front[0].rect != PAGE_RECT:
            raise ValueError("The front template dimensions differ from the supplied reference.")
        page = add_template(output, front)
        draw_photos(page, uploads, front=True, small_crop=small_crop,photo_crops=photo_crops)
        draw_title(page, title,title_block.get("font_size"),title_block.get("color"),title_block["runs"])
        page, y = None, CONTENT_RECT.y0
        def new_page():
            page = add_template(output, content)
            if oval_photos is None:
                draw_photos(page, uploads, front=False,photo_crops=photo_crops)
            else:
                number = len(output)
                draw_photos(page,{"oval":oval_photos.get(number)},front=False,
                            photo_crops={"oval":(oval_crops or {}).get(number,{})})
            draw_header(page, selected, len(output), body)
            return page
        for index, block in enumerate(blocks):
            kind = block["type"]
            size = block_font_size(block)
            scale = size/BODY_SIZE
            if kind == "number":
                marker_width = fitz.Font(fontfile=str(body)).text_length(f'{block["number"]}.', fontsize=size)
                left = max(45.0, 16.0 + marker_width + 8.0)
            else:
                left = max(32.0099,20+3.35*scale+8) if kind == "bullet" else CONTENT_RECT.x0
            story = make_story(block, css)
            _, bounds = story.place(fitz.Rect(left, 0, CONTENT_RECT.x1, 1000000))
            height = bounds[3]
            story.reset()
            keep_height = height
            if kind in {"heading", "subheading"} and index + 1 < len(blocks):
                next_block = blocks[index + 1]
                next_left = 45.0 if next_block["type"] == "number" else 32.0099 if next_block["type"] == "bullet" else CONTENT_RECT.x0
                next_story = make_story(next_block, css)
                _, next_bounds = next_story.place(fitz.Rect(next_left, 0, CONTENT_RECT.x1, 1000000))
                keep_height += 8 + min(next_bounds[3], block_line_height(next_block))
            if page is None:
                page = new_page(); y = CONTENT_RECT.y0
            if keep_height <= CONTENT_RECT.height and y + keep_height > CONTENT_RECT.y1:
                page = new_page(); y = CONTENT_RECT.y0
            if height <= CONTENT_RECT.height and y + height > CONTENT_RECT.y1:
                page = new_page(); y = CONTENT_RECT.y0
            first = True
            while True:
                more, filled = story.place(fitz.Rect(left, y, CONTENT_RECT.x1, CONTENT_RECT.y1))
                if filled[3] <= y + 0.01:
                    if y <= CONTENT_RECT.y0 + 0.01:
                        raise ValueError("A block cannot fit in the locked content region.")
                    page = new_page(); y = CONTENT_RECT.y0
                    continue
                if first and kind in {"bullet", "number"}:
                    if kind == "bullet":
                        page.draw_circle((20, y + 23.5*scale), 3.35*scale, color=BROWN, fill=BROWN)
                    else:
                        page.insert_font(fontname="Number", fontfile=str(body))
                        page.insert_text((16, y + 24.1*scale), f'{block["number"]}.',
                                         fontname="Number", fontsize=size, color=BLACK)
                draw_story(page, story)
                placements.append({"page": len(output)-1, "source_index": block["source_index"],
                                   "type": kind, "font_size":size,"line_height":block_line_height(block),
                                   "color":block.get("color"),
                                   "rect": [left, y, CONTENT_RECT.x1, min(filled[3],CONTENT_RECT.y1)]})
                first = False
                y = filled[3]
                if not more:
                    break
                page = new_page(); y = CONTENT_RECT.y0
            y += 8 if kind in {"heading", "subheading"} else 15.9936
        count = len(output)
        output.set_metadata({"title": title, "creator": "Local Tamil magazine generator"})
        data = compact_pdf(output) if enforce_size else output.tobytes(garbage=4,deflate=True)
    path = None
    if save:
        OUTPUT_DIR.mkdir(exist_ok=True)
        path = OUTPUT_DIR / f"tamil_magazine_{selected:%Y%m%d}_{uuid4().hex[:10]}.pdf"
        path.write_bytes(data)
    return Generation(path, count, font_notices(), data, placements)

def preview_pages(result: Generation) -> list[dict]:
    pages = []
    with fitz.open(stream=result.data, filetype="pdf") as document:
        for index, page in enumerate(document):
            png = page.get_pixmap(matrix=fitz.Matrix(1.4,1.4)).tobytes("png")
            pages.append({"image":base64.b64encode(png).decode("ascii"),
                          "placements":[p for p in result.placements if p["page"] == index]})
    return pages

def blank_preview(selected: date, uploads=None, small_crop=None, photo_crops=None, template_id="original") -> Generation:
    if template_id != "original":
        return render_template(globals(),[],selected,uploads,photo_crops,{}, {},False,True,template_id)
    body, _ = font_paths()
    ensure_bold_font(body)
    with fitz.open(FRONT_TEMPLATE) as front, fitz.open(CONTENT_TEMPLATE) as content, fitz.open() as document:
        page = add_template(document,front)
        draw_photos(page,uploads,front=True,small_crop=small_crop,photo_crops=photo_crops)
        page = add_template(document,content)
        draw_photos(page,uploads,front=False,photo_crops=photo_crops)
        draw_header(page,selected,2,body)
        data = compact_pdf(document)
    return Generation(None,2,[],data,[])

def main() -> None:
    import streamlit as st
    import streamlit.components.v1 as components
    st.set_page_config(page_title="Tamil Magazine PDF", page_icon="📰", layout="wide")
    st.title("Tamil Magazine PDF Generator")
    template_id = st.selectbox("Magazine template",list(TEMPLATES),format_func=TEMPLATES.get,key="magazine_template")
    auto_preview = st.toggle("Auto preview refresh",value=True,key="auto_preview")
    refresh_preview = st.session_state.pop("refresh_preview_requested",False)
    selected = st.date_input("Date", value=datetime.now(ZoneInfo("Asia/Kolkata")).date(), format="DD.MM.YYYY")
    try:
        notices = font_notices()
        if notices:
            with st.expander("Font information"):
                for notice in notices:
                    st.caption(notice)
        body, _ = font_paths()
        font_data = base64.b64encode(body.read_bytes()).decode("ascii")
    except Exception as error:
        st.error(str(error)); return
    st.subheader("Magazine photos")
    uploads = {}
    labels = {"left":"Front left photo", "center":"Front center photo", "right":"Front right photo",
              "small":"Small front photo"}
    if template_id not in {"original","short"}:
        labels = {"center":"Large front photo (optional)"}
        st.caption("One upload fills the large photo shape in this template. The original magazine uses four front photos.")
    for column, (slot, label) in zip(st.columns(len(labels)), labels.items()):
        with column:
            uploaded = st.file_uploader(label, type=["png","jpg","jpeg","webp"], key=f"photo_{slot}")
            if uploaded is not None:
                uploads[slot] = uploaded.getvalue()
    with st.expander("Image movement / crop controls"):
        st.caption("Move and zoom each photo inside its existing frame.")
        photo_crops = {}
        names = {slot:slot.title() for slot in labels}
        for tab,(slot,name) in zip(st.tabs(list(labels.values())),names.items()):
            with tab:
                photo_crops[slot] = {
                    "zoom":st.slider(f"{name} photo zoom",1.0,3.0,1.0,0.05,disabled=slot not in uploads),
                    "pan_x":st.slider(f"{name} photo horizontal crop",-1.0,1.0,0.0,0.05,disabled=slot not in uploads),
                    "pan_y":st.slider(f"{name} photo vertical crop",-1.0,1.0,0.0,0.05,disabled=slot not in uploads)}
    article = st.session_state.get("article",[])
    revision = st.session_state.get("article_revision","initial")
    # Photos never change pagination. Measure it once per article revision so
    # upload boxes appear for exactly the current content pages, even if a
    # photo-filled export cannot satisfy the size limit yet.
    layout_signature = repr((template_id,article))
    if (auto_preview or refresh_preview) and layout_signature != st.session_state.get("oval_layout_signature"):
        try:
            count = generate_pdf(article,selected,save=False,oval_photos={},enforce_size=False,template_id=template_id).page_count if article else 1
            st.session_state["oval_page_count"] = count
        except Exception:
            st.session_state["oval_page_count"] = 1
        st.session_state["oval_layout_signature"] = layout_signature
    oval_photos = {}
    page_count = st.session_state.get("oval_page_count",1)
    saved_crops = st.session_state.setdefault("oval_crop_values",{})
    oval_crops = {number:dict(saved_crops.get(number,{
                    "zoom":st.session_state.get(f"oval_zoom_{number}",1.0),
                    "pan_x":st.session_state.get(f"oval_x_{number}",0.0),
                    "pan_y":st.session_state.get(f"oval_y_{number}",0.0)}))
                  for number in range(2,page_count+1)}
    saved_crops.update({number:dict(crop) for number,crop in oval_crops.items()})
    photo_name = "oval" if template_id=="original" else "photo"
    if template_id=="short":
        page_count=1
    with st.sidebar:
        st.header("Oval photos" if template_id=="original" else "Content-page photos")
        st.caption("Adjust a page here while keeping its PDF preview visible.")
        if template_id=="short":
            st.caption("Short PDF content pages have no photo slots, matching the supplied template.")
        elif page_count < 2:
            st.caption("Paste your article to show its content-page controls.")
        else:
            selected_page = st.selectbox("Oval page" if template_id=="original" else "Photo page",list(range(2,page_count+1)),
                                         format_func=lambda number:f"Page {number}",key="oval_selected_page")
            movement_panel = st.container()
            uploads_by_page, shown_by_page = {}, {}
            with st.expander("Page uploads (optional)"):
                st.caption("Leave an upload empty or turn off Show photo to keep the photo area blank.")
                for number in range(2,page_count+1):
                    uploaded = st.file_uploader(f"Page {number} photo (optional)",
                                               type=["png","jpg","jpeg","webp"],key=f"oval_page_{number}")
                    show = st.checkbox(f"Show photo on Page {number}",value=True,key=f"oval_show_{number}")
                    uploads_by_page[number], shown_by_page[number] = uploaded, show
                    if uploaded is not None and show:
                        oval_photos[number] = uploaded.getvalue()
            with movement_panel:
                number = selected_page
                disabled = uploads_by_page[number] is None or not shown_by_page[number]
                if disabled:
                    st.caption(f"Upload and enable a photo for Page {number} in Page uploads to adjust it.")
                with st.expander(f"Page {number} {photo_name} movement / crop",expanded=True):
                    current = oval_crops[number]
                    oval_crops[number] = {
                        "zoom":st.slider(f"Page {number} {photo_name} zoom",1.0,3.0,current["zoom"],0.05,key=f"oval_zoom_{number}",disabled=disabled),
                        "pan_x":st.slider(f"Page {number} {photo_name} horizontal crop",-1.0,1.0,current["pan_x"],0.05,key=f"oval_x_{number}",disabled=disabled),
                        "pan_y":st.slider(f"Page {number} {photo_name} vertical crop",-1.0,1.0,current["pan_y"],0.05,key=f"oval_y_{number}",disabled=disabled)}
                    saved_crops[number] = dict(oval_crops[number])
    signature = ("blank-template-natural-spacing-v6",template_id,MAX_PDF_BYTES,selected.isoformat(),repr(article),repr(photo_crops),repr(oval_crops),
                 tuple((slot,hashlib.sha256(data).hexdigest()) for slot,data in uploads.items()),
                 tuple((number,hashlib.sha256(data).hexdigest()) for number,data in oval_photos.items()))
    fingerprint = hashlib.sha256(repr(signature).encode()).hexdigest()
    if (auto_preview or refresh_preview) and fingerprint != st.session_state.get("preview_fingerprint"):
        try:
            result = generate_pdf(article,selected,uploads,save=False,photo_crops=photo_crops,
                                  oval_photos=oval_photos,oval_crops=oval_crops,template_id=template_id) if article else blank_preview(selected,uploads,photo_crops=photo_crops,template_id=template_id)
            st.session_state["preview_result"] = result if article else None
            st.session_state["preview_pages"] = preview_pages(result)
            st.session_state["preview_error"] = ""
        except Exception as error:
            st.session_state["preview_result"] = None
            st.session_state["preview_pages"] = []
            st.session_state["preview_error"] = str(error)
        st.session_state["preview_fingerprint"] = fingerprint
        st.session_state["preview_revision"] = revision
    if fingerprint == st.session_state.get("preview_fingerprint"):
        # Editor revisions also change for no-op formatting, undo, or edits
        # restored to the cached content. Reuse the PDF but acknowledge the
        # newest revision so the component does not stay pending forever.
        st.session_state["preview_revision"] = revision
    if st.session_state.get("generated_for") != fingerprint:
        st.session_state.pop("pdf_bytes",None)
    component = components.declare_component("tamil_article", path=str(ROOT / "editor"))
    value = component(font=font_data,initial=article,initial_revision=revision,
                      preview=st.session_state.get("preview_pages",[]),preview_revision=st.session_state.get("preview_revision","initial"),
                      preview_error=st.session_state.get("preview_error",""),context=fingerprint,
                      auto_preview=auto_preview,preview_stale=fingerprint!=st.session_state.get("preview_fingerprint"),
                      uploaded_slots=list(uploads),natural_spacing=template_id!="original",
                      key="article_editor",default=None)
    if value:
        incoming = value.get("blocks",[])
        if repr(incoming) != repr(article) or value.get("revision",revision) != revision:
            st.session_state["article"] = incoming
            st.session_state["article_revision"] = value.get("revision",revision)
            st.rerun()
        request = value.get("request")
        refresh_request = value.get("refresh_request")
        if refresh_request and refresh_request != st.session_state.get("processed_refresh_request"):
            st.session_state["processed_refresh_request"] = refresh_request
            st.session_state["refresh_preview_requested"] = True
            st.rerun()
        if request and request != st.session_state.get("processed_request"):
            if fingerprint != st.session_state.get("preview_fingerprint"):
                st.session_state["refresh_preview_requested"] = True
                st.rerun()
            st.session_state["processed_request"] = request
            st.session_state.pop("pdf_bytes", None)
            try:
                result = st.session_state.get("preview_result")
                if result is None:
                    raise ValueError(st.session_state.get("preview_error") or "Paste an article before generating.")
                OUTPUT_DIR.mkdir(exist_ok=True)
                name = f"tamil_magazine_{selected:%Y%m%d}_{uuid4().hex[:10]}.pdf"
                (OUTPUT_DIR / name).write_bytes(result.data)
                st.session_state["pdf_bytes"] = result.data
                st.session_state["pdf_name"] = name
                st.session_state["pdf_pages"] = result.page_count
                st.session_state["generated_for"] = fingerprint
            except Exception as error:
                st.error(f"PDF generation failed: {error}")
    if "pdf_bytes" in st.session_state:
        st.success(f'PDF generated: {st.session_state["pdf_pages"]} pages.')
        st.caption(f'PDF size: {len(st.session_state["pdf_bytes"])/1000:.1f} KB (limit: 600 KB)')
        st.download_button("DOWNLOAD PDF", data=st.session_state["pdf_bytes"],
                           file_name=st.session_state["pdf_name"], mime="application/pdf", use_container_width=True)

if __name__ == "__main__":
    main()
