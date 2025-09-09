import os
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pinecone import Pinecone

from config import settings
from drive_uploader import upload_images_to_folder
from logger import logger
from pinecone_service import PineconeDocumentIndexer
from processor import ODTProcessor
from schemas import ProcessingResponse

app = APIRouter()


@app.post("/upload-odt/", response_model=ProcessingResponse)
async def upload_odt_file(
    file: UploadFile = File(...),
    index_name: str | None = Form(default=None),
    document_title: str | None = Form(default=None),
    include_markdown: bool = Form(default=False),
    upload_images_to_drive: bool = Form(default=True),  # NEW
    drive_folder_name: str | None = Form(default="Invoice Images"),  # NEW
):

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir_path = Path(temp_dir)
        odt_file_path = temp_dir_path / file.filename
        with open(odt_file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        logger.info(f"Processing ODT file: {file.filename}")

        images_dir = temp_dir_path / "images"
        extracted_images, image_mapping = ODTProcessor.extract_images_from_odt(
            odt_file_path, images_dir
        )

        drive_links: dict[str, str] = {}
        if upload_images_to_drive and extracted_images:
            try:
                drive_links = upload_images_to_folder(
                    image_paths=extracted_images,
                    folder_name=drive_folder_name or "Invoice Images",
                )
                logger.success(f"Uploaded {len(drive_links)} images to Drive.")
            except Exception as e:
                logger.error(f"⚠️ Drive upload failed: {e}")

        try:
            if drive_links:
                markdown_content = ODTProcessor.extract_text_with_links(
                    odt_file_path, image_mapping, drive_links
                )
            else:
                markdown_content = ODTProcessor.extract_text_from_odt(odt_file_path)
        except Exception as e:
            raise HTTPException(
                status_code=400, detail=f"Content processing error: {str(e)}"
            )

        if not markdown_content or len(markdown_content.strip()) < 10:
            raise HTTPException(
                status_code=400, detail="No meaningful content found in ODT file."
            )

        logger.info(f"Extracted {len(markdown_content)} characters of content")

        indexer = PineconeDocumentIndexer(
            pinecone_api_key=settings.pinecone_api_key,
            openai_api_key=settings.openai_api_key,
            index_name=settings.default_index_name,
        )
        indexer.create_index_if_not_exists()
        metadata = {
            "document_id": str(uuid.uuid4()),
            "source_file": file.filename,
            "document_type": "odt",
            "document_title": document_title or file.filename,
            "upload_timestamp": datetime.utcnow().isoformat(),
            "content_length": len(markdown_content),
            "index_name": index_name,
        }
        processing_stats = indexer.process_and_index(markdown_content, metadata)

        return ProcessingResponse(
            success=True,
            message=f"Successfully processed and indexed '{file.filename}'",
            document_id=metadata["document_id"],
            chunks_created=processing_stats["chunks_created"],
            markdown_content=markdown_content if include_markdown else None,
            processing_stats={
                **processing_stats,
                "drive_images_uploaded": len(drive_links),
                "drive_folder_used": drive_folder_name if drive_links else None,
            },
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
