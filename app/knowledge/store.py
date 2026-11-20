"""Generation storage. Publication is one transaction. Search is one read snapshot."""

import hashlib
import math
import struct
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.database import get_engine
from app.tools.exceptions import ToolExecutionError

_BUSY = "An ingestion is already in progress for that document."
_PUBLISH = "The knowledge generation could not be published."
_MISSING = "That document was not found."
_SCAN = "The knowledge search exceeded its candidate limit."
_MEMORY = "The knowledge search exceeded its memory budget."
_INSUFFICIENT = "The knowledge base does not contain enough supported information."
_METADATA_BYTES = 512


def reconcile_staging() -> int:
    """Remove abandoned staging generations. Active and retired generations stay."""
    engine = get_engine()
    with engine.begin() as connection:
        generations = connection.execute(
            text("SELECT document_id, id FROM knowledge_generations WHERE state = 'staging'")
        ).fetchall()
        for document_id, generation_id in generations:
            connection.execute(
                text(
                    "DELETE FROM knowledge_chunks WHERE document_id = :document_id AND generation_id = :generation_id"
                ),
                {"document_id": document_id, "generation_id": generation_id},
            )
            connection.execute(
                text(
                    "DELETE FROM knowledge_generations WHERE document_id = :document_id AND id = :generation_id AND state = 'staging'"
                ),
                {"document_id": document_id, "generation_id": generation_id},
            )
        connection.execute(
            text(
                """
                DELETE FROM knowledge_documents
                WHERE active_generation_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM knowledge_generations
                      WHERE knowledge_generations.document_id = knowledge_documents.id
                  )
                """
            )
        )
    return len(generations)


def find_document_by_source(scope: str, workspace_id: str, relative_path: str, owner_agent_id: str | None) -> dict | None:
    statement = """
        SELECT id, scope, owner_agent_id, created_by_agent_id, workspace_id, relative_path, filename, active_generation_id
        FROM knowledge_documents
        WHERE scope = :scope AND workspace_id = :workspace_id AND relative_path = :relative_path
          AND (
              (:owner IS NULL AND owner_agent_id IS NULL)
              OR owner_agent_id = :owner
          )
    """
    with get_engine().connect() as connection:
        row = connection.execute(
            text(statement),
            {
                "scope": scope,
                "workspace_id": workspace_id,
                "relative_path": relative_path,
                "owner": owner_agent_id,
            },
        ).mappings().first()
    return dict(row) if row else None


