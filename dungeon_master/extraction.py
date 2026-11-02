import posixpath
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from bs4 import BeautifulSoup
from defusedxml import ElementTree
from pypdf import PdfReader


class BookError(ValueError):
    """A book cannot be processed; this message is safe to display to its owner."""


@dataclass(frozen=True)
class Section:
    text: str
    source_label: str


@dataclass
class ExtractedBook:
    sections: list[Section]
    page_count: int
    warnings: list[str]


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Rejoin words hyphenated at a line break without joining paragraphs.
    text = re.sub(r"(?<=\w)-\n(?=[a-z])", "", text)
    text = re.sub(r"[^\S\n]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_book(path: Path, file_format: str, max_characters: int, progress=None) -> ExtractedBook:
    notify = progress or (lambda done, total: None)
    if file_format == "pdf":
        result = extract_pdf(path, max_characters, notify)
    elif file_format == "epub":
        result = extract_epub(path, max_characters, notify)
    else:
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
        except UnicodeError as exc:
            raise BookError("This text file could not be read. Save it as UTF-8 and upload it again.") from exc
        text = clean_text(text)
        result = ExtractedBook([Section(text, "Text")], 1, [])
    total = sum(len(section.text) for section in result.sections)
    if total > max_characters:
        raise BookError("This book contains too much text. Split it into smaller volumes and upload each one.")
    if not any(section.text.strip() for section in result.sections):
        raise BookError("No readable text was found. Scanned or image-only books need OCR before uploading.")
    return result


def extract_pdf(path, max_characters, notify):
    try:
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise BookError("This PDF is password protected. Upload an unlocked copy.")
        total = len(reader.pages)
        if total > 5000:
            raise BookError("This PDF has more than 5,000 pages. Split it into smaller volumes.")
        sections, skipped, characters = [], [], 0
        for index, page in enumerate(reader.pages):
            text = clean_text(page.extract_text() or "")
            characters += len(text)
            if characters > max_characters:
                raise BookError("This PDF contains too much text. Split it into smaller volumes.")
            if text:
                sections.append(Section(text, f"Page {index + 1}"))
            else:
                skipped.append(index + 1)
            notify(index + 1, total)
        warnings = []
        if skipped:
            sample = ", ".join(map(str, skipped[:12]))
            warnings.append(f"{len(skipped)} page(s) had no extractable text and were skipped "
                            f"(PDF pages {sample}{'…' if len(skipped) > 12 else ''}). "
                            "Images and scanned pages require OCR.")
        return ExtractedBook(sections, total, warnings)
    except BookError:
        raise
    except Exception as exc:
        raise BookError("This PDF could not be read. It may be damaged or use unsupported encryption.") from exc


def extract_epub(path, max_characters, notify):
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 10000 or sum(info.file_size for info in entries) > max_characters * 5:
                raise BookError("This EPUB expands to too much data. Split it into smaller volumes.")
            if any(info.flag_bits & 1 for info in entries) or "META-INF/encryption.xml" in archive.namelist():
                raise BookError("This EPUB has encrypted content. Upload a DRM-free copy.")
            container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
            rootfile = container.find(".//{*}rootfile")
            if rootfile is None:
                raise BookError("This EPUB is missing its book manifest.")
            opf_path = rootfile.attrib["full-path"]
            package = ElementTree.fromstring(archive.read(opf_path))
            manifest = {item.attrib["id"]: item for item in package.findall(".//{*}manifest/{*}item")}
            spine = package.findall(".//{*}spine/{*}itemref")
            if not spine:
                raise BookError("This EPUB does not have a readable chapter order.")
            sections, characters = [], 0
            for index, reference in enumerate(spine):
                item = manifest[reference.attrib["idref"]]
                href = unquote(item.attrib["href"].split("#")[0])
                member = posixpath.normpath(posixpath.join(posixpath.dirname(opf_path), href))
                if member.startswith(("../", "/")):
                    raise BookError("This EPUB has an invalid chapter path.")
                soup = BeautifulSoup(archive.read(member), "html.parser")
                heading = soup.find(["h1", "h2", "title"])
                title = heading.get_text(" ", strip=True)[:160] if heading else ""
                for element in soup(["script", "style", "head", "nav"]):
                    element.decompose()
                text = clean_text(soup.get_text("\n", strip=True))
                characters += len(text)
                if characters > max_characters:
                    raise BookError("This EPUB contains too much text. Split it into smaller volumes.")
                if text:
                    label = f"Section {index + 1}" + (f" · {title}" if title else "")
                    sections.append(Section(text, label))
                notify(index + 1, len(spine))
            return ExtractedBook(sections, len(spine), [])
    except BookError:
        raise
    except Exception as exc:
        raise BookError("This EPUB could not be read. Upload a valid, DRM-free EPUB.") from exc
