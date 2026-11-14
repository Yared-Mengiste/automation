import io
from pathlib import Path

import pytest
from conftest import FIXTURE
from pypdf import PdfReader, PdfWriter

from prompt_studio.export import render_sections, validate_glyphs
from prompt_studio.pdf_source import parse_text, read_pdf


def test_sample_has_independent_timelines():
    sample = Path("banana-cake.pdf")
    if not sample.exists():
        pytest.skip("Personal acceptance PDF is not included in the public repository.")
    source = read_pdf(sample.read_bytes())
    assert [(s.duration, len(s.shots)) for s in source.sections] == [(14, 8), (22, 4)]
    assert all(s.shots[0].start == 0 for s in source.sections)
    assert "antioxidants. So soft and so fluffy." in source.text()


def test_repeated_mapping_and_dialogue_spanning_pages(source):
    assert [(s.duration, len(s.shots)) for s in source.sections] == [(14, 8), (22, 4)]
    assert len([b for s in source.sections for b in s.blocks if b.kind == "dialogue"]) == 12
    assert "loaded with antioxidants." in source.text()


def test_fragmented_extraction():
    text = (
        FIXTURE.read_text()
        .replace("FORMAT:", "F O R M A T:")
        .replace("SHOT ", "S H O T ")
        .replace("@image_1", "@ i m a g e _ 1")
        .replace("Reference Mapping:", "R E F E R E N C E M A P P I N G:")
    )
    assert len(parse_text(text).sections) == 2


@pytest.mark.parametrize(
    "old,new",
    [
        ("22s / 4 SHOTS", "22s / 5 SHOTS"),
        ("0:03 to 0:07", "0:04 to 0:07"),
        ("0:14 to 0:22", "0:14 to 0:23"),
        ("SHOT 2 —", "SHOT 8 —"),
        ("@image_1", "@image_2"),
        ("SHOT 1 — 0:00 to 0:02", "SHOT 1 — anytime"),
    ],
)
def test_reject_ambiguous_or_invalid_structures(old, new):
    with pytest.raises(ValueError):
        parse_text(FIXTURE.read_text().replace(old, new))


def test_reject_inconsistent_mapping():
    text = FIXTURE.read_text().replace("The man — lean", "The woman — lean", 1)
    with pytest.raises(ValueError, match="Repeated"):
        parse_text(text)


def test_reject_unclosed_dialogue():
    with pytest.raises(ValueError, match="Unclosed"):
        parse_text(FIXTURE.read_text().replace('eggs"', "eggs"))


def test_pdf_round_trip(source):
    result = read_pdf(render_sections(source, "Test"))
    assert [(s.duration, s.shots) for s in source.sections] == [
        (s.duration, s.shots) for s in result.sections
    ]
    assert [b.text for s in source.sections for b in s.blocks if b.kind == "dialogue"] == [
        b.text for s in result.sections for b in s.blocks if b.kind == "dialogue"
    ]


def test_ethiopic_round_trip(source):
    source.sections[0].blocks[1].text += " ማያ ኢትዮጵያ"
    pdf = render_sections(source, "ማያ")
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert "ማያ ኢትዮጵያ" in text


def test_unsupported_glyphs_rejected():
    with pytest.raises(ValueError, match=r"U\+10FFFF"):
        validate_glyphs("unsupported \U0010ffff")


def test_unreadable_scanned_pdf_rejected():
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(ValueError, match="scanned"):
        read_pdf(output.getvalue())


def test_encrypted_rejected(pdf):
    writer = PdfWriter(clone_from=io.BytesIO(pdf))
    writer.encrypt("password")
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(ValueError, match="Encrypted"):
        read_pdf(output.getvalue())


@pytest.mark.parametrize("data", [b"not pdf", b"", b"%PDF-" + b"x" * 10_000_000])
def test_invalid_or_oversize_pdf_rejected(data):
    with pytest.raises(ValueError):
        read_pdf(data)


def test_page_limit():
    writer = PdfWriter()
    for _ in range(31):
        writer.add_blank_page(width=100, height=100)
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(ValueError, match="30 pages"):
        read_pdf(output.getvalue())


def test_text_limit():
    with pytest.raises(ValueError, match="40,000"):
        parse_text("a" * 40_001)