def get_document(document_id: str) -> dict | None:
    with get_engine().connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT d.id, d.scope, d.owner_agent_id, d.created_by_agent_id, d.workspace_id,
                       d.relative_path, d.filename, d.active_generation_id,
                       g.embedding_model, g.embedding_digest, g.embedding_dimensions,
                       g.content_sha256, g.chunk_count, g.state
                FROM knowledge_documents d
                LEFT JOIN knowledge_generations g
                  ON g.document_id = d.id AND g.id = d.active_generation_id
                WHERE d.id = :document_id
                """
            ),
            {"document_id": document_id},
        ).mappings().first()
    return dict(row) if row else None


def matching_active(document_id: str, content_sha256: str, model: str, digest: str) -> dict | None:
    document = get_document(document_id)
    if document is None or document.get("state") != "active":
        return None
    if (
        document.get("content_sha256") == content_sha256
        and document.get("embedding_model") == model
        and document.get("embedding_digest") == digest
    ):
        return document
    return None


def begin_staging(
    *,
    document_id: str,
    generation_id: str,
    scope: str,
    owner_agent_id: str | None,
    created_by: str,
    workspace_id: str,
    relative_path: str,
    filename: str,
    content_sha256: str,
    model: str,
    digest: str,
    dimensions: int,
    approval_id: str | None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    engine = get_engine()
    try:
        with engine.begin() as connection:
            existing = connection.execute(
                text("SELECT id FROM knowledge_documents WHERE id = :id"),
                {"id": document_id},
            ).first()
            if existing is None:
                connection.execute(
                    text(
                        """
                        INSERT INTO knowledge_documents (
                            id, scope, owner_agent_id, created_by_agent_id, workspace_id,
                            relative_path, filename, active_generation_id, created_at, updated_at
                        ) VALUES (
                            :id, :scope, :owner, :created_by, :workspace_id,
                            :relative_path, :filename, NULL, :now, :now
                        )
                        """
                    ),
                    {
                        "id": document_id,
                        "scope": scope,
                        "owner": owner_agent_id,
                        "created_by": created_by,
                        "workspace_id": workspace_id,
                        "relative_path": relative_path,
                        "filename": filename,
                        "now": now,
                    },
                )
            connection.execute(
                text(
                    """
                    INSERT INTO knowledge_generations (
                        document_id, id, state, content_sha256, embedding_model, embedding_digest,
                        embedding_dimensions, chunk_count, approval_id, created_at, activated_at
                    ) VALUES (
                        :document_id, :id, 'staging', :content_sha256, :model, :digest,
                        :dimensions, 0, :approval_id, :now, NULL
                    )
                    """
                ),
                {
                    "document_id": document_id,
                    "id": generation_id,
                    "content_sha256": content_sha256,
                    "model": model,
                    "digest": digest,
                    "dimensions": dimensions,
                    "approval_id": approval_id,
                    "now": now,
                },
            )
    except (IntegrityError, OperationalError) as exc:
        raise ToolExecutionError(_BUSY) from exc


def insert_chunk_batch(document_id: str, generation_id: str, rows: list[dict]) -> None:
    statement = text(
        """
        INSERT INTO knowledge_chunks (
            document_id, generation_id, ordinal, text, page_number, section, source_filename,
            embedding, embedding_model, embedding_digest, embedding_dimensions
        ) VALUES (
            :document_id, :generation_id, :ordinal, :text, :page_number, :section, :source_filename,
            :embedding, :model, :digest, :dimensions
        )
        """
    )
    with get_engine().begin() as connection:
        connection.execute(statement, rows)


def publish(
    *,
    document_id: str,
    generation_id: str,
    chunk_count: int,
    recipients: list[str],
    approval_id: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_engine().begin() as connection:
            count = connection.execute(
                text(
                    """
                    SELECT COUNT(*) FROM knowledge_chunks
                    WHERE document_id = :document_id AND generation_id = :generation_id
                    """
                ),
                {"document_id": document_id, "generation_id": generation_id},
            ).scalar_one()
            if int(count) != chunk_count:
                raise ToolExecutionError(_PUBLISH)
            connection.execute(
                text(
                    "UPDATE knowledge_generations SET chunk_count = :count WHERE document_id = :document_id AND id = :generation_id AND state = 'staging'"
                ),
                {"count": chunk_count, "document_id": document_id, "generation_id": generation_id},
            )
            connection.execute(
                text("UPDATE knowledge_documents SET active_generation_id = NULL, updated_at = :now WHERE id = :id"),
                {"id": document_id, "now": now},
            )
            connection.execute(
                text(
                    """
                    UPDATE knowledge_generations
                    SET state = 'retired'
                    WHERE document_id = :document_id AND state = 'active'
                    """
                ),
                {"document_id": document_id},
            )
            activated = connection.execute(
                text(
                    """
                    UPDATE knowledge_generations
                    SET state = 'active', activated_at = :now
                    WHERE document_id = :document_id AND id = :generation_id AND state = 'staging'
                    """
                ),
                {"document_id": document_id, "generation_id": generation_id, "now": now},
            )
            if activated.rowcount != 1:
                raise ToolExecutionError(_PUBLISH)
            pointed = connection.execute(
                text(
                    """
                    UPDATE knowledge_documents
                    SET active_generation_id = :generation_id, updated_at = :now
                    WHERE id = :document_id
                    """
                ),
                {"document_id": document_id, "generation_id": generation_id, "now": now},
            )
            if pointed.rowcount != 1:
                raise ToolExecutionError(_PUBLISH)
            connection.execute(
                text("DELETE FROM knowledge_access WHERE document_id = :document_id"),
                {"document_id": document_id},
            )
            if recipients:
                connection.execute(
                    text(
                        """
                        INSERT INTO knowledge_access (document_id, agent_id, approval_id)
                        VALUES (:document_id, :agent_id, :approval_id)
                        """
                    ),
                    [
                        {"document_id": document_id, "agent_id": agent_id, "approval_id": approval_id}
                        for agent_id in recipients
                    ],
                )
    except (IntegrityError, OperationalError) as exc:
        raise ToolExecutionError(_PUBLISH) from exc


def abandon(document_id: str, generation_id: str) -> None:
    with get_engine().begin() as connection:
        connection.execute(
            text(
                "DELETE FROM knowledge_chunks WHERE document_id = :document_id AND generation_id = :generation_id"
            ),
            {"document_id": document_id, "generation_id": generation_id},
        )
        connection.execute(
            text(
                """
                DELETE FROM knowledge_generations
                WHERE document_id = :document_id AND id = :generation_id AND state = 'staging'
                """
            ),
            {"document_id": document_id, "generation_id": generation_id},
        )
        connection.execute(
            text(
                """
                DELETE FROM knowledge_documents
                WHERE id = :document_id
                  AND active_generation_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM knowledge_generations WHERE document_id = :document_id
                  )
                """
            ),
            {"document_id": document_id},
        )


def delete_document(document_id: str) -> None:
    with get_engine().begin() as connection:
        row = connection.execute(
            text("SELECT id FROM knowledge_documents WHERE id = :id"),
            {"id": document_id},
        ).first()
        if row is None:
            raise ToolExecutionError(_MISSING)
        connection.execute(
            text("UPDATE knowledge_documents SET active_generation_id = NULL WHERE id = :id"),
            {"id": document_id},
        )
        connection.execute(
            text("DELETE FROM knowledge_chunks WHERE document_id = :id"),
            {"id": document_id},
        )
        connection.execute(
            text("DELETE FROM knowledge_generations WHERE document_id = :id"),
            {"id": document_id},
        )
        connection.execute(
            text("DELETE FROM knowledge_access WHERE document_id = :id"),
            {"id": document_id},
        )
        connection.execute(
            text("DELETE FROM knowledge_documents WHERE id = :id"),
            {"id": document_id},
        )


def pack_vector(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *[float(item) for item in values])


def unpack_vector(blob: bytes) -> list[float] | None:
    if len(blob) % 4 != 0 or not blob:
        return None
    count = len(blob) // 4
    values = list(struct.unpack(f"<{count}f", blob))
    if any(not math.isfinite(item) for item in values):
        return None
    return values


def search_authorized(
    *,
    caller: str,
    parent: str,
    model: str,
    digest: str,
    dimensions: int,
    query: list[float],
    allowed_documents: list[str],
    max_scan: int,
    batch_limit: int,
    max_results: int,
    budget_bytes: int,
    excerpt_chars: int,
    query_text: str = "",
) -> dict:
    """Count and scan eligible chunks on one connection. Unauthorized ids are absent.

    Ranking is hybrid: vector similarity mixed with keyword overlap (see lexical.py).
    """
    from app.knowledge.lexical import combine, keyword_score, terms

    query_terms = terms(query_text)
    if not allowed_documents:
        return {"results": [], "notice": _INSUFFICIENT, "citations": []}
    excerpt_bytes = excerpt_chars * 4
    heap_bytes = max_results * ((dimensions * 4) + excerpt_bytes + _METADATA_BYTES)
    connection = get_engine().connect()
    transaction = connection.begin()
    try:
        connection.execute(text("CREATE TEMP TABLE knowledge_allowed (document_id TEXT PRIMARY KEY)"))
        connection.execute(
            text("INSERT INTO knowledge_allowed (document_id) VALUES (:document_id)"),
            [{"document_id": document_id} for document_id in allowed_documents],
        )
        identity = {
            "model": model,
            "digest": digest,
            "dimensions": dimensions,
        }
        count = int(
            connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM knowledge_chunks c
                    JOIN knowledge_allowed allowed ON allowed.document_id = c.document_id
                    JOIN knowledge_documents d ON d.id = c.document_id
                    JOIN knowledge_generations g
                      ON g.document_id = d.id
                     AND g.id = c.generation_id
                     AND g.id = d.active_generation_id
                     AND g.state = 'active'
                    WHERE g.embedding_model = :model
                      AND g.embedding_digest = :digest
                      AND g.embedding_dimensions = :dimensions
                    """
                ),
                identity,
            ).scalar_one()
        )
        if count > max_scan:
            return {"results": [], "notice": _SCAN, "citations": [], "candidate_count": count}
        widest = connection.execute(
            text(
                """
                SELECT MAX(length(c.embedding))
                FROM knowledge_chunks c
                JOIN knowledge_allowed allowed ON allowed.document_id = c.document_id
                JOIN knowledge_documents d ON d.id = c.document_id
                JOIN knowledge_generations g
                  ON g.document_id = d.id
                 AND g.id = c.generation_id
                 AND g.id = d.active_generation_id
                 AND g.state = 'active'
                WHERE g.embedding_model = :model
                  AND g.embedding_digest = :digest
                  AND g.embedding_dimensions = :dimensions
                """
            ),
            identity,
        ).scalar_one()
        vector_bytes = int(widest or 0)
        if vector_bytes and heap_bytes + (2 * vector_bytes) + excerpt_bytes + _METADATA_BYTES > budget_bytes:
            return {"results": [], "notice": _MEMORY, "citations": [], "candidate_count": count}
        from app.core.config import get_settings

        # Hybrid ranking reads each chunk's full text; count it in the budget.
        full_text_bytes = get_settings().knowledge_chunk_chars * 4
        per_candidate = (2 * vector_bytes) + excerpt_bytes + full_text_bytes + _METADATA_BYTES
        room = 0 if per_candidate <= 0 or heap_bytes >= budget_bytes else (budget_bytes - heap_bytes) // per_candidate
        page_size = min(batch_limit, int(room))
        if count and page_size < 1:
            return {"results": [], "notice": _MEMORY, "citations": [], "candidate_count": count}
        ranked: list[tuple[float, str, int, dict]] = []
        last_document = ""
        last_ordinal = -1
        while True:
            rows = connection.execute(
                text(
                    """
                    SELECT c.document_id, c.ordinal, substr(c.text, 1, :excerpt) AS excerpt, c.text AS full_text,
                           c.page_number, c.section, c.source_filename, c.embedding, d.filename
                    FROM knowledge_chunks c
                    JOIN knowledge_allowed allowed ON allowed.document_id = c.document_id
                    JOIN knowledge_documents d ON d.id = c.document_id
                    JOIN knowledge_generations g
                      ON g.document_id = d.id
                     AND g.id = c.generation_id
                     AND g.id = d.active_generation_id
                     AND g.state = 'active'
                    WHERE g.embedding_model = :model
                      AND g.embedding_digest = :digest
                      AND g.embedding_dimensions = :dimensions
                      AND (c.document_id > :last_document OR (c.document_id = :last_document AND c.ordinal > :last_ordinal))
                    ORDER BY c.document_id, c.ordinal
                    LIMIT :page_size
                    """
                ),
                {
                    **identity,
                    "excerpt": excerpt_chars,
                    "last_document": last_document,
                    "last_ordinal": last_ordinal,
                    "page_size": page_size,
                },
            ).mappings().all()
            if not rows:
                break
            for row in rows:
                vector = unpack_vector(row["embedding"])
                if vector is None or len(vector) != dimensions:
                    continue
                vector_score = _cosine(query, vector)
                lexical = keyword_score(query_terms, row["full_text"]) if query_terms else 0.0
                score = combine(vector_score, lexical) if query_terms else vector_score
                item = {
                    "document_id": row["document_id"],
                    "ordinal": row["ordinal"],
                    "excerpt": row["excerpt"],
                    "page_number": row["page_number"],
                    "section": row["section"],
                    "filename": row["source_filename"] or row["filename"],
                    "score": score,
                    "vector_score": vector_score,
                    "keyword_score": lexical,
                }
                ranked.append((score, row["document_id"], int(row["ordinal"]), item))
                ranked.sort(key=lambda entry: (-entry[0], entry[1], entry[2]))
                del ranked[max_results:]
            last_document = rows[-1]["document_id"]
            last_ordinal = int(rows[-1]["ordinal"])
            del rows
        results = [entry[3] for entry in ranked]
        notice = None if results else _INSUFFICIENT
        return {
            "results": results,
            "citations": [_citation(item) for item in results],
            "notice": notice,
            "candidate_count": count,
        }
    finally:
        transaction.rollback()
        connection.close()


