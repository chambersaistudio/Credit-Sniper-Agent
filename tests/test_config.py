import pytest

from app.config import normalize_database_url


@pytest.mark.parametrize("raw, expected", [
    ("postgres://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
    ("postgresql://u:p@h.neon.tech/db?sslmode=require&channel_binding=require",
     "postgresql+asyncpg://u:p@h.neon.tech/db?ssl=require"),
    ("postgresql+asyncpg://u:p@localhost/db", "postgresql+asyncpg://u:p@localhost/db"),
])
def test_database_url_normalization(raw, expected):
    assert normalize_database_url(raw) == expected
