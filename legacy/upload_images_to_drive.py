import csv
import mimetypes
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
import json

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from config import settings

# Google API settings
SCOPES = ["https://www.googleapis.com/auth/drive"]

# Local + Drive settings
LOCAL_DIR = Path("extracted_images")
FOLDER_NAME = "Invoice Images"
OUTPUT_CSV = Path("drive_links.csv")
OUTPUT_MARKDOWN = Path("converted_document.md")
ODT_FILE = "Generate Batch Payments - Invoice.odt"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".svg", ".webp"}


def extract_images_from_odt(odt_path: str, output_dir: Path) -> tuple:
    """Extract all images from ODT file and save to output directory"""
    odt_file = Path(odt_path)

    if not odt_file.exists():
        print(f"❌ ODT file not found: {odt_path}")
        return [], {}

    # Create output directory
    output_dir.mkdir(exist_ok=True)

    extracted_images = []
    image_mapping = {}  # Maps original image path to extracted filename

    try:
        with zipfile.ZipFile(odt_file, "r") as odt_zip:
            file_list = odt_zip.namelist()
            image_files = [
                f
                for f in file_list
                if (f.startswith("Pictures/") or f.startswith("media/"))
                and any(f.lower().endswith(ext) for ext in IMAGE_EXTS)
            ]

            if not image_files:
                print("⚠️ No images found in ODT file")
                return [], {}

            print(f"📷 Found {len(image_files)} images in ODT file")

            for img_path in image_files:
                img_data = odt_zip.read(img_path)
                img_name = Path(img_path).name
                output_path = output_dir / img_name

                with open(output_path, "wb") as f:
                    f.write(img_data)

                extracted_images.append(output_path)
                image_mapping[img_path] = img_name
                print(f"✅ Extracted: {img_name}")

    except zipfile.BadZipFile:
        print(f"❌ Invalid ODT file: {odt_path}")
        return [], {}
    except Exception as e:
        print(f"❌ Error extracting images: {e}")
        return [], {}

    return extracted_images, image_mapping


def process_paragraph_content(
    elem, namespaces, image_mapping, drive_links, processed_elements
):
    """Process paragraph content preserving the order of text and images"""
    content_parts = []

    # Process direct children in order to maintain sequence
    for child in elem:
        if child.tag.endswith("}frame"):
            # Process image frame
            processed_elements.add(id(child))
            img_text = process_image(child, namespaces, image_mapping, drive_links)
            if img_text:
                content_parts.append(img_text.strip())
        elif child.tag.endswith("}span") or child.tag.endswith("}a"):
            # Process text spans and links
            processed_elements.add(id(child))
            text = clean_text("".join(child.itertext()))
            if text and text not in ["\\", "", " "]:
                content_parts.append(text)
        else:
            # Process other child elements
            processed_elements.add(id(child))
            text = clean_text("".join(child.itertext()))
            if text and text not in ["\\", "", " "]:
                content_parts.append(text)

    # Also get direct text content of the paragraph (not in child elements)
    if elem.text:
        direct_text = clean_text(elem.text)
        if direct_text and direct_text not in ["\\", "", " "]:
            content_parts.insert(0, direct_text)

    # Handle tail text (text after child elements)
    for child in elem:
        if child.tail:
            tail_text = clean_text(child.tail)
            if tail_text and tail_text not in ["\\", "", " "]:
                content_parts.append(tail_text)

    # If no structured content found, fall back to itertext
    if not content_parts:
        text = clean_text("".join(elem.itertext()))
        if text and text not in ["\\", "", " "]:
            content_parts = [text]

    if not content_parts:
        return ""

    # Join content and add proper spacing
    result = []
    for i, part in enumerate(content_parts):
        if part.startswith("![") and part.endswith(")"):
            # This is an image - add it as a separate line
            if i > 0 and content_parts[i - 1]:  # Add text before image
                result.append("\n\n")
            result.append(part)
            if i < len(content_parts) - 1:  # More content after image
                result.append("\n\n")
        else:
            # This is text
            result.append(part)
            if i < len(content_parts) - 1 and not content_parts[i + 1].startswith("!["):
                result.append(" ")  # Space between text parts

    final_result = "".join(result)

    # Clean up excessive spacing
    final_result = re.sub(r" +", " ", final_result)
    final_result = re.sub(r"\n{3,}", "\n\n", final_result)

    if final_result and not final_result.endswith("\n"):
        final_result += "\n\n"

    return final_result


def clean_text(text: str) -> str:
    """Clean and normalize text content"""
    if not text:
        return ""

    # Remove excessive whitespace and normalize
    text = re.sub(r"\s+", " ", text.strip())

    # Remove common formatting artifacts
    text = text.replace("\n", " ").replace("\r", " ")

    return text


