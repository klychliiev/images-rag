import hashlib
import re
import time
from typing import Any

from langchain.text_splitter import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from langchain_openai.embeddings import OpenAIEmbeddings
from loguru import logger
from pinecone import Pinecone, ServerlessSpec

from config import settings

# Split on markdown headers first so each section (with its image) stays in one chunk.
# Then only size-split sections that are genuinely too long.
_HEADERS_TO_SPLIT = [("#", "h1"), ("##", "h2"), ("###", "h3")]
_md_splitter = MarkdownHeaderTextSplitter(
    headers_to_split_on=_HEADERS_TO_SPLIT,
    strip_headers=False,
)
_size_splitter = RecursiveCharacterTextSplitter(
    chunk_size=700,
    chunk_overlap=80,
    length_function=len,
    separators=["\n\n", "\n"],
)


def _source_prefix(filename: str) -> str:
    """Stable Pinecone-safe ID prefix derived from the source filename."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", filename)[:40]


class PineconeDocumentIndexer:
    def __init__(
        self,
        pinecone_api_key: str,
        openai_api_key: str,
        index_name: str,
    ):
        self.pc = Pinecone(api_key=pinecone_api_key)
        self.index_name = index_name
        self.index = None

        self.embeddings = OpenAIEmbeddings(
            openai_api_key=openai_api_key, model="text-embedding-3-small"
        )

        self.batch_size = 200

    def _list_index_names(self) -> list[str]:
        idxs = self.pc.list_indexes()

        return [getattr(i, "name", i.get("name")) for i in idxs]

    def _wait_until_ready(self, timeout_s: int = 180, poll_s: int = 2):
        start = time.time()
        while time.time() - start < timeout_s:
            desc = self.pc.describe_index(self.index_name)

            if (
                getattr(desc, "status", {}).get("ready", False)
                if isinstance(desc.status, dict)
                else getattr(desc.status, "ready", False)
            ):
                return
            time.sleep(poll_s)
        raise RuntimeError(f"Index '{self.index_name}' not ready after {timeout_s}s")

    def create_index_if_not_exists(self, dimension: int = 1536, metric: str = "cosine"):
        try:
            names = self._list_index_names()
            if self.index_name not in names:
                logger.info(f"Creating index '{self.index_name}'...")
                self.pc.create_index(
                    name=self.index_name,
                    dimension=dimension,
                    metric=metric,
                    spec=ServerlessSpec(
                        cloud=settings.pinecone_cloud,  # make configurable
                        region=settings.pinecone_region,
                    ),
                )
                logger.info(f"Index '{self.index_name}' creation requested.")
            else:
                logger.info(f"Index '{self.index_name}' already exists.")

            self._wait_until_ready()
            self.index = self.pc.Index(self.index_name)
            logger.success(f"Index '{self.index_name}' is ready.")

        except Exception as e:
            logger.error(f"Error creating or preparing index: {e}")
            raise

    def ensure_index(self):
        if self.index is None:
            raise RuntimeError(
                "Pinecone index handle not initialized. "
                "Call create_index_if_not_exists() before upserting."
            )

    def chunk_document(self, content: str) -> list[str]:
        # Split by markdown headers first so each procedure + its image stays together.
        # Fall back to pure size-splitting if the document has no headers.
        header_docs = _md_splitter.split_text(content or "")
        chunks: list[str] = []
        for doc in header_docs:
            text = doc.page_content
            if not text.strip():
                continue
            if len(text) <= 700:
                chunks.append(text)
            else:
                chunks.extend(_size_splitter.split_text(text))

        if not chunks:
            chunks = _size_splitter.split_text(content or "")

        if not chunks:
            return []

        logger.info(f"Document split into {len(chunks)} chunks")
        lens = [len(c) for c in chunks]
        logger.info(
            f"Chunk length stats - Min: {min(lens)}, Max: {max(lens)}, Avg: {sum(lens)/len(lens):.0f}"
        )
        return chunks

    def create_embeddings(self, chunks: list[str]) -> list[list[float]]:
        if not chunks:
            return []
        logger.info("Generating embeddings...")
        embeddings = self.embeddings.embed_documents(chunks)
        logger.info(f"Generated {len(embeddings)} embeddings")
        return embeddings

    def prepare_vectors(
        self,
        chunks: list[str],
        embeddings: list[list[float]],
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        vectors = []
        base_metadata = metadata or {}
        filename = base_metadata.get("filename", "doc")
        prefix = _source_prefix(filename)
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            chunk_hash = hashlib.md5(chunk.encode()).hexdigest()[:8]
            vid = f"{prefix}_{i}_{chunk_hash}"
            vectors.append(
                {
                    "id": vid,
                    "values": embedding,
                    "metadata": {
                        **base_metadata,
                        "chunk_index": i,
                        "text": chunk,
                        "chunk_length": len(chunk),
                    },
                }
            )
        return vectors

    def upsert_vectors_batch(self, vectors: list[dict[str, Any]]) -> None:
        self.ensure_index()
        total = len(vectors)
        if total == 0:
            logger.info("No vectors to upsert.")
            return
        logger.info(f"Upserting {total} vectors in batches of {self.batch_size}...")
        for i in range(0, total, self.batch_size):
            batch = vectors[i : i + self.batch_size]
            try:
                self.index.upsert(vectors=batch)
                logger.info(
                    f"Batch {i//self.batch_size + 1}: Upserted {len(batch)} vectors"
                )
            except Exception as e:
                logger.error(f"Error upserting batch {i//self.batch_size + 1}: {e}")
                raise

    def delete_guide_vectors(self, guide_name: str) -> None:
        """Delete all Pinecone vectors for a guide using metadata filter."""
        self.ensure_index()
        try:
            self.index.delete(filter={"guide_name": {"$eq": guide_name}})
            logger.info(f"Deleted Pinecone vectors for guide '{guide_name}'")
        except Exception as e:
            logger.warning(f"Could not delete vectors for guide '{guide_name}': {e}")

    def delete_document_vectors(self, filename: str) -> int:
        """Delete all previously indexed vectors for a given file (by ID prefix)."""
        self.ensure_index()
        prefix = _source_prefix(filename)
        deleted = 0
        try:
            for id_page in self.index.list(prefix=prefix):
                if id_page:
                    self.index.delete(ids=id_page)
                    deleted += len(id_page)
            if deleted:
                logger.info(f"Deleted {deleted} stale vectors for '{filename}'")
        except Exception as e:
            logger.warning(f"Could not delete stale vectors for '{filename}': {e}")
        return deleted

    def process_and_index(
        self, content: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.ensure_index()
        filename = (metadata or {}).get("filename", "")
        if filename:
            self.delete_document_vectors(filename)
        chunks = self.chunk_document(content or "")
        embeddings = self.create_embeddings(chunks)
        vectors = self.prepare_vectors(chunks, embeddings, metadata)
        self.upsert_vectors_batch(vectors)

        try:
            stats = self.index.describe_index_stats()
            total = stats.get("total_vector_count")
        except Exception:
            total = None
        return {
            "chunks_created": len(chunks),
            "vectors_upserted": len(vectors),
            "index_total_vectors": total,
        }
