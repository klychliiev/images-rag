import re
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from loguru import logger

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".svg", ".webp"}


def _direct_link_for(drive_links: dict, filename: str) -> str | None:
    url = drive_links.get(filename)
    if not url:
        return None

    if "drive.google.com" in url and "/uc?" not in url:
        try:
            file_id = url.split("/d/")[1].split("/")[0]
            return f"https://drive.google.com/uc?export=view&id={file_id}"
        except Exception:
            return url
    return url


class ODTProcessor:
    """Handles ODT file processing and conversion to Markdown"""

    @staticmethod
    def clean_text(text: str) -> str:
        """Clean and normalize text content"""
        if not text:
            return ""

        text = re.sub(r"\s+", " ", text.strip())

        text = text.replace("\n", " ").replace("\r", " ")

        return text

    @staticmethod
    def extract_images_from_odt(odt_path: Path, output_dir: Path) -> tuple:
        """Extract all images from ODT file and save to output directory"""
        if not odt_path.exists():
            logger.info(f"❌ ODT file not found: {odt_path}")
            return [], {}

        output_dir.mkdir(exist_ok=True)

        extracted_images = []
        image_mapping = {}

        try:
            with zipfile.ZipFile(odt_path, "r") as odt_zip:
                file_list = odt_zip.namelist()
                image_files = [
                    f
                    for f in file_list
                    if (f.startswith("Pictures/") or f.startswith("media/"))
                    and any(f.lower().endswith(ext) for ext in IMAGE_EXTS)
                ]

                if not image_files:
                    logger.debug("⚠️ No images found in ODT file")
                    return [], {}

                logger.info(f"📷 Found {len(image_files)} images in ODT file")

                for img_path in image_files:
                    img_data = odt_zip.read(img_path)
                    img_name = Path(img_path).name  # image1, image2 etc
                    img_name = re.sub(
                        "image\d+", f"image_{str(uuid.uuid4())[:5]}", img_name
                    )
                    output_path = output_dir / img_name

                    with open(output_path, "wb") as f:
                        f.write(img_data)

                    extracted_images.append(output_path)
                    image_mapping[img_path] = img_name
                    logger.success(f"✅ Extracted: {img_name}")

        except zipfile.BadZipFile:
            logger.error(f"❌ Invalid ODT file: {odt_path}")
            return [], {}
        except Exception as e:
            logger.error(f"❌ Error extracting images: {e}")
            return [], {}

        return extracted_images, image_mapping

    @staticmethod
    def extract_text_from_odt(odt_path: Path) -> str:
        """Extract text content from ODT file and convert to markdown"""
        if not odt_path.exists():
            raise FileNotFoundError(f"ODT file not found: {odt_path}")

        try:
            with zipfile.ZipFile(odt_path, "r") as odt_zip:
                content_xml = odt_zip.read("content.xml").decode("utf-8")
                root = ET.fromstring(content_xml)

                namespaces = {
                    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
                    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
                    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
                    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
                }

                markdown_content = []
                body = root.find(".//office:body/office:text", namespaces)
                if body is None:
                    return ""

                # Process elements in document order
                processed_elements = set()

                for elem in body:
                    if id(elem) in processed_elements:
                        continue

                    markdown_text = ODTProcessor.process_element(
                        elem, namespaces, processed_elements
                    )
                    if markdown_text:
                        markdown_content.append(markdown_text)

                result = "".join(markdown_content)

                # Clean up formatting
                result = re.sub(r"\n{3,}", "\n\n", result)
                result = re.sub(r"[ \t]+\n", "\n", result)

                return result

        except zipfile.BadZipFile:
            raise ValueError(f"Invalid ODT file: {odt_path}")
        except Exception as e:
            raise RuntimeError(f"Error extracting text from ODT: {e}")

    @staticmethod
    def process_element(elem, namespaces, processed_elements, level=0):
        """Process a single element and return markdown text"""
        if id(elem) in processed_elements:
            return ""

        processed_elements.add(id(elem))
        tag = elem.tag
        result = []

        if tag.endswith("}h"):
            outline_level = elem.get(
                "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}outline-level", "1"
            )
            text = ODTProcessor.clean_text("".join(elem.itertext()))
            if text:
                result.append("#" * int(outline_level) + " " + text + "\n\n")

        elif tag.endswith("}p"):
            text = ODTProcessor.clean_text("".join(elem.itertext()))
            if text and text not in ["\\", "", " "]:
                result.append(text + "\n\n")

        elif tag.endswith("}list"):
            for item in elem.findall(".//text:list-item", namespaces):
                processed_elements.add(id(item))
                text = ODTProcessor.clean_text("".join(item.itertext()))
                if text:
                    result.append("- " + text + "\n")
            result.append("\n")

        elif tag.endswith("}table"):
            table_md = ODTProcessor.process_table(elem, namespaces, processed_elements)
            if table_md:
                result.append(table_md)

        else:
            for child in elem:
                child_result = ODTProcessor.process_element(
                    child, namespaces, processed_elements, level + 1
                )
                if child_result:
                    result.append(child_result)

        return "".join(result)

    @staticmethod
    def process_table(table_elem, namespaces, processed_elements):
        """Process a table element and convert to markdown table"""
        rows = table_elem.findall(".//table:table-row", namespaces)
        if not rows:
            return ""

        table_data = []
        for row in rows:
            processed_elements.add(id(row))
            cells = row.findall(".//table:table-cell", namespaces)
            row_data = []
            for cell in cells:
                processed_elements.add(id(cell))
                cell_text = ODTProcessor.clean_text("".join(cell.itertext()))
                row_data.append(cell_text if cell_text else " ")
            if row_data:
                table_data.append(row_data)

        if not table_data:
            return ""

        result = []

        if table_data:
            result.append("| " + " | ".join(table_data[0]) + " |\n")
            result.append("| " + " | ".join(["---"] * len(table_data[0])) + " |\n")

            for row in table_data[1:]:
                while len(row) < len(table_data[0]):
                    row.append(" ")
                result.append("| " + " | ".join(row[: len(table_data[0])]) + " |\n")

            result.append("\n")

        return "".join(result)

    @staticmethod
    def extract_text_with_links(
        odt_path: Path, image_mapping: dict, drive_links: dict
    ) -> str:
        """
        Like extract_text_from_odt, but replaces image refs with Drive links when available.
        """
        if not odt_path.exists():
            raise FileNotFoundError(f"ODT file not found: {odt_path}")

        with zipfile.ZipFile(odt_path, "r") as odt_zip:
            content_xml = odt_zip.read("content.xml").decode("utf-8")
            root = ET.fromstring(content_xml)

            ns = {
                "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
                "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
                "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
                "xlink": "http://www.w3.org/1999/xlink",
                "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
            }

            body = root.find(".//office:body/office:text", ns)
            if body is None:
                return ""

            out = []
            processed = set()

            def img_md_for_href(href: str) -> str | None:
                target = None
                for original_path, mapped_filename in image_mapping.items():
                    if original_path.endswith(href) or href.endswith(
                        Path(original_path).name
                    ):
                        target = mapped_filename
                        break
                if not target:
                    return None
                link = _direct_link_for(drive_links, target)
                if not link:
                    return None
                return f"![{target}]({link})"

            def walk(elem):
                if id(elem) in processed:
                    return ""
                processed.add(id(elem))
                tag = elem.tag

                if tag.endswith("}h"):
                    level = elem.get(
                        "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}outline-level",
                        "1",
                    )
                    txt = ODTProcessor.clean_text("".join(elem.itertext()))
                    return ("#" * int(level)) + f" {txt}\n\n" if txt else ""

                if tag.endswith("}p"):
                    parts = []
                    if elem.text:
                        t = ODTProcessor.clean_text(elem.text)
                        if t:
                            parts.append(t)
                    for child in elem:
                        if child.tag.endswith("}frame"):
                            img = child.find(".//draw:image", ns)
                            if img is not None:
                                href = img.get("{http://www.w3.org/1999/xlink}href", "")
                                md = img_md_for_href(href)
                                if md:
                                    parts.append(md)
                        else:
                            t = ODTProcessor.clean_text("".join(child.itertext()))
                            if t:
                                parts.append(t)
                        if child.tail:
                            tail = ODTProcessor.clean_text(child.tail)
                            if tail:
                                parts.append(tail)
                    if not parts:
                        t = ODTProcessor.clean_text("".join(elem.itertext()))
                        if t:
                            parts = [t]

                    buf = []
                    for i, p in enumerate(parts):
                        if p.startswith("!["):
                            if buf and not buf[-1].endswith("\n\n"):
                                buf.append("\n\n")
                            buf.append(p)
                            buf.append("\n\n")
                        else:
                            if buf and not buf[-1].endswith((" ", "\n", "\n\n")):
                                buf.append(" ")
                            buf.append(p)
                    txt = "".join(buf)
                    txt = re.sub(r" +", " ", txt)
                    txt = re.sub(r"\n{3,}", "\n\n", txt)
                    if txt and not txt.endswith("\n"):
                        txt += "\n\n"
                    return txt

                # lists
                if tag.endswith("}list"):
                    rows = []
                    for item in elem.findall(".//text:list-item", ns):
                        processed.add(id(item))
                        t = ODTProcessor.clean_text("".join(item.itertext()))
                        if t:
                            rows.append(f"- {t}")
                    return "\n".join(rows) + "\n\n" if rows else ""

                if tag.endswith("}table"):
                    return ODTProcessor.process_table(elem, ns, processed)

                acc = []
                for ch in elem:
                    v = walk(ch)
                    if v:
                        acc.append(v)
                return "".join(acc)

            for child in body:
                out.append(walk(child))

            result = "".join(out)
            result = re.sub(r"\n{3,}", "\n\n", result)
            result = re.sub(r"[ \t]+\n", "\n", result)
            return result


