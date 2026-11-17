"""Central, environment-driven configuration.

Every setting can be overridden with an environment variable of the same name
(case-insensitive), or via a `.env` file. OpenTelemetry exporters additionally
honour the standard `OTEL_*` variables (OTEL_EXPORTER_OTLP_ENDPOINT,
OTEL_TRACES_SAMPLER, OTEL_RESOURCE_ATTRIBUTES, OTEL_SDK_DISABLED, ...).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- General -----------------------------------------------------------
    deployment_environment: str = Field("dev", description="dev | staging | prod")
    service_version: str = "1.0.0"

    # --- Logging -----------------------------------------------------------
    log_level: str = "INFO"
    log_format: str = Field("json", description="json (production) | console (local dev)")
    # MCP request/response payload logging. Disable in environments where
    # prompts/tool output may contain PII you must not persist.
    log_payloads: bool = True
    log_payload_max_chars: int = Field(2000, ge=100)

    # --- Telemetry export ---------------------------------------------------
    otel_enabled: bool = True
    otel_metric_export_interval_ms: int = Field(15000, ge=1000)

    # --- LLM ---------------------------------------------------------------
    llm_provider: str = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2:latest"
    llm_temperature: float = 0.0
    llm_timeout_s: float = 120.0
    # Optional pricing so cost is tracked even for hosted models later.
    # Leave at 0 for self-hosted Ollama.
    llm_cost_per_1k_input_tokens: float = 0.0
    llm_cost_per_1k_output_tokens: float = 0.0

    # --- Agent -------------------------------------------------------------
    agent_host: str = "0.0.0.0"
    agent_port: int = 8080
    agent_recursion_limit: int = Field(12, ge=2, description="Max graph super-steps per request")
    agent_request_timeout_s: float = 300.0

    # --- MCP (client side) --------------------------------------------------
    mcp_server_url: str = "http://localhost:8000/mcp"
    mcp_server_name: str = "stock-tools"
    mcp_auth_token: SecretStr | None = None
    mcp_tool_timeout_s: float = 60.0
    mcp_connect_retries: int = Field(2, ge=0, le=5)

    # --- MCP (server side) --------------------------------------------------
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000
    mcp_allowed_hosts: list[str] = ["localhost:*", "127.0.0.1:*", "mcp-server:*"]
    data_dir: Path = Path("data")
    charts_dir: Path = Path("charts")
    python_exec_timeout_s: float = 30.0
    python_exec_max_output_chars: int = 20000


@lru_cache
def get_settings() -> Settings:
    return Settings()
