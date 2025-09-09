import re
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
                    img_name = Path(img_path).name
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