# --- add below your ODTProcessor in processor.py ---

import io
import zipfile
from pathlib import Path
from typing import Tuple, Dict, List

# -------- PDF --------
class PDFProcessor:
    """Extract text & images from PDFs; prefers PyMuPDF (fitz), falls back to PyPDF2 for text only."""
    @staticmethod
    def extract_images_from_pdf(pdf_path: Path, output_dir: Path) -> Tuple[List[Path], Dict[str, str]]:
        try:
            import fitz  # PyMuPDF
        except ImportError:
            logger.warning("PyMuPDF not installed; skipping PDF image extraction. `pip install pymupdf`")
            return [], {}

        output_dir.mkdir(exist_ok=True)
        extracted, mapping = [], {}
        doc = fitz.open(pdf_path)
        for pno in range(len(doc)):
            page = doc[pno]
            for idx, img in enumerate(page.get_images(full=True)):
                xref = img[0]
                pix = fitz.Pixmap(doc, xref)
                ext = ".png" if not pix.alpha and pix.colorspace.n == 3 else ".png"
                name = f"image_{uuid.uuid4().hex[:7]}{ext}"
                out = output_dir / name
                if pix.n >= 5:  # CMYK
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                pix.save(out)
                extracted.append(out)
                mapping[f"p{pno+1}_{xref}"] = name
        doc.close()
        logger.info(f"📷 PDF images extracted: {len(extracted)}")
        return extracted, mapping

    @staticmethod
    def extract_text_from_pdf(pdf_path: Path) -> str:
        # Try PyMuPDF first (better text layout), then PyPDF2.
        try:
            import fitz
            with fitz.open(pdf_path) as doc:
                text = []
                for p in doc:
                    text.append(p.get_text())
                return "\n\n".join(ODTProcessor.clean_text(t) for t in text if t)
        except Exception:
            pass

        try:
            import PyPDF2
            text = []
            with open(pdf_path, "rb") as f:
                r = PyPDF2.PdfReader(f)
                for page in r.pages:
                    text.append(page.extract_text() or "")
            return "\n\n".join(ODTProcessor.clean_text(t) for t in text if t)
        except Exception as e:
            raise RuntimeError(f"PDF text extraction failed: {e}")

    @staticmethod
    def extract_text_with_links(pdf_path: Path, image_mapping: dict, drive_links: dict) -> str:
        """Simple heuristic: put page text, then any images found on that page (if PyMuPDF available)."""
        try:
            import fitz
            def _link(name: str) -> str | None:
                direct = _direct_link_for(drive_links, name)
                return f"![{name}]({direct})" if direct else None

            out = []
            with fitz.open(pdf_path) as doc:
                for pno, page in enumerate(doc, start=1):
                    out.append(f"# Page {pno}\n\n")
                    out.append(ODTProcessor.clean_text(page.get_text() or "") + "\n\n")
                    imgs = page.get_images(full=True)
                    for idx, img in enumerate(imgs):
                        key = f"p{pno}_{img[0]}"
                        fname = image_mapping.get(key)
                        if fname:
                            md = _link(fname)
                            if md:
                                out.append(md + "\n\n")
            return "".join(out)
        except Exception:
            # Fallback: just text
            return PDFProcessor.extract_text_from_pdf(pdf_path)


