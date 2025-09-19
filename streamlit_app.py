# streamlit_app.py
from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import streamlit as st

from config import settings
from processor import process_any
from drive_uploader import upload_images_to_folder
from pinecone_service import PineconeDocumentIndexer

from auth import auth_sidebar


st.set_page_config(page_title="Docs → Markdown & Pinecone", page_icon="📄", layout="centered")

st.markdown(
    """
    <style>
      .header-title {font-size: 2.0rem; font-weight: 800; margin-bottom: .5rem;}
      .subtle {color: #6c757d;}
      .ok {color: #28a745; font-weight: 600;}
      .warn {color: #fd7e14; font-weight: 600;}
      .err {color: #dc3545; font-weight: 600;}

      /* Center container with max width and extra top padding */
      .block-container {
        max-width: 900px;
        margin: 0 auto;
        padding-top: 6rem;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="header-title">📄 Document Processor</div>', unsafe_allow_html=True)

is_authed = auth_sidebar()  # show sidebar auth and capture auth status

st.sidebar.header("⚙️ Options")
disabled = not is_authed

default_folder = "support-agent-images"
drive_folder = st.sidebar.text_input("Drive folder name", value=default_folder, disabled=disabled)

pinecone_index_name = st.sidebar.text_input(
    "Pinecone index name",
    value=settings.pinecone_index_default,
    help="Name of the Pinecone index where extracted text will be stored.",
    disabled=disabled,
)

files = st.file_uploader(
    "Upload files",
    type=["odt", "pdf", "docx"],
    accept_multiple_files=True,
    help="Supported formats: .odt, .pdf, .docx",
    disabled=disabled,
)

st.info(
    "📂 **Supported file formats:** ODT, PDF, DOCX. "
    "Images embedded in documents will also be extracted automatically."
)


def make_indexer() -> PineconeDocumentIndexer:
    """Create & prepare Pinecone indexer; override chunk settings from sidebar."""
    idx = PineconeDocumentIndexer(
        pinecone_api_key=settings.pinecone_api_key,
        openai_api_key=settings.openai_api_key,
        index_name=pinecone_index_name,
    )
    idx.create_index_if_not_exists()
    return idx


def process_one(upload, indexer: PineconeDocumentIndexer):
    """Persist upload, extract text/images (process_any), optionally upload images, then index text."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        in_path = tmpdir / upload.name
        in_path.write_bytes(upload.getvalue())

        img_dir = tmpdir / "images"

        # First pass: extract text + local images (no Drive links yet)
        extracted = process_any(in_path, img_dir, drive_links=None)
        images: List[Path] = extracted["images"]
        original_image_mapping = extracted["image_mapping"]
        markdown = extracted["markdown"]

        drive_links: Dict[str, str] = {}
        if images:
            st.info(f"📤 Uploading {len(images)} images to Drive folder: {drive_folder}")
            with st.spinner("Uploading to Google Drive..."):
                # {filename_on_disk: drive_url}
                drive_links = upload_images_to_folder(images, drive_folder)

            st.success(f"✅ Uploaded {len(drive_links)} images to Drive")

            # Rebuild markdown for the SAME file type, embedding Drive links
            rebuilt = process_any(
                in_path,
                img_dir,
                drive_links=drive_links,
                existing_image_mapping=original_image_mapping,
            )
            markdown = rebuilt["markdown"]
        else:
            st.info("ℹ️ No images detected in the document.")

        st.info("💾 Indexing document in Pinecone...")
        metadata = {
            "filename": upload.name,
            "uploaded_at": datetime.utcnow().isoformat(),
            "content_ext": Path(upload.name).suffix.lower(),
            "images_uploaded": bool(drive_links),
            "images_count": len(images),
            "drive_links_count": len(drive_links),
        }

        try:
            stats = indexer.process_and_index(markdown, metadata)
            st.success("✅ Document indexed in Pinecone")
        except Exception as e:
            st.error(f"❌ Failed to index in Pinecone: {e}")
            stats = {"error": str(e)}

        return {
            "markdown": markdown,
            "images": [p.name for p in images],
            "drive_links": drive_links,  # {filename: direct_link}
            "stats": stats,
        }


go = st.button("🚀 Process", type="primary", use_container_width=True, disabled=disabled)

if not is_authed:
    st.warning("You must sign in with your **@artisio.co** account to process files.")
    st.stop()

if go:
    if not files:
        st.error("Please upload at least one file (.odt, .pdf, .docx).")
    else:
        idx = make_indexer()
        progress = st.progress(0.0)

        for i, f in enumerate(files, start=1):
            with st.expander(f"📄 {f.name}", expanded=True):
                try:
                    res = process_one(f, idx)

                    left, right = st.columns([2, 1])

                    st.subheader(f"Images detected: {len(res['images'])}")
                    if res["drive_links"]:
                        st.write("### Image links")
                        st.json(res["drive_links"])

                except Exception as e:
                    st.error(f"❌ {e}")

            progress.progress(i / len(files))