def extract_text_from_odt(odt_path: str, image_mapping: dict, drive_links: dict) -> str:
    """Extract text content from ODT file and convert to markdown with proper structure"""
    odt_file = Path(odt_path)

    if not odt_file.exists():
        print(f"❌ ODT file not found: {odt_path}")
        return ""

    try:
        with zipfile.ZipFile(odt_file, "r") as odt_zip:
            content_xml = odt_zip.read("content.xml").decode("utf-8")
            root = ET.fromstring(content_xml)

            namespaces = {
                "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
                "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
                "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
                "xlink": "http://www.w3.org/1999/xlink",
                "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
            }

            markdown_content = []
            body = root.find(".//office:body/office:text", namespaces)
            if body is None:
                return ""

            # Process elements in document order
            processed_elements = set()  # Track processed elements to avoid duplicates

            for elem in body:
                if id(elem) in processed_elements:
                    continue

                markdown_text = process_element(
                    elem, namespaces, image_mapping, drive_links, processed_elements
                )
                if markdown_text:
                    markdown_content.append(markdown_text)

            return "".join(markdown_content)

    except Exception as e:
        print(f"❌ Error extracting text: {e}")
        return ""


def process_element(
    elem, namespaces, image_mapping, drive_links, processed_elements, level=0
):
    """Process a single element and return markdown text"""
    if id(elem) in processed_elements:
        return ""

    processed_elements.add(id(elem))
    tag = elem.tag
    result = []

    # Handle headings
    if tag.endswith("}h"):
        outline_level = elem.get(
            "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}outline-level", "1"
        )
        text = clean_text("".join(elem.itertext()))
        if text:
            result.append("#" * int(outline_level) + " " + text + "\n\n")

    # Handle paragraphs
    elif tag.endswith("}p"):
        # Check if this paragraph contains only an image
        frames = elem.findall(".//draw:frame", namespaces)

        if frames:
            # Process images in this paragraph
            for frame in frames:
                img_text = process_image(frame, namespaces, image_mapping, drive_links)
                if img_text:
                    result.append(img_text)

            # Also get any text content that's not part of the image
            text_parts = []
            for text_elem in elem.iter():
                if text_elem.tag.endswith("}p") or text_elem.tag.endswith("}span"):
                    if text_elem.text and not any(
                        child.tag.endswith("}frame") for child in text_elem
                    ):
                        text_parts.append(text_elem.text)

            remaining_text = clean_text("".join(text_parts))
            if remaining_text and remaining_text not in ["\\", "", " "]:
                result.append(remaining_text + "\n\n")
        else:
            # Regular paragraph without images
            text = clean_text("".join(elem.itertext()))
            if text and text not in ["\\", "", " "]:
                result.append(text + "\n\n")

    # Handle lists
    elif tag.endswith("}list"):
        for item in elem.findall(".//text:list-item", namespaces):
            processed_elements.add(id(item))
            text = clean_text("".join(item.itertext()))
            if text:
                result.append("- " + text + "\n")
        result.append("\n")

    # Handle tables
    elif tag.endswith("}table"):
        table_md = process_table(elem, namespaces, processed_elements)
        if table_md:
            result.append(table_md)

    # Handle other elements recursively
    else:
        for child in elem:
            child_result = process_element(
                child,
                namespaces,
                image_mapping,
                drive_links,
                processed_elements,
                level + 1,
            )
            if child_result:
                result.append(child_result)

    return "".join(result)


def process_image(frame_elem, namespaces, image_mapping, drive_links):
    """Process an image frame and return markdown image syntax"""
    img_elem = frame_elem.find(".//draw:image", namespaces)
    if img_elem is not None:
        href = img_elem.get("{http://www.w3.org/1999/xlink}href", "")
        if href:
            # Find the corresponding filename in our mapping
            filename = None
            for original_path, mapped_filename in image_mapping.items():
                if original_path.endswith(href) or href.endswith(
                    Path(original_path).name
                ):
                    filename = mapped_filename
                    break

            if filename and filename in drive_links:
                drive_url = drive_links[filename]
                if "drive.google.com" in drive_url:
                    file_id = drive_url.split("/d/")[1].split("/")[0]
                    direct_link = (
                        f"https://drive.google.com/uc?export=view&id={file_id}"
                    )
                else:
                    direct_link = drive_url

                return f"![{filename}]({direct_link})\n\n"
            else:
                # Fallback to original href if no drive link available
                return f"![Image]({href})\n\n"

    return ""


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
            cell_text = clean_text("".join(cell.itertext()))
            row_data.append(cell_text if cell_text else " ")
        if row_data:
            table_data.append(row_data)

    if not table_data:
        return ""

    # Convert to markdown table
    result = []

    # Header row
    if table_data:
        result.append("| " + " | ".join(table_data[0]) + " |\n")
        result.append("| " + " | ".join(["---"] * len(table_data[0])) + " |\n")

        # Data rows
        for row in table_data[1:]:
            # Pad row to match header length
            while len(row) < len(table_data[0]):
                row.append(" ")
            result.append("| " + " | ".join(row[: len(table_data[0])]) + " |\n")

        result.append("\n")

    return "".join(result)