# -------- DOCX --------
class DOCXProcessor:
    """Extract text & images from .docx; uses python-docx."""
    @staticmethod
    def extract_images_from_docx(docx_path: Path, output_dir: Path) -> Tuple[List[Path], Dict[str, str]]:
        output_dir.mkdir(exist_ok=True)
        extracted, mapping = [], {}
        with zipfile.ZipFile(docx_path) as z:
            candidates = [n for n in z.namelist() if n.startswith("word/media/")]
            for n in candidates:
                ext = Path(n).suffix.lower()
                if ext:
                    data = z.read(n)
                    new_name = f"docx_{uuid.uuid4().hex[:6]}{ext}"
                    out = output_dir / new_name
                    out.write_bytes(data)
                    extracted.append(out)
                    mapping[n] = new_name
        logger.info(f"📷 DOCX images extracted: {len(extracted)}")
        return extracted, mapping

    @staticmethod
    def extract_text_from_docx(docx_path: Path) -> str:
        try:
            from docx import Document
        except ImportError:
            raise RuntimeError("python-docx not installed. `pip install python-docx`")

        doc = Document(docx_path)
        lines: List[str] = []

        # Headings & paragraphs
        for p in doc.paragraphs:
            txt = ODTProcessor.clean_text(p.text)
            if not txt:
                continue
            if p.style and str(p.style.name).lower().startswith("heading"):
                # Try to parse heading level; defaults to 1
                level = 1
                for n in range(1, 7):
                    if f"heading {n}" in str(p.style.name).lower():
                        level = n
                        break
                lines.append("#" * level + " " + txt + "\n\n")
            else:
                lines.append(txt + "\n\n")

        # Tables → markdown
        for t in doc.tables:
            rows = []
            for r in t.rows:
                rows.append([ODTProcessor.clean_text(c.text) or " " for c in r.cells])
            if rows:
                lines.append("| " + " | ".join(rows[0]) + " |\n")
                lines.append("| " + " | ".join(["---"] * len(rows[0])) + " |\n")
                for r in rows[1:]:
                    while len(r) < len(rows[0]):
                        r.append(" ")
                    lines.append("| " + " | ".join(r[:len(rows[0])]) + " |\n")
                lines.append("\n")

        return "".join(lines)

    @staticmethod
    def extract_text_with_links(docx_path: Path, image_mapping: dict, drive_links: dict) -> str:
        """
        Best-effort: get normal text; append an 'Images' section with embedded Drive links
        (inline placement inside paragraphs requires deeper XML parsing).
        """
        text = DOCXProcessor.extract_text_from_docx(docx_path)
        if not image_mapping:
            return text
        parts = [text, "## Images\n\n"]
        for _, mapped in image_mapping.items():
            link = _direct_link_for(drive_links, mapped)
            if link:
                parts.append(f"![{mapped}]({link})\n\n")
        return "".join(parts)


