from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DATABASE_PATH = _PROJECT_ROOT / "data" / "assistant.db"
_DEFAULT_DATABASE_URL = f"sqlite:///{_DEFAULT_DATABASE_PATH.as_posix()}"


class Settings(BaseSettings):
    app_name: str = "Personal AI Assistant"
    app_env: str = "development"
    api_prefix: str = "/api/v1"
    ollama_base_url: str = "http://localhost:11434"
    ollama_timeout_seconds: float = 120.0
    ollama_model: str = ""
    # Recent messages included in one model request. Older messages stay stored.
    chat_context_message_limit: int = Field(default=20, ge=1)
    # Enabled memories appended to one model request. Unrelated memories stay stored.
    memory_injection_limit: int = Field(default=5, ge=1, le=20)
    # How long a remember or forget proposal can be confirmed, in seconds.
    memory_action_ttl_seconds: int = Field(default=900, ge=60, le=86_400)
    # Automatic agent selection when the request does not name an agent.
    auto_agent_routing_enabled: bool = True
    # Empty means the routing call uses OLLAMA_MODEL.
    agent_routing_model: str = ""
    agent_routing_confidence_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    agent_routing_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    # Tool calls are local unless a tool itself uses the network.
    tool_execution_enabled: bool = True
    tool_max_calls_per_turn: int = Field(default=3, ge=1, le=8)
    tool_default_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    # Empty means the default assistant is not offered any tools.
    default_assistant_tools: str = ""
    default_timezone: str = "UTC"
    # Web search is external network access, not local inference. Off by default.
    web_search_enabled: bool = False
    web_search_provider: str = "duckduckgo"
    web_search_max_results: int = Field(default=5, ge=1, le=10)
    # Optional second search endpoint. Empty means it is not used.
    # The key comes from the environment and is never written into logs.
    web_search_secondary_url: str = ""
    web_search_secondary_key: str = ""
    # Brave web search. An empty key leaves the provider unused.
    # Brave's published Search price is $5 per 1,000 requests, with monthly
    # credit that still requires a Brave plan. This project does not treat it as free.
    web_search_brave_key: str = ""
    web_search_brave_url: str = "https://api.search.brave.com/res/v1/web/search"
    web_search_rate_limit_per_minute: int = Field(default=30, ge=1, le=120)
    research_max_queries: int = Field(default=3, ge=1, le=5)
    research_max_pages: int = Field(default=3, ge=1, le=5)
    research_max_page_bytes: int = Field(default=200_000, ge=1_024, le=1_000_000)
    research_max_total_bytes: int = Field(default=600_000, ge=1_024, le=2_000_000)
    research_timeout_seconds: float = Field(default=8.0, gt=0, le=30)
    research_max_redirects: int = Field(default=3, ge=0, le=5)
    research_max_concurrent: int = Field(default=2, ge=1, le=4)
    research_retries: int = Field(default=1, ge=0, le=2)
    # Local knowledge. The embedding model name is a tag. Its digest is resolved
    # from Ollama and is not stored in these settings.
    parent_agent_id: str = ""
    knowledge_embedding_model: str = "qwen3-embedding:latest"
    knowledge_chunk_chars: int = Field(default=800, ge=200, le=4_000)
    knowledge_chunk_overlap: int = Field(default=100, ge=0, le=1_000)
    knowledge_max_file_bytes: int = Field(default=1_048_576, ge=1_024, le=5_242_880)
    knowledge_max_chunks: int = Field(default=200, ge=1, le=200)
    knowledge_embed_batch: int = Field(default=8, ge=1, le=32)
    knowledge_max_scan: int = Field(default=5_000, ge=1, le=20_000)
    knowledge_candidate_batch: int = Field(default=200, ge=1, le=2_000)
    knowledge_memory_budget_bytes: int = Field(default=8_388_608, ge=262_144, le=33_554_432)
    knowledge_max_results: int = Field(default=5, ge=1, le=8)
    knowledge_max_dimensions: int = Field(default=8_192, ge=1, le=8_192)
    knowledge_excerpt_chars: int = Field(default=400, ge=40, le=2_000)
    knowledge_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    # Local API only. Authentication is required before any non-loopback deployment.
    api_host: str = "127.0.0.1"
    # Concurrent MCP server processes. Each one is a child process on this Mac.
    mcp_max_processes: int = Field(default=2, ge=1, le=16)
    # Default daily limits per agent. Empty means unlimited; per-agent budgets override.
    agent_daily_tool_calls: int | None = Field(default=None, ge=1, le=100_000)
    agent_daily_chat_turns: int | None = Field(default=None, ge=1, le=100_000)
    # Code sandbox (run_code). Off by default. Each run needs approval.
    sandbox_enabled: bool = False
    sandbox_docker_path: str = "docker"
    sandbox_timeout_seconds: int = Field(default=20, ge=1, le=120)
    sandbox_memory_mb: int = Field(default=512, ge=64, le=4_096)
    sandbox_cpus: float = Field(default=1.0, gt=0, le=4)
    sandbox_python_image: str = "python:3.12-alpine"
    sandbox_node_image: str = "node:22-alpine"
    sandbox_shell_image: str = "alpine:3.20"
    # Background study scheduler for agents with a study plan.
    study_scheduler_enabled: bool = True
    # Read-only file tools. Sizes are bytes. Output is capped so one file cannot flood a turn.
    file_max_bytes: int = Field(default=1_048_576, ge=1_024, le=5_242_880)
    file_max_output_chars: int = Field(default=20_000, ge=500, le=100_000)
    file_list_limit: int = Field(default=200, ge=1, le=500)
    file_search_limit: int = Field(default=20, ge=1, le=50)
    file_search_scan_limit: int = Field(default=200, ge=1, le=1_000)
    file_project_depth: int = Field(default=3, ge=1, le=6)
    file_spreadsheet_max_rows: int = Field(default=100, ge=1, le=500)
    file_spreadsheet_max_columns: int = Field(default=30, ge=1, le=100)
    file_json_max_depth: int = Field(default=8, ge=1, le=20)
    # One file operation may hold at most this many extracted bytes.
    file_max_memory_bytes: int = Field(default=33_554_432, ge=1_024, le=67_108_864)
    # Isolated tool processes that may run at the same time.
    file_max_concurrent_workers: int = Field(default=4, ge=1, le=16)
    # CPU-time backstop inside a worker, in seconds. The parent timeout is shorter.
    file_worker_cpu_seconds: int = Field(default=30, ge=1, le=60)
    # Isolated worker pipe caps after UTF-8 serialization. Parent enforces both;
    # the worker applies the stdout cap in _emit as defense in depth.
    tool_worker_stdout_max_bytes: int = Field(default=1_048_576, ge=64, le=16_777_216)
    tool_worker_stderr_max_bytes: int = Field(default=65_536, ge=64, le=1_048_576)
    # Search snippets returned to a model turn.
    file_search_snippet_chars: int = Field(default=180, ge=40, le=500)
    # In-memory archive reads. These limits reject zip bombs before and during decompression.
    file_zip_max_uncompressed_bytes: int = Field(default=33_554_432, ge=4_096, le=67_108_864)
    file_zip_max_member_bytes: int = Field(default=8_388_608, ge=1_024, le=33_554_432)
    file_zip_max_files: int = Field(default=128, ge=1, le=1_000)
    file_zip_max_ratio: int = Field(default=100, ge=2, le=10_000)
    file_zip_max_depth: int = Field(default=8, ge=1, le=32)
    file_zip_max_seconds: float = Field(default=5.0, gt=0, le=30)
    # How long a paused tool approval can be decided, in seconds.
    approval_ttl_seconds: int = Field(default=900, ge=30, le=86_400)
    # Text changes. One file per call. Recovery bytes stay outside workspaces.
    file_write_max_bytes: int = Field(default=102_400, ge=1, le=1_048_576)
    file_delete_batch_limit: int = Field(default=1, ge=1, le=1)
    file_backup_max_bytes: int = Field(default=8_388_608, ge=4_096, le=67_108_864)
    file_backup_max_count: int = Field(default=40, ge=1, le=200)
    # SQLAlchemy URL. Override with DATABASE_URL. PostgreSQL can replace SQLite later.
    database_url: str = _DEFAULT_DATABASE_URL

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