def candidate_documents(
    *,
    caller: str,
    parent: str,
    model: str,
    digest: str,
    dimensions: int,
    scope: str | None,
    workspace_id: str | None,
    document_id: str | None,
) -> list[dict]:
    filters = [
        "g.state = 'active'",
        "d.active_generation_id = g.id",
        "g.embedding_model = :model",
        "g.embedding_digest = :digest",
        "g.embedding_dimensions = :dimensions",
        """
        (
            (d.scope = 'agent_private' AND d.owner_agent_id = :caller)
            OR (
                d.scope = 'user_private' AND (
                    (:parent != '' AND :caller = :parent)
                    OR EXISTS (
                        SELECT 1 FROM knowledge_access access_row
                        WHERE access_row.document_id = d.id AND access_row.agent_id = :caller
                    )
                )
            )
            OR (
                d.scope = 'project' AND EXISTS (
                    SELECT 1 FROM agent_workspace_grants grant_row
                    WHERE grant_row.agent_id = :caller AND grant_row.workspace_id = d.workspace_id
                )
            )
            OR (
                d.scope = 'shared' AND EXISTS (
                    SELECT 1 FROM knowledge_access access_row
                    WHERE access_row.document_id = d.id AND access_row.agent_id = :caller
                )
            )
        )
        """,
    ]
    params: dict = {
        "caller": caller,
        "parent": parent,
        "model": model,
        "digest": digest,
        "dimensions": dimensions,
    }
    if scope:
        filters.append("d.scope = :scope")
        params["scope"] = scope
    if workspace_id:
        filters.append("d.workspace_id = :workspace_id")
        params["workspace_id"] = workspace_id
    if document_id:
        filters.append("d.id = :document_id")
        params["document_id"] = document_id
    statement = f"""
        SELECT d.id, d.scope, d.workspace_id, d.relative_path, d.filename
        FROM knowledge_documents d
        JOIN knowledge_generations g ON g.document_id = d.id AND g.id = d.active_generation_id
        WHERE {' AND '.join(filters)}
        ORDER BY d.id
    """
    with get_engine().connect() as connection:
        rows = connection.execute(text(statement), params).mappings().all()
    return [dict(row) for row in rows]


