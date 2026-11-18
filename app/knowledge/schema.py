"""Knowledge tables, foreign keys, and triggers.

Startup creates these with CREATE TABLE IF NOT EXISTS before the ORM
create_all pass. SQLite accepts the circular document and generation
foreign keys at insert time. Triggers reject an active pointer that is
not an active generation of the same document.
"""

from sqlalchemy import Engine, text

_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS knowledge_documents (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        scope VARCHAR(32) NOT NULL,
        owner_agent_id VARCHAR(36),
        created_by_agent_id VARCHAR(36) NOT NULL,
        workspace_id VARCHAR(36) NOT NULL,
        relative_path VARCHAR(400) NOT NULL,
        filename VARCHAR(200) NOT NULL,
        active_generation_id VARCHAR(36),
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        FOREIGN KEY (id, active_generation_id)
            REFERENCES knowledge_generations (document_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_generations (
        document_id VARCHAR(36) NOT NULL,
        id VARCHAR(36) NOT NULL,
        state VARCHAR(16) NOT NULL,
        content_sha256 VARCHAR(64) NOT NULL,
        embedding_model VARCHAR(200) NOT NULL,
        embedding_digest VARCHAR(64) NOT NULL,
        embedding_dimensions INTEGER NOT NULL,
        chunk_count INTEGER NOT NULL,
        approval_id VARCHAR(36),
        created_at DATETIME NOT NULL,
        activated_at DATETIME,
        PRIMARY KEY (document_id, id),
        FOREIGN KEY (document_id) REFERENCES knowledge_documents (id) ON DELETE CASCADE,
        CHECK (state IN ('staging', 'active', 'retired')),
        CHECK (embedding_dimensions > 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_chunks (
        document_id VARCHAR(36) NOT NULL,
        generation_id VARCHAR(36) NOT NULL,
        ordinal INTEGER NOT NULL,
        text TEXT NOT NULL,
        page_number INTEGER,
        section VARCHAR(200),
        source_filename VARCHAR(200) NOT NULL,
        embedding BLOB NOT NULL,
        embedding_model VARCHAR(200) NOT NULL,
        embedding_digest VARCHAR(64) NOT NULL,
        embedding_dimensions INTEGER NOT NULL,
        PRIMARY KEY (document_id, generation_id, ordinal),
        FOREIGN KEY (document_id, generation_id)
            REFERENCES knowledge_generations (document_id, id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_access (
        document_id VARCHAR(36) NOT NULL,
        agent_id VARCHAR(36) NOT NULL,
        approval_id VARCHAR(36) NOT NULL,
        PRIMARY KEY (document_id, agent_id),
        FOREIGN KEY (document_id) REFERENCES knowledge_documents (id) ON DELETE CASCADE
    )
    """,
)

_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_one_active ON knowledge_generations (document_id) WHERE state = 'active'",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_one_staging ON knowledge_generations (document_id) WHERE state = 'staging'",
    "CREATE INDEX IF NOT EXISTS ix_knowledge_generation_identity ON knowledge_generations (embedding_model, embedding_digest, embedding_dimensions)",
    "CREATE INDEX IF NOT EXISTS ix_knowledge_document_scope ON knowledge_documents (scope, owner_agent_id)",
    "CREATE INDEX IF NOT EXISTS ix_knowledge_document_path ON knowledge_documents (workspace_id, relative_path)",
    "CREATE INDEX IF NOT EXISTS ix_knowledge_access_agent ON knowledge_access (agent_id, document_id)",
)

_TRIGGERS = (
    """
    CREATE TRIGGER IF NOT EXISTS knowledge_active_pointer_state
    BEFORE UPDATE OF active_generation_id ON knowledge_documents
    FOR EACH ROW
    WHEN NEW.active_generation_id IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, 'active generation is not active')
        WHERE NOT EXISTS (
            SELECT 1 FROM knowledge_generations
            WHERE document_id = NEW.id
              AND id = NEW.active_generation_id
              AND state = 'active'
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS knowledge_retire_pointed
    BEFORE UPDATE OF state ON knowledge_generations
    FOR EACH ROW
    WHEN OLD.state = 'active' AND NEW.state != 'active'
    BEGIN
        SELECT RAISE(ABORT, 'cannot retire the active pointer')
        WHERE EXISTS (
            SELECT 1 FROM knowledge_documents
            WHERE id = OLD.document_id AND active_generation_id = OLD.id
        );
    END
    """,
)


def ensure_knowledge_schema(engine: Engine) -> None:
    """Create knowledge tables when they are missing. Existing rows stay."""
    with engine.begin() as connection:
        for statement in _TABLES:
            connection.execute(text(statement))
        for statement in _INDEXES:
            connection.execute(text(statement))
        for statement in _TRIGGERS:
            connection.execute(text(statement))
