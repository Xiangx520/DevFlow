from functools import lru_cache
from pathlib import Path
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEVFLOW_", env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://devflow:devflow@postgres/devflow"
    api_token: str = ""
    github_token: str = ""
    github_webhook_secret: str = ""
    github_api_url: str = "https://api.github.com"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = ""
    llm_thinking: Literal["enabled", "disabled"] | None = None
    public_url: str = "http://localhost:8000"
    internal_url: str = "http://api:8000"
    feishu_webhook: str = ""
    feishu_api_url: str = "https://open.feishu.cn/open-apis"
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    data_dir: Path = Path("/data")
    runner_image: str = "devflow-runner:local"
    runner_network: str = "devflow_execution"
    max_concurrency: int = 2
    lease_seconds: int = 90
    poll_seconds: float = 2
    webhook_poll_seconds: int = 60

@lru_cache
def settings() -> Settings:
    return Settings()
