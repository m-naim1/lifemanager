from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LIFEMANAGER_")

    db_path: str = "lifemanager.db"
    host: str = "127.0.0.1"
    port: int = 8000
    jwt_secret: str = "dev-only-change-me"
    jwt_expire_minutes: int = 60 * 24 * 7
    allow_signup: bool = True
    admin_email: str = ""
    admin_password: str = ""

    # Database driver: "sqlite" (local aiosqlite) or "d1" (Cloudflare D1 via REST API).
    db_driver: str = "sqlite"
    # D1 settings (required when db_driver == "d1"). Passed into the container
    # by the Worker on startup, see wrangler.jsonc and worker/index.ts.
    d1_account_id: str = ""
    d1_database_id: str = ""
    d1_api_token: str = ""
    d1_api_base_url: str = "https://api.cloudflare.com/client/v4"
    # Apply backend/migrations/*.sql on startup when using the D1 driver.
    auto_migrate: bool = True
    migrations_dir: str = ""  # empty = backend/migrations next to the app package


settings = Settings()