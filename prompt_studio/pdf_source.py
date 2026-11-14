"""Parse the supported labeled PDF dialect before any paid call."""

import hashlib
import io
import re
import unicodedata

from pypdf import PdfReader

from .models import ParsedSource, PromptSection, SourceBlock, SourceShot
from .settings import Settings

MAPPING_HEADER = re.compile(r"^Reference\s+Mapping\s*:\s*$", re.I)
FORMAT = re.compile(
    r"^FORMAT:\s*(\d+(?:\.\d+)?)\s*(?:s|seconds)\s*/\s*(\d+)\s+SHOTS\b\s*(?:/\s*)?", re.I
)
TIME = r"(?:\d+:\d{2}(?:\.\d+)?|\d+(?:\.\d+)?s?)"
SHOT = re.compile(rf"^(SHOT\s+(\d+)\s*[—–-]\s*({TIME})\s+to\s+({TIME}))(?=\s*[,;.]|\s*$)", re.I)
LABEL = re.compile(r"^[A-Z][A-Z _/—–-]{1,60}:")
DIALOGUE = re.compile(r'^(?:DIALOGUE\s*:|[^:]{1,50}:\s*["“])', re.I)
SECTION_LABEL = re.compile(r"^(?:PART|SECTION|PROMPT)\s+\d+\b", re.I)