def incompatible_documents(*, caller: str, parent: str, digest: str) -> bool:
    """True when an authorized active generation uses another model digest."""
    statement = """
        SELECT 1
        FROM knowledge_documents d
        JOIN knowledge_generations g ON g.document_id = d.id AND g.id = d.active_generation_id
        WHERE g.state = 'active'
          AND g.embedding_digest != :digest
          AND (
                (d.scope = 'agent_private' AND d.owner_agent_id = :caller)
                OR (
                    d.scope = 'user_private' AND (
                        (:parent != '' AND :caller = :parent)
                        OR EXISTS (
                            SELECT 1 FROM knowledge_access access_row
                            WHERE access_row.document_id = d.id AND access_row.agent_id = :caller
                        )
                    )
                )
                OR (
                    d.scope = 'project' AND EXISTS (
                        SELECT 1 FROM agent_workspace_grants grant_row
                        WHERE grant_row.agent_id = :caller AND grant_row.workspace_id = d.workspace_id
                    )
                )
                OR (
                    d.scope = 'shared' AND EXISTS (
                        SELECT 1 FROM knowledge_access access_row
                        WHERE access_row.document_id = d.id AND access_row.agent_id = :caller
                    )
                )
          )
        LIMIT 1
    """
    with get_engine().connect() as connection:
        row = connection.execute(
            text(statement),
            {"caller": caller, "parent": parent, "digest": digest},
        ).first()
    return row is not None


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _citation(item: dict) -> dict:
    return {
        "document_id": item["document_id"],
        "filename": item["filename"],
        "page_number": item["page_number"],
        "section": item["section"],
        "ordinal": item["ordinal"],
    }


def _cosine(left: list[float], right: list[float]) -> float:
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for first, second in zip(left, right):
        dot += first * second
        left_norm += first * first
        right_norm += second * second
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (math.sqrt(left_norm) * math.sqrt(right_norm))
