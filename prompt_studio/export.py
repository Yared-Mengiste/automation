"""Unicode PDF rendering with explicit glyph coverage checks."""

import os
from functools import lru_cache
from pathlib import Path

from fontTools.ttLib import TTFont
from fpdf import FPDF
from fpdf.enums import XPos, YPos

from .models import ParsedSource


@lru_cache(maxsize=8)
def _coverage(path: str) -> frozenset[int]:
    with TTFont(path) as font:
        return frozenset(font.getBestCmap())


def font_paths() -> list[Path]:
    candidates = [
        os.getenv("PDF_FONT_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        os.getenv(
            "PDF_ETHIOPIC_FONT_PATH", "/usr/share/fonts/truetype/noto/NotoSansEthiopic-Regular.ttf"
        ),
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansEthiopic-Regular.ttf",
    ]
    return list(dict.fromkeys(Path(p) for p in candidates if Path(p).is_file()))


def validate_glyphs(text: str) -> list[Path]:
    paths = font_paths()
    if not paths:
        raise ValueError(
            "Install fonts-dejavu-core and fonts-noto-core, or configure PDF_FONT_PATH and PDF_ETHIOPIC_FONT_PATH."
        )
    coverage = set().union(*(_coverage(str(p)) for p in paths))
    unsupported = sorted({ord(c) for c in text if not c.isspace() and ord(c) not in coverage})
    if unsupported:
        points = ", ".join(f"U+{code:04X}" for code in unsupported[:12])
        raise ValueError(
            f"Unsupported PDF glyphs: {points}. Install a Unicode font covering this language before generating."
        )
    return paths


def render_sections(source: ParsedSource, title: str) -> bytes:
    paths = validate_glyphs(source.text() + title)
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=16)
    families = []
    for i, path in enumerate(paths):
        family = f"Body{i}"
        pdf.add_font(family, fname=str(path))
        families.append(family)
    pdf.set_font(families[0], size=10)
    pdf.set_fallback_fonts(families[1:])
    pdf.set_title(title)
    pdf.set_author("Character Prompt Studio")
    for section in source.sections:
        pdf.add_page()
        index = 0
        while index < len(section.blocks):
            block = section.blocks[index]
            text = block.text
            # Keep the dialect's comma between the protected timing header and
            # editable camera/action prose, including after PDF extraction.
            if (
                block.kind == "shot_header"
                and index + 1 < len(section.blocks)
                and section.blocks[index + 1].kind == "prose"
            ):
                index += 1
                text += ", " + section.blocks[index].text
            pdf.multi_cell(0, 5.3, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT, wrapmode="WORD")
            pdf.ln(2)
            index += 1
    return bytes(pdf.output())