def create_markdown_with_drive_links(
    odt_path: str, image_mapping: dict, drive_links: dict, output_path: Path
):
    """Create markdown file with Google Drive image links"""
    print("📝 Creating markdown file...")

    markdown_content = extract_text_from_odt(odt_path, image_mapping, drive_links)

    if not markdown_content:
        print("⚠️ No text content found, creating basic markdown with images")
        markdown_content = "# Converted Document\n\n"

        # Add images if we have them
        for filename, drive_link in drive_links.items():
            if "drive.google.com" in drive_link:
                file_id = drive_link.split("/d/")[1].split("/")[0]
                direct_link = f"https://drive.google.com/uc?export=view&id={file_id}"
            else:
                direct_link = drive_link
            markdown_content += f"![{filename}]({direct_link})\n\n"

    # Clean up any remaining formatting issues
    markdown_content = re.sub(
        r"\n{3,}", "\n\n", markdown_content
    )  # Remove excessive newlines
    markdown_content = re.sub(
        r"[ \t]+\n", "\n", markdown_content
    )  # Remove trailing whitespace

    # Save markdown file
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(markdown_content)

    print(f"✅ Markdown file created: {output_path.resolve()}")


def get_service():
    creds = None
    token_json = settings.google_token_json
    if isinstance(token_json, str):
        token_info = json.loads(token_json)
    else:
        token_info = token_json
        
    creds = Credentials.from_authorized_user_info(json.loads(token_info), [settings.google_cloud_scopes])

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())

            settings.google_token_json = creds.to_json()
        else:
            client_config = {
                "installed": {
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uris": settings.google_redirect_uris,
                    "auth_uri": settings.google_auth_uri,
                    "token_uri": settings.google_token_uri,
                }
            }
            flow = InstalledAppFlow.from_client_config(client_config, [settings.google_cloud_scopes])
            creds = flow.run_local_server(port=0)
            # If you don’t want a token file, you can keep creds only in env/secret store:
            # os.environ["GOOGLE_TOKEN_JSON"] = creds.to_json()

    return build("drive", "v3", credentials=creds)


def ensure_folder(service, folder_name: str) -> str:
    safe_name = folder_name.replace("'", "\\'")
    query = f"mimeType='application/vnd.google-apps.folder' and name='{safe_name}' and trashed=false"
    resp = (
        service.files()
        .list(q=query, spaces="drive", fields="files(id, name)", pageSize=10)
        .execute()
    )
    files = resp.get("files", [])

    if files:
        return files[0]["id"]

    file_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    folder = service.files().create(body=file_metadata, fields="id").execute()
    return folder["id"]


def set_public(service, file_id: str):
    permission = {"type": "anyone", "role": "reader"}
    service.permissions().create(fileId=file_id, body=permission).execute()


def upload_one(service, local_path: Path, folder_id: str) -> dict:
    mime_type, _ = mimetypes.guess_type(str(local_path))
    if not mime_type:
        mime_type = "application/octet-stream"

    metadata = {"name": local_path.name, "parents": [folder_id]}
    media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)
    file = (
        service.files()
        .create(body=metadata, media_body=media, fields="id,name,webViewLink")
        .execute()
    )
    set_public(service, file["id"])

    return {"filename": file["name"], "webViewLink": file.get("webViewLink", "")}


def main():
    # Step 1: Extract images from ODT file
    print("🔍 Extracting images from ODT file...")
    extracted_images, image_mapping = extract_images_from_odt(ODT_FILE, LOCAL_DIR)

    if not extracted_images:
        print("❌ No images extracted, but continuing with text conversion...")
        create_markdown_with_drive_links(ODT_FILE, {}, {}, OUTPUT_MARKDOWN)
        return []

    # Step 2: Upload to Google Drive
    print("\n🌐 Uploading to Google Drive...")
    service = get_service()
    folder_id = ensure_folder(service, FOLDER_NAME)
    print(f"✅ Using Drive folder: {FOLDER_NAME} (id: {folder_id})")

    rows = []
    drive_links = []
    drive_links_dict = {}

    for img_path in extracted_images:
        info = upload_one(service, img_path, folder_id)
        print(f"⬆️ Uploaded {info['filename']} -> {info['webViewLink']}")
        rows.append(info)
        drive_links.append(info["webViewLink"])
        drive_links_dict[info["filename"]] = info["webViewLink"]

    # Step 3: Save to CSV
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["filename", "webViewLink"])
        writer.writeheader()
        writer.writerows(rows)

    # Step 4: Create markdown file with Drive links
    print(f"\n📝 Creating markdown file with Drive links...")
    create_markdown_with_drive_links(
        ODT_FILE, image_mapping, drive_links_dict, OUTPUT_MARKDOWN
    )

    print(f"\n📄 Links saved to {OUTPUT_CSV.resolve()}")
    print(f"📄 Markdown file saved to {OUTPUT_MARKDOWN.resolve()}")
    print(f"🎉 Successfully processed {len(drive_links)} images")

    return drive_links


if __name__ == "__main__":
    links = main()
    print(f"\n📋 Drive Links List:")
    for i, link in enumerate(links, 1):
        print(f"{i}. {link}")
