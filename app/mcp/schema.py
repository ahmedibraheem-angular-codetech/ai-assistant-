"""Singleton revision row for the operator MCP file.

The file remains the authority. This table records only the last committed
global revision and epoch.
"""

from sqlalchemy import Engine, text

_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mcp_config_revisions (
        id VARCHAR(16) PRIMARY KEY,
        revision CHAR(64) NOT NULL,
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        loaded_at DATETIME NOT NULL,
        CHECK (id = 'global'),
        CHECK (length(revision) = 64)
    )
    """,
)


def ensure_mcp_schema(engine: Engine) -> None:
    with engine.begin() as connection:
        for statement in _STATEMENTS:
            connection.execute(text(statement))
