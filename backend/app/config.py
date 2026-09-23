from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "backend/.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Helpdesk"
    secret_key: str = "dev-insecure-secret-change-me"
    access_token_expire_minutes: int = 1440
    database_url: str = "sqlite:///./data/helpdesk.db"
    cors_origins: str = "*"

    bootstrap_admin_email: str = "admin@helpdesk.local"
    # пустой пароль означает «не создавать bootstrap-админа автоматически».
    # Так развернутая копия не поднимается с известным паролем по умолчанию.
    bootstrap_admin_password: str = ""
    bootstrap_admin_name: str = "Administrator"

    public_base_url: str = "http://localhost:8091"

    # WhatsApp
    whatsapp_base_url: str = ""
    whatsapp_token: str = ""
    whatsapp_send_path: str = "/sendMessage"

    # VK
    vk_group_id: str = ""
    vk_access_token: str = ""
    vk_confirmation_code: str = ""
    vk_secret: str = ""
    vk_api_version: str = "5.199"

    # VK OAuth (Authorization Code Flow). The app secret lives only here,
    # never in the browser. redirect_uri must be registered on dev.vk.com;
    # empty value means "build it from public_base_url".
    vk_app_id: str = "54786450"
    vk_client_secret: str = ""
    vk_oauth_redirect_uri: str = ""
    vk_oauth_frontend_url: str = "/app/"
    # service token: only for public lookups like resolving a
    # community short name to its numeric id. Never sent to clients.
    vk_service_token: str = ""

    # Email
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_ssl: bool = True

    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_use_ssl: bool = True
    imap_poll_seconds: int = 60

    @property
    def cors_origin_list(self) -> List[str]:
        raw = (self.cors_origins or "").strip()
        if not raw or raw == "*":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