def repair_whitespace(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\u00ad", "").replace("\r", "\n")
    # Repair letter-spaced labels, not normal words or dialogue.
    for label in ["REFERENCE MAPPING", "FORMAT", "SHOT", "SUBJECT", "WARDROBE", "DIALOGUE"]:
        pattern = r"[ \t]+".join(re.escape(c) for c in label.replace(" ", ""))
        text = re.sub(
            pattern, label.title() if label == "REFERENCE MAPPING" else label, text, flags=re.I
        )
    text = re.sub(r"@\s*i\s*m\s*a\s*g\s*e\s*_\s*(\d+)", r"@image_\1", text, flags=re.I)
    return "\n".join(re.sub(r"[^\S\n]+", " ", line).strip() for line in text.splitlines())


def _seconds(value: str) -> float:
    value = value.rstrip("s")
    if ":" in value:
        minutes, seconds = value.split(":")
        if float(seconds) >= 60:
            raise ValueError("Invalid shot timestamp.")
        return int(minutes) * 60 + float(seconds)
    return float(value)


def _event(line: str) -> bool:
    return bool(
        MAPPING_HEADER.match(line)
        or line.startswith("@image_")
        or re.match(r"^(?:FORMAT:|SHOT\s+\d+\b)", line, re.I)
        or LABEL.match(line)
        or DIALOGUE.match(line)
        or SECTION_LABEL.match(line)
    )


def _chunks(text: str) -> list[str]:
    chunks, current = [], []
    for line in repair_whitespace(text).splitlines():
        if not line or re.fullmatch(r"[-—–=]{3,}", line):
            continue
        if _event(line) and current:
            chunks.append(" ".join(current))
            current = []
        current.append(line)
    if current:
        chunks.append(" ".join(current))
    return chunks


def parse_text(text: str, *, pages=0, settings: Settings | None = None) -> ParsedSource:
    settings = settings or Settings()
    if not text.strip() or len(text) > settings.max_characters:
        raise ValueError(
            "PDF must contain readable text under 40,000 extracted characters. OCR is outside v1."
        )
    if set(re.findall(r"@image_[\w-]+", repair_whitespace(text))) - {"@image_1"}:
        raise ValueError("Only one reference character, @image_1, is supported.")
    groups, current, heading = [], [], []
    for chunk in _chunks(text):
        if SECTION_LABEL.match(chunk):
            if current:
                groups.append(current)
                current = []
            heading.append(chunk)
        elif MAPPING_HEADER.match(chunk):
            if current:
                groups.append(current)
            current = heading + [chunk]
            heading = []
        elif current:
            current.append(chunk)
        else:
            raise ValueError("Each section must start with a Reference Mapping label.")
    if current:
        groups.append(current)
    if heading or not groups:
        raise ValueError("Ambiguous or empty prompt sections.")
    sections, mappings = [], []
    for number, chunks in enumerate(groups, 1):
        blocks, shots, declaration = [], [], None
        mapping_count = 0

        def add(kind, value, editable=False, *, number=number, blocks=blocks):
            block = SourceBlock(
                id=f"s{number}-b{len(blocks) + 1}", kind=kind, text=value, editable=editable
            )
            blocks.append(block)
            return block.id

        for chunk in chunks:
            if MAPPING_HEADER.match(chunk) or SECTION_LABEL.match(chunk):
                add("prose", chunk)
            elif chunk.startswith("@image_1"):
                if declaration or not re.fullmatch(r"@image_1\s*=\s*\S.*", chunk):
                    raise ValueError(
                        "Mapping must precede FORMAT and contain one complete description."
                    )
                mappings.append(re.sub(r"\s+", " ", chunk).strip())
                mapping_count += 1
                add("mapping", chunk)
            elif re.match(r"^FORMAT:", chunk, re.I):
                match = FORMAT.match(chunk)
                if not match or declaration or mapping_count != 1:
                    raise ValueError(
                        "Each section needs one mapping and one FORMAT: duration / count SHOTS declaration."
                    )
                declaration = (float(match[1]), int(match[2]))
                if not 0 < declaration[0] <= 600 or not 1 <= declaration[1] <= 100:
                    raise ValueError("Unsupported section duration or shot count.")
                add("format", chunk[: match.end()].rstrip())
                if chunk[match.end() :].strip():
                    add("prose", chunk[match.end() :].strip(), True)
            elif re.match(r"^SHOT\s+\d+\b", chunk, re.I):
                match = SHOT.match(chunk)
                if not match or not declaration:
                    raise ValueError("Timed SHOT blocks must follow a valid FORMAT declaration.")
                block_id = add("shot_header", match[1])
                shots.append(
                    SourceShot(
                        number=int(match[2]),
                        start=_seconds(match[3]),
                        end=_seconds(match[4]),
                        header_id=block_id,
                    )
                )
                rest = chunk[match.end() :].strip().lstrip(",;").strip()
                if rest:
                    add("prose", rest, True)
            elif DIALOGUE.match(chunk) and not chunk.upper().startswith("DIALOGUE ASSIGNMENT"):
                # Quoted dialogue can continue across page boundaries, but must close.
                quote = re.search(r'["“]', chunk)
                if quote and not chunk[quote.end() :].rstrip().endswith(('"', "”")):
                    raise ValueError("Unclosed or ambiguous quoted dialogue.")
                if not shots:
                    raise ValueError("Dialogue must belong to a timed shot.")
                add("dialogue", chunk)
            else:
                add("prose", chunk, True)
        if not declaration or mapping_count != 1 or len(shots) != declaration[1]:
            raise ValueError("Section shot count does not match its FORMAT declaration.")
        cursor = 0.0
        for index, shot in enumerate(shots, 1):
            if (
                shot.number != index
                or abs(shot.start - cursor) > 0.000001
                or shot.end <= shot.start
            ):
                raise ValueError(
                    "Each section must have consecutive shots starting at zero, without gaps or overlaps."
                )
            cursor = shot.end
        if abs(cursor - declaration[0]) > 0.000001:
            raise ValueError("Each section timeline must end at its declared duration.")
        sections.append(
            PromptSection(
                number=number,
                duration=declaration[0],
                declared_shots=declaration[1],
                blocks=blocks,
                shots=shots,
            )
        )
    if len(set(mappings)) != 1:
        raise ValueError(
            "Repeated @image_1 mappings must describe the same primary character exactly."
        )
    return ParsedSource(sections=sections, pages=pages, extracted_characters=len(text))


def read_pdf(data: bytes, settings: Settings | None = None) -> ParsedSource:
    settings = settings or Settings()
    if not 0 < len(data) <= settings.max_pdf_bytes:
        raise ValueError("PDF must be nonempty and at most 10 MB.")
    if not data.lstrip().startswith(b"%PDF-"):
        raise ValueError("Upload a valid text-based PDF.")
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs are unsupported. Export an unencrypted PDF.")
        if not 0 < len(reader.pages) <= settings.max_pages:
            raise ValueError("PDF must contain between 1 and 30 pages.")
        texts, count = [], 0
        for page in reader.pages:
            text = page.extract_text(extraction_mode="layout") or ""
            if not text.strip():
                raise ValueError(
                    "Unreadable or scanned page found. Export a text-based PDF; OCR is outside v1."
                )
            count += len(text)
            if count > settings.max_characters:
                raise ValueError("PDF exceeds 40,000 extracted characters.")
            texts.append(text)
        source = parse_text("\n".join(texts), pages=len(reader.pages), settings=settings)
        source.source_sha256 = hashlib.sha256(data).hexdigest()
        return source
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 — decoder exceptions may expose source contents.
        raise ValueError(
            "Could not read this PDF. Export a normal unencrypted text-based PDF."
        ) from exc
