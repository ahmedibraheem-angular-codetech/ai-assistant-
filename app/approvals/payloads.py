"""Ciphertext for one MCP approval. The row holds no plaintext."""

from sqlalchemy import LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class McpExecutionPayloadRecord(Base):
    """AES-256-GCM ciphertext bound to one approval.

    nonce is the 12-byte GCM nonce. ciphertext includes the authentication tag.
    There is no foreign-key cascade. Callers delete the row in the same
    transaction that leaves the approval.
    """

    __tablename__ = "mcp_execution_payloads"

    approval_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
