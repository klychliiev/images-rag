import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import streamlit as st

from config import settings
from processor import process_any, MarkdownProcessor
from drive_uploader import (
    upload_images_to_folder, upload_guide_images,
    list_guides, delete_guide_folder,
    list_loose_files, delete_file,
    get_thumbnail_bytes,
)
from pinecone_service import PineconeDocumentIndexer

from auth import auth_sidebar, _get_client, _read_cookie, _write_cookies

st.set_page_config(
    page_title="Document Processor",
    page_icon="📄",
    layout="centered"
)

st.markdown("""
<style>
/* Light base */
html, body, .stApp {
    background-color: #ffffff !important;
}

[data-testid="stAppViewContainer"] {
    background-color: #ffffff !important;
}

[data-testid="stSidebar"] {
    background-color: #f0f2f6 !important;
}

/* Text contrast on light bg */
h1, h2, h3, h4, h5, h6, p, span, label {
    color: #1a1a2e !important;
}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<style>
/* Primary button stays on-brand */
button[kind="primary"] {
    background-color: #10cafb !important;
    color: #1a1a2e !important;
    border: none !important;
}
</style>
""", unsafe_allow_html=True)

def _large_thumb_url(url: str) -> str:
    return re.sub(r'=s\d+', '=s1600', url)


@st.dialog("Image preview", width="large")
def _image_preview_dialog() -> None:
    info = st.session_state.get("_preview_file")
    if not info:
        return
    st.caption(f"**{info['name']}**")
    cache_key = f"_preview_bytes_{info['id']}"
    if cache_key not in st.session_state:
        with st.spinner("Loading..."):
            st.session_state[cache_key] = get_thumbnail_bytes(_large_thumb_url(info["thumbnail"]))
    img_bytes = st.session_state[cache_key]
    if img_bytes:
        st.image(img_bytes, use_container_width=True)
    else:
        st.warning("Could not load preview.")


def handle_password_recovery():
    """Detect Supabase recovery redirect via URL params."""
    params = st.query_params
    if params.get("type") == "recovery":
        st.session_state.recovery = True


def password_reset_view():
    """UI for setting a new password after email link."""
    st.title("🔐 Reset your password")

    new_password = st.text_input("New password", type="password")
    confirm = st.text_input("Confirm password", type="password")

    if st.button("Update password", use_container_width=True):
        if not new_password or not confirm:
            st.warning("Please fill in both fields.")
            return

        if new_password != confirm:
            st.error("Passwords do not match.")
            return

        try:
            _get_client().auth.update_user({"password": new_password})

            st.success("✅ Password updated! You can now sign in.")
            st.session_state.recovery = False

            # Clear query params so it doesn't trigger again
            st.query_params.clear()

        except Exception as e:
            st.error(str(e))


handle_password_recovery()

if st.session_state.get("recovery"):
    password_reset_view()
    st.stop()