# -------- Dispatcher --------
def process_any(path: Path, output_dir: Path, drive_links: dict | None = None) -> dict:
    """
    Extract text + images from path based on extension.
    Returns: {markdown:str, images:List[Path], image_mapping:dict}
    If drive_links provided {filename: url}, text will embed direct links when possible.
    """
    ext = path.suffix.lower()
    output_dir.mkdir(exist_ok=True, parents=True)
    drive_links = drive_links or {}

    if ext == ".odt":
        imgs, mapping = ODTProcessor.extract_images_from_odt(path, output_dir)
        md = (ODTProcessor.extract_text_with_links(path, mapping, drive_links)
              if drive_links else ODTProcessor.extract_text_from_odt(path))
        return {"markdown": md, "images": imgs, "image_mapping": mapping}

    if ext == ".pdf":
        imgs, mapping = PDFProcessor.extract_images_from_pdf(path, output_dir)
        md = (PDFProcessor.extract_text_with_links(path, mapping, drive_links)
              if drive_links else PDFProcessor.extract_text_from_pdf(path))
        return {"markdown": md, "images": imgs, "image_mapping": mapping}

    if ext == ".docx":
        imgs, mapping = DOCXProcessor.extract_images_from_docx(path, output_dir)
        md = (DOCXProcessor.extract_text_with_links(path, mapping, drive_links)
              if drive_links else DOCXProcessor.extract_text_from_docx(path))
        return {"markdown": md, "images": imgs, "image_mapping": mapping}

    raise ValueError(f"Unsupported extension: {ext}")
