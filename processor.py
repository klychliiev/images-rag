import re
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from loguru import logger
import io
from typing import Tuple, Dict, List
import fitz

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".svg", ".webp"}


def _direct_link_for(drive_links: dict, filename: str) -> str | None:
    """Convert Google Drive sharing link to direct view link"""
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
                    # Create unique filename to avoid collisions
                    base_name = Path(img_name).stem  # filename without extension
                    ext = Path(img_name).suffix      # extension with dot
                    img_name = f"image_{str(uuid.uuid4())[:5]}_{base_name}{ext}"
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
        Extract text from ODT and replace image references with Drive links when available.
        """
        if not odt_path.exists():
            raise FileNotFoundError(f"ODT file not found: {odt_path}")

        logger.info(f"🔗 Processing ODT with {len(image_mapping)} images and {len(drive_links)} drive links")
        
        # Debug logging
        for orig_path, mapped_name in image_mapping.items():
            drive_link = drive_links.get(mapped_name)
            logger.info(f"Image mapping: {orig_path} -> {mapped_name} -> {drive_link}")

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

            def find_mapped_filename_for_href(href: str) -> str | None:
                """Find the mapped filename for an image href"""
                logger.debug(f"Looking for href: {href}")
                
                # Try exact match first
                if href in image_mapping:
                    return image_mapping[href]
                
                # Try matching by filename
                href_filename = Path(href).name
                for original_path, mapped_filename in image_mapping.items():
                    original_filename = Path(original_path).name
                    if original_filename == href_filename:
                        logger.debug(f"Found match: {original_filename} -> {mapped_filename}")
                        return mapped_filename
                
                # Try partial matching
                for original_path, mapped_filename in image_mapping.items():
                    if original_path.endswith(href) or href.endswith(Path(original_path).name):
                        logger.debug(f"Found partial match: {original_path} -> {mapped_filename}")
                        return mapped_filename
                
                logger.warning(f"No mapping found for href: {href}")
                return None

            def img_md_for_href(href: str) -> str | None:
                """Convert image href to markdown with Drive link"""
                mapped_filename = find_mapped_filename_for_href(href)
                if not mapped_filename:
                    return None
                
                drive_link = _direct_link_for(drive_links, mapped_filename)
                if not drive_link:
                    logger.warning(f"No drive link found for: {mapped_filename}")
                    return None
                
                logger.info(f"Creating markdown link: ![{mapped_filename}]({drive_link})")
                # FIX: render as image, not just a link
                return f"![{mapped_filename}]({drive_link})"

            def walk(elem):
                processed = set()
                
                def process_elem(elem):
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
                        
                        # Process text and child elements
                        if elem.text:
                            t = ODTProcessor.clean_text(elem.text)
                            if t:
                                parts.append(t)
                        
                        for child in elem:
                            if child.tag.endswith("}frame"):
                                # Look for images in frames
                                img = child.find(".//draw:image", ns)
                                if img is not None:
                                    href = img.get("{http://www.w3.org/1999/xlink}href", "")
                                    logger.debug(f"Found image with href: {href}")
                                    if href:
                                        md = img_md_for_href(href)
                                        if md:
                                            parts.append(md)
                                        else:
                                            # Fallback: just mention the image
                                            parts.append(f"[Image: {Path(href).name}]")
                            else:
                                # Process other child elements
                                child_text = process_elem(child)
                                if child_text:
                                    parts.append(child_text)
                            
                            if child.tail:
                                tail = ODTProcessor.clean_text(child.tail)
                                if tail:
                                    parts.append(tail)
                        
                        # If no parts found, get all text
                        if not parts:
                            t = ODTProcessor.clean_text("".join(elem.itertext()))
                            if t:
                                parts = [t]

                        # Combine parts with proper spacing
                        if parts:
                            result = []
                            for i, part in enumerate(parts):
                                if part.startswith("![") or part.startswith("[Image:"):
                                    # Image - add with newlines
                                    if result and not result[-1].endswith("\n\n"):
                                        result.append("\n\n")
                                    result.append(part)
                                    result.append("\n\n")
                                else:
                                    # Text - add with space if needed
                                    if result and not result[-1].endswith((" ", "\n")):
                                        result.append(" ")
                                    result.append(part)
                            
                            txt = "".join(result)
                            txt = re.sub(r" +", " ", txt)
                            txt = re.sub(r"\n{3,}", "\n\n", txt)
                            if txt and not txt.endswith("\n"):
                                txt += "\n\n"
                            return txt
                        
                        return ""

                    # Handle lists
                    if tag.endswith("}list"):
                        rows = []
                        for item in elem.findall(".//text:list-item", ns):
                            processed.add(id(item))
                            t = ODTProcessor.clean_text("".join(item.itertext()))
                            if t:
                                rows.append(f"- {t}")
                        return "\n".join(rows) + "\n\n" if rows else ""

                    # Handle tables
                    if tag.endswith("}table"):
                        return ODTProcessor.process_table(elem, ns, processed)

                    # Handle other elements recursively
                    acc = []
                    for ch in elem:
                        v = process_elem(ch)
                        if v:
                            acc.append(v)
                    return "".join(acc)
                
                return process_elem(elem)

            # Process all body elements
            out = []
            for child in body:
                result = walk(child)
                if result:
                    out.append(result)

            final_result = "".join(out)
            final_result = re.sub(r"\n{3,}", "\n\n", final_result)
            final_result = re.sub(r"[ \t]+\n", "\n", final_result)
            
            logger.info(f"📄 Final result length: {len(final_result)} characters")
            return final_result
class ImprovedPDFProcessor:
    """PDF processing that preserves document structure with proper image placement."""
    
    @staticmethod
    def extract_images_from_pdf(pdf_path: Path, output_dir: Path) -> Tuple[List[Path], Dict[int, str]]:
        """Extract images and return mapping of xref -> filename"""
        output_dir.mkdir(exist_ok=True)
        extracted = []
        mapping = {}  # xref -> filename
        
        doc = fitz.open(pdf_path)
        processed_xrefs = set()
        
        logger.info(f"Processing PDF with {len(doc)} pages")
        
        for pno in range(len(doc)):
            page_images = doc[pno].get_images(full=True)
            for img in page_images:
                xref = img[0]
                
                if xref in processed_xrefs:
                    continue
                
                processed_xrefs.add(xref)
                
                try:
                    pix = fitz.Pixmap(doc, xref)
                    name = f"image_{uuid.uuid4().hex[:7]}.png"
                    out = output_dir / name
                    
                    if pix.n >= 5:  # CMYK
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    
                    pix.save(out)
                    extracted.append(out)
                    mapping[xref] = name
                    
                    logger.success(f"Saved: {name}")
                    pix = None
                    
                except Exception as e:
                    logger.warning(f"Error processing image: {e}")
                    continue
        
        doc.close()
        logger.info(f"Extracted {len(extracted)} unique images")
        return extracted, mapping

    @staticmethod
    def pdf_to_markdown_with_placeholders(pdf_path: Path, image_mapping: Dict[int, str]) -> str:
        """Convert PDF to markdown with image placeholders at correct positions"""
        doc = fitz.open(pdf_path)
        result = []
        
        for pno in range(len(doc)):
            page = doc[pno]
            
            # Get text blocks with position info
            blocks = page.get_text("dict")
            page_images = page.get_images(full=True)
            
            # Create list of content items with their positions
            content_items = []
            
            # Add text blocks
            for block in blocks["blocks"]:
                if "lines" in block:  # Text block
                    text_content = ""
                    for line in block["lines"]:
                        for span in line["spans"]:
                            text_content += span["text"]
                        text_content += " "
                    
                    if text_content.strip():
                        content_items.append({
                            "type": "text",
                            "content": text_content.strip(),
                            "y0": block["bbox"][1],  # Top Y coordinate
                            "y1": block["bbox"][3]   # Bottom Y coordinate
                        })
            
            # Add image placeholders
            for img in page_images:
                xref = img[0]
                if xref in image_mapping:
                    # Get image position
                    img_instances = page.get_image_rects(xref)
                    for rect in img_instances:
                        placeholder = f"{{{{IMG_PLACEHOLDER_{xref}}}}}"
                        content_items.append({
                            "type": "image",
                            "content": placeholder,
                            "y0": rect.y0,
                            "y1": rect.y1,
                            "xref": xref
                        })
            
            # Sort content by vertical position (top to bottom)
            content_items.sort(key=lambda x: x["y0"])
            
            # Build page content
            for item in content_items:
                if item["type"] == "text":
                    clean_text = ImprovedPDFProcessor.clean_text(item["content"])
                    if clean_text:
                        result.append(clean_text + "\n\n")
                elif item["type"] == "image":
                    result.append(item["content"] + "\n\n")
        
        doc.close()
        
        # Join and clean up
        text = "".join(result)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        
        return text.strip()
    
    @staticmethod
    def replace_placeholders_with_links(markdown_text: str, image_mapping: Dict[int, str], 
                                      drive_links: Dict[str, str]) -> str:
        """Replace image placeholders with actual Drive links"""
        
        def replace_placeholder(match):
            xref = int(match.group(1))
            if xref in image_mapping:
                filename = image_mapping[xref]
                drive_link = ImprovedPDFProcessor._direct_link_for(drive_links, filename)
                if drive_link:
                    return f"![{filename}]({drive_link})"
                else:
                    return f"[Image: {filename}]"
            return match.group(0)  # Keep original if no mapping found
        
        # Replace all placeholders
        result = re.sub(r'\{\{IMG_PLACEHOLDER_(\d+)\}\}', replace_placeholder, markdown_text)
        
        return result
    
    @staticmethod
    def _direct_link_for(drive_links: dict, filename: str) -> str | None:
        """Convert Google Drive sharing link to direct view link"""
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
    
    @staticmethod
    def clean_text(text: str) -> str:
        """Clean and normalize text content"""
        if not text:
            return ""
        
        text = re.sub(r"\s+", " ", text.strip())
        text = text.replace("\n", " ").replace("\r", " ")
        
        return text
    
    @staticmethod
    def extract_text_with_links(pdf_path: Path, image_mapping: dict, drive_links: dict) -> str:
        """Main method: extract text with properly positioned images"""
        
        # Step 1: Convert PDF to markdown with placeholders
        markdown_with_placeholders = ImprovedPDFProcessor.pdf_to_markdown_with_placeholders(
            pdf_path, image_mapping
        )
        
        logger.info(f"Generated markdown with placeholders: {len(markdown_with_placeholders)} chars")
        
        # Step 2: Replace placeholders with actual Drive links
        final_markdown = ImprovedPDFProcessor.replace_placeholders_with_links(
            markdown_with_placeholders, image_mapping, drive_links
        )
        
        logger.info(f"Final markdown with Drive links: {len(final_markdown)} chars")
        
        return final_markdown
    

class DOCXProcessor:
    """Extract text & images from .docx with proper placeholder system."""
    
    @staticmethod
    def extract_images_from_docx(docx_path: Path, output_dir: Path) -> Tuple[List[Path], Dict[str, str]]:
        """Extract images from DOCX file"""
        output_dir.mkdir(exist_ok=True)
        extracted, mapping = [], {}
        
        with zipfile.ZipFile(docx_path) as z:
            candidates = [n for n in z.namelist() if n.startswith("word/media/")]
            for img_path in candidates:
                ext = Path(img_path).suffix.lower()
                if ext in IMAGE_EXTS:
                    data = z.read(img_path)
                    new_name = f"docx_{uuid.uuid4().hex[:6]}{ext}"
                    out_path = output_dir / new_name
                    out_path.write_bytes(data)
                    extracted.append(out_path)
                    mapping[img_path] = new_name
        
        logger.info(f"📷 DOCX images extracted: {len(extracted)}")
        return extracted, mapping

    @staticmethod
    def get_relationship_mapping(docx_path: Path) -> Dict[str, str]:
        """Get mapping of relationship IDs to image targets"""
        rel_mapping = {}
        
        try:
            with zipfile.ZipFile(docx_path) as z:
                rels_path = 'word/_rels/document.xml.rels'
                if rels_path in z.namelist():
                    content = z.read(rels_path)
                    root = ET.fromstring(content)
                    ns = {'r': 'http://schemas.openxmlformats.org/package/2006/relationships'}
                    
                    for rel in root.findall('r:Relationship', ns):
                        rel_id = rel.get('Id')
                        target = rel.get('Target')
                        if rel_id and target:
                            if target.startswith('media/'):
                                target = f"word/{target}"
                            elif not target.startswith('word/'):
                                target = f"word/{target}"
                            rel_mapping[rel_id] = target
        except Exception as e:
            logger.warning(f"Could not parse relationships: {e}")
        
        return rel_mapping

    @staticmethod
    def docx_to_markdown_with_placeholders(docx_path: Path, image_mapping: dict) -> str:
        """Convert DOCX to markdown with image placeholders"""
        try:
            from docx import Document
        except ImportError:
            raise RuntimeError("python-docx not installed. `pip install python-docx`")

        doc = Document(docx_path)
        content_parts = []
        rel_mapping = DOCXProcessor.get_relationship_mapping(docx_path)
        
        def clean_text(text):
            """Clean text without corrupting words"""
            if not text:
                return ""
            return ' '.join(text.split()).strip()
        
        image_counter = 0
        image_placeholders = {}  # Track which image goes to which placeholder
        
        for paragraph in doc.paragraphs:
            paragraph_text = []
            has_content = False
            
            for run in paragraph.runs:
                # Add text content
                if run.text and run.text.strip():
                    clean_run_text = clean_text(run.text)
                    if clean_run_text:
                        paragraph_text.append(clean_run_text)
                        has_content = True
                
                # Check for images in run
                if hasattr(run, '_r') and run._r is not None:
                    # Look for blip elements (images)
                    blip = run._r.find('.//{*}blip')
                    if blip is not None:
                        embed_id = blip.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed')
                        if embed_id and embed_id in rel_mapping:
                            image_path = rel_mapping[embed_id]
                            if image_path in image_mapping:
                                # Create placeholder
                                placeholder = f"{{{{DOCX_IMG_{image_counter}}}}}"
                                paragraph_text.append(placeholder)
                                image_placeholders[image_counter] = image_mapping[image_path]
                                image_counter += 1
                                has_content = True
            
            if not has_content:
                continue
            
            full_text = " ".join(paragraph_text)
            
            # Handle headings
            if paragraph.style and str(paragraph.style.name).lower().startswith("heading"):
                level = 1
                for n in range(1, 7):
                    if f"heading {n}" in str(paragraph.style.name).lower():
                        level = n
                        break
                content_parts.append("#" * level + " " + full_text + "\n\n")
            else:
                content_parts.append(full_text + "\n\n")
        
        # Process tables
        for table in doc.tables:
            rows = []
            for row in table.rows:
                row_data = [clean_text(cell.text) or " " for cell in row.cells]
                if any(cell.strip() for cell in row_data):
                    rows.append(row_data)
            
            if rows:
                content_parts.append("| " + " | ".join(rows[0]) + " |\n")
                content_parts.append("| " + " | ".join(["---"] * len(rows[0])) + " |\n")
                for row in rows[1:]:
                    while len(row) < len(rows[0]):
                        row.append(" ")
                    content_parts.append("| " + " | ".join(row) + " |\n")
                content_parts.append("\n")
        
        markdown_text = "".join(content_parts)
        markdown_text = re.sub(r'\n{3,}', '\n\n', markdown_text)
        markdown_text = re.sub(r'[ \t]+\n', '\n', markdown_text)
        
        return markdown_text.strip(), image_placeholders

    @staticmethod
    def replace_placeholders_with_links(markdown_text: str, image_placeholders: dict, drive_links: dict) -> str:
        """Replace image placeholders with actual Drive links"""
        
        def replace_placeholder(match):
            placeholder_id = int(match.group(1))
            if placeholder_id in image_placeholders:
                filename = image_placeholders[placeholder_id]
                drive_link = _direct_link_for(drive_links, filename)
                if drive_link:
                    return f"![{filename}]({drive_link})"
            return "[Image]"
        
        result = re.sub(r'\{\{DOCX_IMG_(\d+)\}\}', replace_placeholder, markdown_text)
        return result

    @staticmethod
    def extract_text_with_links(docx_path: Path, image_mapping: dict, drive_links: dict) -> str:
        """Main method: extract text with properly positioned images"""
        
        # Step 1: Convert DOCX to markdown with placeholders
        markdown_with_placeholders, image_placeholders = DOCXProcessor.docx_to_markdown_with_placeholders(
            docx_path, image_mapping
        )
        
        logger.info(f"Generated markdown with {len(image_placeholders)} image placeholders")
        
        # Step 2: Replace placeholders with actual Drive links
        final_markdown = DOCXProcessor.replace_placeholders_with_links(
            markdown_with_placeholders, image_placeholders, drive_links
        )
        
        logger.info(f"Final markdown with Drive links: {len(final_markdown)} chars")
        
        return final_markdown

    @staticmethod
    def extract_text_from_docx(docx_path: Path) -> str:
        """Basic text extraction without image links"""
        markdown_text, _ = DOCXProcessor.docx_to_markdown_with_placeholders(docx_path, {})
        return markdown_text


# -------- Dispatcher --------
def process_any(path: Path, output_dir: Path, drive_links: dict | None = None, 
                existing_image_mapping: dict | None = None) -> dict:
    """
    Extract text + images from path based on extension.
    Returns: {markdown:str, images:List[Path], image_mapping:dict}
    If drive_links provided {filename: url}, text will embed direct links when possible.
    If existing_image_mapping provided, reuse it instead of creating new one.
    """
    ext = path.suffix.lower()
    output_dir.mkdir(exist_ok=True, parents=True)
    drive_links = drive_links or {}

    if ext == ".odt":
        if existing_image_mapping:
            # Reuse existing mapping - don't extract images again
            imgs = [output_dir / fname for fname in existing_image_mapping.values() 
                   if (output_dir / fname).exists()]
            mapping = existing_image_mapping
        else:
            # First time - extract images
            imgs, mapping = ODTProcessor.extract_images_from_odt(path, output_dir)
        
        md = (ODTProcessor.extract_text_with_links(path, mapping, drive_links)
              if drive_links else ODTProcessor.extract_text_from_odt(path))
        logger.success(f"MARKDOWN: {md}")
        return {"markdown": md, "images": imgs, "image_mapping": mapping}

    if ext == ".pdf":
        if existing_image_mapping:
            imgs = [output_dir / fname for fname in existing_image_mapping.values() 
                   if (output_dir / fname).exists()]
            mapping = existing_image_mapping
        else:
            imgs, mapping = ImprovedPDFProcessor.extract_images_from_pdf(path, output_dir)
        
        md = (ImprovedPDFProcessor.extract_text_with_links(path, mapping, drive_links)
              if drive_links else ImprovedPDFProcessor.pdf_to_markdown_with_placeholders(path, mapping))
        
        logger.success(f"MARKDOWN: {md}")
        return {"markdown": md, "images": imgs, "image_mapping": mapping}
    

    if ext == ".docx":
        if existing_image_mapping:
            imgs = [output_dir / fname for fname in existing_image_mapping.values() 
                   if (output_dir / fname).exists()]
            mapping = existing_image_mapping
        else:
            imgs, mapping = DOCXProcessor.extract_images_from_docx(path, output_dir)
        
        md = (DOCXProcessor.extract_text_with_links(path, mapping, drive_links)
              if drive_links else DOCXProcessor.extract_text_from_docx(path))
        logger.success(f"MARKDOWN: {md}")
        return {"markdown": md, "images": imgs, "image_mapping": mapping}

    if ext == ".md":
        return MarkdownProcessor.process_md(path, path.parent / "images", drive_links or {})

    raise ValueError(f"Unsupported extension: {ext}")


class MarkdownProcessor:
    @staticmethod
    def process_md(
        md_path: Path,
        images_dir: Path | None = None,
        drive_links: dict | None = None,
    ) -> dict:
        content = md_path.read_text(encoding="utf-8")
        img_pattern = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')

        images: list[Path] = []
        image_mapping: dict[str, str] = {}
        seen: set[Path] = set()

        if images_dir and images_dir.exists():
            for match in img_pattern.finditer(content):
                ref = match.group(2)
                img_path = (images_dir / Path(ref).name).resolve()
                if img_path.exists() and img_path not in seen:
                    seen.add(img_path)
                    images.append(img_path)
                    image_mapping[ref] = img_path.name

        markdown = content
        if drive_links:
            def _replace(m: re.Match) -> str:
                alt, ref = m.group(1), m.group(2)
                url = drive_links.get(Path(ref).name)
                return f"![{alt}]({url})" if url else m.group(0)
            markdown = img_pattern.sub(_replace, content)

        return {"markdown": markdown, "images": images, "image_mapping": image_mapping}