st.markdown(
    """
    <style>
      .header-title {font-size: 2.0rem; font-weight: 800; margin-bottom: .5rem;}
      .subtle {color: #6c757d;}
      .ok {color: #28a745; font-weight: 600;}
      .warn {color: #fd7e14; font-weight: 600;}
      .err {color: #dc3545; font-weight: 600;}

      .block-container {
        max-width: 900px;
        margin: 0 auto;
        padding-top: 6rem;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="header-title">📄 Document Processor</div>', unsafe_allow_html=True
)


is_authed = auth_sidebar()

st.sidebar.header("⚙️ Options")
disabled = not is_authed


default_folder = "support-agent-images"
drive_folder = st.sidebar.text_input(
    "Drive folder name", value=default_folder, disabled=disabled
)

pinecone_index_name = st.sidebar.text_input(
    "Pinecone index name",
    value=settings.pinecone_index_default,
    help="Name of the Pinecone index where extracted text will be stored.",
    disabled=disabled,
)

def make_indexer() -> PineconeDocumentIndexer:
    idx = PineconeDocumentIndexer(
        pinecone_api_key=settings.pinecone_api_key,
        openai_api_key=settings.openai_api_key,
        index_name=pinecone_index_name,
    )
    idx.create_index_if_not_exists()
    return idx


def process_one(upload, indexer: PineconeDocumentIndexer):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        in_path = tmpdir / upload.name
        in_path.write_bytes(upload.getvalue())

        img_dir = tmpdir / "images"

        extracted = process_any(in_path, img_dir, drive_links=None)
        images: List[Path] = extracted["images"]
        original_image_mapping = extracted["image_mapping"]
        markdown = extracted["markdown"]

        drive_links: Dict[str, str] = {}
        if images:
            st.info(
                f"📤 Uploading {len(images)} images to Drive folder: {drive_folder}"
            )
            with st.spinner("Uploading to Google Drive..."):
                drive_links = upload_images_to_folder(images, drive_folder)

            st.success(f"✅ Uploaded {len(drive_links)} images to Drive")

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
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
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
            "drive_links": drive_links,
            "stats": stats,
        }


if not is_authed:
    st.warning("You must sign in with your **@artisio.co** account to process files.")
    # Temporary session diagnostics — open the app with ?diag=1 to view.
    if st.query_params.get("diag"):
        with st.expander("🔧 Session diagnostics", expanded=True):
            jar = st.session_state.get("_cookie_jar_cache", {})
            st.write(f"streamlit version: `{st.__version__}`")
            st.write(f"header cookies (st.context): `{sorted(st.context.cookies.keys())}`")
            st.write(f"frontend jar cookies: `{sorted(jar.keys())}`")
            st.write(f"probe readback: `{_read_cookie('sb_probe', jar)!r}`")
            _write_cookies({"sb_probe": "cloud-write-ok"})
    st.stop()


def process_zip(upload, indexer: PineconeDocumentIndexer):
    guide_name = Path(upload.name).stem

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        zip_path = tmpdir / upload.name
        zip_path.write_bytes(upload.getvalue())

        extract_dir = tmpdir / "extracted"
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(extract_dir)

        md_files = list(extract_dir.rglob("*.md"))
        if not md_files:
            st.error("No .md file found in the ZIP.")
            return None

        md_path = md_files[0]
        images_dir = md_path.parent / "images"

        extracted = MarkdownProcessor.process_md(md_path, images_dir)
        images: List[Path] = extracted["images"]

        drive_links: Dict[str, str] = {}
        if images:
            st.info(f"📤 Uploading {len(images)} images to Drive → `{drive_folder}/{guide_name}/`")
            with st.spinner("Uploading to Google Drive..."):
                drive_links = upload_guide_images(images, drive_folder, guide_name)
            st.success(f"✅ Uploaded {len(drive_links)} images")
        else:
            st.info("ℹ️ No images found in the ZIP.")

        rebuilt = MarkdownProcessor.process_md(md_path, images_dir, drive_links)
        markdown = rebuilt["markdown"]

        st.info("💾 Indexing in Pinecone...")
        metadata = {
            "filename": upload.name,
            "guide_name": guide_name,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "images_count": len(images),
        }

        try:
            stats = indexer.process_and_index(markdown, metadata)
            st.success("✅ Guide indexed in Pinecone")
        except Exception as e:
            st.error(f"❌ Pinecone indexing failed: {e}")
            stats = {"error": str(e)}

        return {
            "guide_name": guide_name,
            "images": [p.name for p in images],
            "drive_links": drive_links,
            "stats": stats,
        }


tab1, tab2 = st.tabs(["📤 Upload Guides", "📚 Manage Guides"])

with tab1:
    files = st.file_uploader(
        "Upload guides",
        type=["zip", "odt", "pdf", "docx"],
        accept_multiple_files=True,
        help="ZIP (recommended): MD file + images/ folder. Legacy: .odt, .pdf, .docx",
    )

    st.info(
        "📦 **ZIP (recommended):** Pack one guide as a ZIP containing the `.md` file and an `images/` folder.  \n"
        "The ZIP filename becomes the guide name (e.g. `customers.zip` → guide `customers`).  \n"
        "Re-uploading the same ZIP name replaces the existing guide."
    )

    go = st.button("🚀 Process", type="primary", use_container_width=True)

    if go:
        if not files:
            st.error("Please upload at least one file.")
        else:
            idx = make_indexer()
            progress = st.progress(0.0)

            for i, f in enumerate(files, start=1):
                with st.expander(f"📄 {f.name}", expanded=True):
                    try:
                        if f.name.lower().endswith(".zip"):
                            res = process_zip(f, idx)
                            if res:
                                st.subheader(f"Images uploaded: {len(res['images'])}")
                        else:
                            res = process_one(f, idx)
                            st.subheader(f"Images detected: {len(res['images'])}")
                    except Exception as e:
                        st.error(f"❌ {e}")
                progress.progress(i / len(files))

with tab2:
    st.subheader(f"Guides in `{drive_folder}`")

    if st.button("🔄 Refresh", key="refresh_guides"):
        st.session_state.pop("guides", None)

    if "guides" not in st.session_state:
        with st.spinner("Loading guides from Google Drive..."):
            try:
                st.session_state["guides"] = list_guides(drive_folder)
            except Exception as e:
                st.error(f"❌ Could not load guides: {e}")
                st.session_state["guides"] = []

    guides = st.session_state.get("guides", [])

    if not guides:
        st.info("No guides found. Upload a ZIP in the Upload tab to get started.")
    else:
        st.write(f"**{len(guides)} guide(s)** in `{drive_folder}`")

        for guide in guides:
            gid = guide["id"]
            gname = guide["name"]
            modified = guide.get("modifiedTime", "")[:10]
            files = guide.get("files", [])

            with st.expander(f"📚 {gname}  —  {len(files)} image(s)  —  last modified {modified}"):
                # Delete controls render before the thumbnail grid so a failed
                # thumbnail can never take the button down with it.
                confirm_key = f"confirm_delete_guide_{gid}"
                if st.session_state.get(confirm_key):
                    st.warning(
                        f"Delete **{gname}**? This permanently removes all Drive images "
                        f"and Pinecone vectors for this guide."
                    )
                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button("Yes, delete guide", key=f"yes_guide_{gid}", type="primary"):
                            with st.spinner("Deleting..."):
                                try:
                                    delete_guide_folder(gid)
                                    idx = make_indexer()
                                    idx.delete_guide_vectors(gname)
                                    st.success(f"🗑️ Guide '{gname}' deleted from Drive and Pinecone.")
                                    st.session_state[confirm_key] = False
                                    st.session_state.pop("guides", None)
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"❌ Delete failed: {e}")
                    with c2:
                        if st.button("Cancel", key=f"cancel_guide_{gid}"):
                            st.session_state[confirm_key] = False
                            st.rerun()
                else:
                    if st.button("🗑️ Delete guide", key=f"delete_guide_{gid}"):
                        st.session_state[confirm_key] = True
                        st.rerun()

                st.divider()


                if files:
                    cols = st.columns(min(len(files), 4))
                    for i, f in enumerate(files):
                        thumb = f.get("thumbnailLink")
                        with cols[i % 4]:
                            try:
                                thumb_bytes = get_thumbnail_bytes(thumb) if thumb else None
                                if thumb_bytes:
                                    st.image(thumb_bytes, width=120, caption=f["name"])
                                    if st.button("🔍", key=f"view_{f['id']}", help="View full size"):
                                        st.session_state["_preview_file"] = {
                                            "id": f["id"],
                                            "name": f["name"],
                                            "thumbnail": thumb,
                                        }
                                        _image_preview_dialog()
                                else:
                                    st.caption(f["name"])
                            except Exception:
                                st.caption(f"⚠️ {f['name']} (preview unavailable)")
                else:
                    st.write("*(no images)*")

    st.divider()
    st.subheader("🧹 Loose files")
    st.caption(
        "Files sitting directly in the Drive folder (uploaded by the old pipeline). "
        "They are not part of any guide but still count against Drive storage."
    )

    if st.button("🔄 Refresh loose files", key="refresh_loose"):
        st.session_state.pop("loose_files", None)

    if "loose_files" not in st.session_state:
        with st.spinner("Scanning for loose files..."):
            try:
                st.session_state["loose_files"] = list_loose_files(drive_folder)
            except Exception as e:
                st.error(f"❌ Could not scan loose files: {e}")
                st.session_state["loose_files"] = []

    loose = st.session_state.get("loose_files", [])

    if not loose:
        st.info("No loose files — everything in the folder belongs to a guide.")
    else:
        st.write(f"**{len(loose)} loose file(s)** in `{drive_folder}`")

        if st.session_state.get("confirm_delete_loose_all"):
            st.warning(
                f"Delete **all {len(loose)} loose files** from Drive? This cannot be undone."
            )
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Yes, delete all", key="yes_loose_all", type="primary"):
                    with st.spinner("Deleting loose files..."):
                        failed = 0
                        for f in loose:
                            try:
                                delete_file(f["id"])
                            except Exception:
                                failed += 1
                        st.session_state["confirm_delete_loose_all"] = False
                        st.session_state.pop("loose_files", None)
                        if failed:
                            st.error(f"❌ {failed} file(s) could not be deleted.")
                        else:
                            st.rerun()
            with c2:
                if st.button("Cancel", key="cancel_loose_all"):
                    st.session_state["confirm_delete_loose_all"] = False
                    st.rerun()
        else:
            if st.button("🗑️ Delete all loose files", key="delete_loose_all"):
                st.session_state["confirm_delete_loose_all"] = True
                st.rerun()

        for f in loose:
            c1, c2 = st.columns([5, 1])
            with c1:
                st.write(f"📄 {f['name']}  —  modified {f.get('modifiedTime', '')[:10]}")
            with c2:
                if st.button("🗑️", key=f"del_loose_{f['id']}", help="Delete this file"):
                    try:
                        delete_file(f["id"])
                        st.session_state.pop("loose_files", None)
                        st.rerun()
                    except Exception as e:
                        st.error(f"❌ Delete failed: {e}")
