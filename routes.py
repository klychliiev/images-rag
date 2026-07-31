# routes.py
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from loguru import logger
from pinecone import Pinecone

from config import settings
from drive_uploader import upload_images_to_folder
from pinecone_service import PineconeDocumentIndexer
from processor import process_any  # <-- unified dispatcher for ODT/PDF/DOCX
from schemas import ProcessingResponse

app = APIRouter()

SUPPORTED_EXTS = {".odt", ".pdf", ".docx"}


async def _process_and_index(
    file: UploadFile,
    *,
    index_name: str | None,
    document_title: str | None,
    include_markdown: bool,
    upload_images_to_drive: bool,
    drive_folder_name: str | None,
) -> ProcessingResponse:
    """Shared handler used by both routes."""
    # --- persist upload to a temp file ---
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir_path = Path(temp_dir)
        in_path = temp_dir_path / file.filename
        with open(in_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        ext = in_path.suffix.lower()
        if ext not in SUPPORTED_EXTS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(SUPPORTED_EXTS))}",
            )

        logger.info(f"Processing file: {file.filename} ({ext})")

        # --- 1) Extract images & text (first pass, without Drive links) ---
        images_dir = temp_dir_path / "images"
        first_pass = process_any(in_path, images_dir, drive_links=None)
        images = first_pass["images"]               # List[Path]
        markdown_content = first_pass["markdown"]   # str

        # --- 2) Optional: upload images to Drive, then regenerate markdown with direct links ---
        drive_links: dict[str, str] = {}
        if upload_images_to_drive and images:
            try:
                drive_links = upload_images_to_folder(
                    image_paths=images,
                    folder_name=drive_folder_name or "Invoice Images",
                )
                logger.success(f"Uploaded {len(drive_links)} images to Drive.")
                # Re-run to inject direct-view links
                second_pass = process_any(in_path, images_dir, drive_links=drive_links)
                markdown_content = second_pass["markdown"]
            except Exception as e:
                logger.error(f"Drive upload failed: {e}")

        # --- 3) Basic content sanity check ---
        if not markdown_content or len(markdown_content.strip()) < 10:
            raise HTTPException(
                status_code=400,
                detail="No meaningful content found in the file.",
            )

        logger.info(f"Extracted {len(markdown_content)} characters of content")

        # --- 4) Index in Pinecone ---
        indexer = PineconeDocumentIndexer(
            pinecone_api_key=settings.pinecone_api_key,
            openai_api_key=settings.openai_api_key,
            index_name=index_name or settings.pinecone_index_default,
        )
        indexer.create_index_if_not_exists()

        metadata = {
            "document_id": str(uuid.uuid4()),
            "source_file": file.filename,
            "document_type": ext.lstrip("."),  # odt | pdf | docx
            "document_title": document_title or file.filename,
            "upload_timestamp": datetime.utcnow().isoformat(),
            "content_length": len(markdown_content),
            "index_name": index_name or settings.pinecone_index_default,
        }
        processing_stats = indexer.process_and_index(markdown_content, metadata)

        # --- 5) Build response ---
        return ProcessingResponse(
            success=True,
            message=f"Successfully processed and indexed '{file.filename}'",
            document_id=metadata["document_id"],
            chunks_created=processing_stats["chunks_created"],
            markdown_content=markdown_content if include_markdown else None,
            processing_stats={
                **processing_stats,
                "drive_images_uploaded": len(drive_links),
                "drive_folder_used": (drive_folder_name or "Invoice Images") if drive_links else None,
            },
        )


# NEW: generic endpoint for .odt / .pdf / .docx
@app.post("/upload/", response_model=ProcessingResponse)
async def upload_file(
    file: UploadFile = File(...),
    index_name: str | None = Form(default=None),
    document_title: str | None = Form(default=None),
    include_markdown: bool = Form(default=False),
    upload_images_to_drive: bool = Form(default=True),
    drive_folder_name: str | None = Form(default="Invoice Images"),
):
    return await _process_and_index(
        file,
        index_name=index_name,
        document_title=document_title,
        include_markdown=include_markdown,
        upload_images_to_drive=upload_images_to_drive,
        drive_folder_name=drive_folder_name,
    )


# Back-compat: original route name, now supports pdf/docx too
@app.post("/upload-odt/", response_model=ProcessingResponse)
async def upload_odt_file(
    file: UploadFile = File(...),
    index_name: str | None = Form(default=None),
    document_title: str | None = Form(default=None),
    include_markdown: bool = Form(default=False),
    upload_images_to_drive: bool = Form(default=True),
    drive_folder_name: str | None = Form(default="Invoice Images"),
):
    return await _process_and_index(
        file,
        index_name=index_name,
        document_title=document_title,
        include_markdown=include_markdown,
        upload_images_to_drive=upload_images_to_drive,
        drive_folder_name=drive_folder_name,
    )


@app.get("/health/")
async def health_check():
    """Health check endpoint"""
    try:
        pc = Pinecone(api_key=settings.pinecone_api_key)
        indexes = pc.list_indexes()

        return {
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "pinecone_connected": True,
            "available_indexes": len(indexes),
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "timestamp": datetime.utcnow().isoformat(),
            "pinecone_connected": False,
            "error": str(e),
        }
