from pydantic import BaseModel


class ProcessingResponse(BaseModel):
    success: bool
    message: str
    document_id: str
    chunks_created: int
    markdown_content: str | None = None
    processing_stats: dict


class ErrorResponse(BaseModel):
    error: str
    detail: str
