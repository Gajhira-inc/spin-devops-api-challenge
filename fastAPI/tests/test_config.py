import pytest
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app


def test_production_requires_token_and_verified_postgres():
    with pytest.raises(ValueError):
        create_app(Settings(_env_file=None, app_env="production"))
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None, app_env="production", api_bearer_token="x" * 32, db_sslmode="disable"
        )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env="production",
            api_bearer_token="x" * 32,
            database_url="sqlite+aiosqlite:///test.db",
        )


def test_provider_origin_is_configuration_not_user_input():
    for url in [
        "file:///etc/passwd",
        "http://user:password@localhost",
        "http://provider?injected=true",
    ]:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, provider_base_url=url)
