from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    app_env: Literal["local", "test", "production"] = "local"
    api_bearer_token: SecretStr | None = None
    client_id: str = Field(default="poc-client", min_length=1, max_length=128)
    docs_enabled: bool = True
    database_url: SecretStr | None = None
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "spin"
    db_user: str = "spin_app"
    db_password: SecretStr | None = None
    db_auth_mode: str = "password"
    db_sslmode: str = "verify-full"
    db_sslrootcert: str | None = None
    db_pool_size: int = Field(default=2, ge=1, le=50)
    db_max_overflow: int = Field(default=0, ge=0, le=50)
    db_pool_timeout: float = Field(default=5, gt=0, le=30)
    aws_region: str = "us-west-2"
    provider_base_url: str = "http://localhost:8001"
    provider_allow_http: bool = True
    provider_connect_timeout: float = Field(default=1, gt=0, le=10)
    provider_timeout: float = Field(default=3, gt=0, le=30)
    max_body_bytes: int = Field(default=65536, ge=1024, le=1048576)
    max_request_concurrency: int = Field(default=64, ge=1, le=1000)

    @model_validator(mode="after")
    def safe_configuration(self):
        from urllib.parse import urlsplit

        provider = urlsplit(self.provider_base_url)
        if (
            provider.scheme not in {"http", "https"}
            or not provider.hostname
            or provider.username
            or provider.password
            or provider.query
            or provider.fragment
        ):
            raise ValueError(
                "PROVIDER_BASE_URL must be a configured HTTP(S) origin without credentials/query"
            )
        if provider.scheme == "http" and not self.provider_allow_http:
            raise ValueError("Plain HTTP provider requires explicit PROVIDER_ALLOW_HTTP=true")
        if self.db_auth_mode not in {"password", "iam"}:
            raise ValueError("DB_AUTH_MODE must be password or iam")
        if self.db_sslmode not in {"verify-full", "disable"}:
            raise ValueError("DB_SSLMODE must be verify-full or disable")
        if self.app_env == "production":
            if self.db_sslmode != "verify-full" or (
                self.database_url and self.database_url.get_secret_value().startswith("sqlite")
            ):
                raise ValueError("Production requires PostgreSQL with verified TLS")
        return self

    def sqlalchemy_url(self):
        if self.database_url:
            return self.database_url.get_secret_value()
        return URL.create(
            "postgresql+asyncpg",
            username=self.db_user,
            password=self.db_password.get_secret_value() if self.db_password else None,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )

    def validate_api_startup(self):
        # Migration jobs need database credentials, not an unrelated API bearer token.
        if self.app_env == "production" and (
            not self.api_bearer_token or len(self.api_bearer_token.get_secret_value()) < 32
        ):
            raise ValueError("Production API needs API_BEARER_TOKEN of at least 32 characters")
