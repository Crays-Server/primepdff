"""Additional PrimePDF layouts; the original rendering path stays in app.py.

Coordinates use PDF points. Backgrounds contain artwork only, with photo masks
stored separately so an omitted upload never exposes a reference photograph.
"""
from io import BytesIO
from pathlib import Path
from functools import lru_cache
import fitz
from PIL import Image, ImageOps
from photos import cover

ROOT = Path(__file__).resolve().parent
TEMPLATES = {"original": "Original magazine", "kids": "Children / toys (Blank)",
             "fabric": "Fabric / flowers (Blank 1)", "blue": "Blue geometric (Blank 2)",
             "teal": "Cream / teal (Blank 3)", "short": "Short PDF"}
PROFILES = {
    "short": dict(title=[28,630,568,800],color="#a8387b",regions=[[[30,35,565,805]]]),
    "kids": dict(title=[28,60,580,252], color="#202bce", date=[76,38,211,67], date_color="#ffffff", number=[289,807,313,833],
                 regions=[[[25,90,570,796]],[[25,90,570,796]]]),
    "fabric": dict(title=[29,535,566,734], color="#495400", date=[42,34,198,60], number=[283,802,307,823],
                   regions=[[[27,88,565,795]]]),
    "blue": dict(title=[48,58,554,260], title_align="left", color="#ffffff", date=[38,25,158,49], number=[540,812,559,832],
                 regions=[[[35,82,565,795]],[[35,82,565,795]]]),
    "teal": dict(title=[30,68,565,260], color="#005c4e", date=[30,41,180,64], number=[282,798,312,820],
                 regions=[[[30,85,565,790]]]),
}

# Photo/frame envelopes include overlapping template ornaments (for example the
# teddy beneath the tilted frame). Flowers remain usable background artwork.
PHOTO_EXCLUSIONS = {
    "kids": [[299,115,583,445],[20,477,300,800]],
    "fabric": [[345,102,575,420]],
    "blue": [[335,132,568,428],[40,535,280,809]],
    "teal": [[320,170,558,420]],
}

def flow_regions(template, variant_index):
    """One logical page area, with automatic flow around its photo envelope."""
    profile=PROFILES[template]
    area=fitz.Rect(profile["regions"][variant_index][0])
    if template=="short":
        return [area]
    photo=fitz.Rect(PHOTO_EXCLUSIONS[template][variant_index])
    above=fitz.Rect(area.x0,area.y0,area.x1,min(area.y1,photo.y0-6))
    below=fitz.Rect(area.x0,max(area.y0,photo.y1+6),area.x1,area.y1)
    top,bottom=max(area.y0,photo.y0-6),min(area.y1,photo.y1+6)
    if (photo.x0+photo.x1)/2 > (area.x0+area.x1)/2:
        beside=fitz.Rect(area.x0,top,photo.x0-8,bottom)
    else:
        beside=fitz.Rect(photo.x1+8,top,area.x1,bottom)
    return [r for r in (above,beside,below) if r.width>0 and r.height>0]

@lru_cache(maxsize=32)
def photo_image(template, variant, data, zoom=1., pan_x=0., pan_y=0.):
    if len(data) > 25*1024*1024:
        raise ValueError("Each photo must be smaller than 25 MB.")
    with Image.open(ROOT / f"assets/templates/{template}-{variant}-mask.png") as source:
        mask = source.convert("L")
    box = mask.getbbox()
    with Image.open(BytesIO(data)) as source:
        image = cover(ImageOps.exif_transpose(source).convert("RGB"),box[2]-box[0],box[3]-box[1],zoom,pan_x,pan_y)
    layer = Image.new("RGB",mask.size,"white")
    layer.paste(image,box[:2])
    layer = layer.convert("RGBA")
    layer.putalpha(mask)
    result = BytesIO(); layer.save(result,format="PNG")
    return result.getvalue()

def background(output, template, variant, data=None, crop=None):
    page = output.new_page(width=595.2000122,height=841.9199829)
    page.insert_image(page.rect,filename=str(ROOT / f"assets/templates/{template}-{variant}.jpg"))
    if data:
        page.insert_image(page.rect,stream=photo_image(template,variant,data,**(crop or {})))
    return page

def render(api, article, selected, uploads, photo_crops, oval_photos, oval_crops, save, enforce_size, template):
    """Continue each paragraph around images and onto following pages."""
    profile = PROFILES[template]
    blocks = api["clean_blocks"](article) if article else []
    for block in blocks:
        # Old editor states saved justification as a default. Natural spacing
        # should replace that implicit setting, while deliberate choices remain.
        if block.get("align")=="justify" and not block.get("align_explicit"):
            block["align"]="left"
    body, sub = api["font_paths"](); api["ensure_bold_font"](body)
    css = api["story_css"](body,sub)
    blank = not blocks
    title = blocks.pop(next((i for i,b in enumerate(blocks) if b["type"]=="heading"),0)) if blocks else None
    placements = []
    with fitz.open() as output:
        page = background(output,template,"front",None if template=="short" else (uploads or {}).get("center"),(photo_crops or {}).get("center"))
        if template=="short":
            for slot in ("left","center","right","small"):
                if (uploads or {}).get(slot):
                    page.insert_image(page.rect,stream=photo_image(template,f"front-{slot}",uploads[slot],**(photo_crops or {}).get(slot,{})))
        if title:
            target = fitz.Rect(profile["title"])
            maximum=round(title.get("font_size") or 42)
            for size in range(maximum,min(maximum,18)-1,-1):
                block = dict(title,font_size=size,align=profile.get("title_align","center"),color=title.get("color") or profile["color"])
                story = api["make_story"](block,css)
                more, _ = story.place(fitz.Rect(0,0,target.width,target.height))
                if not more: break
            else: raise ValueError("The title is too long for this template. Reduce its size or add line breaks.")
            story.reset()
            api["centered_story"](page,story,target,align=profile.get("title_align","center"))
            placements.append(dict(page=0,source_index=title["source_index"],type="heading",rect=list(target),
                                   font_size=size,line_height=api["block_line_height"](block),edit_bottom=target.y1,color=block["color"],align=block["align"]))
        page = None; regions=[]; region_index=0; y=0
        def new_page():
            number = len(output)+1
            variant_index = (number-2)%len(profile["regions"])
            variant = f"body{variant_index}"
            photo = None if template=="short" else (oval_photos or {}).get(number)
            page = background(output,template,variant,photo,(oval_crops or {}).get(number))
            if template=="short":
                return page,flow_regions(template,variant_index)
            if template=="kids":
                page.draw_circle((301,820),10,color=None,fill=(.65,0,.45))
            header_css = css + "body{font-size:14pt;}p{line-height:19pt;}"
            for text,rect in [(selected.strftime("%d-%m-%Y"),profile["date"]),(str(number),profile["number"])]:
                color="#ffffff" if template in {"kids","blue"} and text==str(number) else "#0656a3" if template=="blue" else profile["color"]
                if text!=str(number):
                    color=profile.get("date_color",color)
                story = api["make_story"]({"type":"body","runs":[{"text":text,"bold":False,"color":color}]},header_css)
                api["centered_story"](page,story,fitz.Rect(rect))
            if template=="teal":
                for start,end in [((190,58),(565,58))]:
                    page.draw_line(start,end,color=(0,.36,.31),width=.6)
            return page,flow_regions(template,variant_index)
        for block in blocks:
            api["validate_text"](block["text"],sub if block["type"] in {"heading","subheading"} else body)
            # Keep list labels in the shaped text, including wrapped continuations.
            if block["type"] in {"number","bullet"}:
                marker = f'{block["number"]}. ' if block["type"]=="number" else "• "
                block = dict(block,runs=[{"text":marker,"bold":False,"bullet_marker":block["type"]=="bullet"}]+block["runs"],text=marker+block["text"])
            story=api["make_story"](block,css)
            while True:
                if page is None:
                    page,regions=new_page(); region_index=0; y=regions[0].y0
                region=regions[region_index]
                if y>=region.y1:
                    region_index+=1
                    if region_index>=len(regions): page=None
                    else: y=regions[region_index].y0
                    continue
                more,filled=story.place(fitz.Rect(region.x0,y,region.x1,region.y1))
                if filled[3]>y+.01:
                    api["draw_story"](page,story)
                    placements.append(dict(page=len(output)-1,source_index=block["source_index"],type=block["type"],
                        font_size=api["block_font_size"](block),line_height=api["block_line_height"](block),
                        color=block.get("color"),align=block["align"],rect=[region.x0,y,region.x1,min(filled[3],region.y1)],edit_bottom=region.y1))
                    y=filled[3]+(8 if block["type"] in {"heading","subheading"} else 15.9936)
                    if not more: break
                elif y<=region.y0+.01 and region_index==len(regions)-1:
                    raise ValueError("Text cannot fit this template's region. Reduce the font size.")
                region_index+=1
                if region_index>=len(regions): page=None
                else: y=regions[region_index].y0
        if blank: new_page()
        output.set_metadata({"title":title["text"] if title else "PrimePDF template preview","creator":"PrimePDF"})
        count=len(output)
        data=api["compact_pdf"](output) if enforce_size else output.tobytes(garbage=4,deflate=True)
    path=None
    if save:
        from uuid import uuid4
        api["OUTPUT_DIR"].mkdir(exist_ok=True)
        path=api["OUTPUT_DIR"] / f"tamil_magazine_{selected:%Y%m%d}_{uuid4().hex[:10]}.pdf"
        path.write_bytes(data)
    return api["Generation"](path,count,api["font_notices"](),data,placements)
