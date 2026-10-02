"""Test configuration.

Tests run against a real PostgreSQL database (partial unique indexes, ON CONFLICT, row locks and
JSONB are part of what is being tested). The schema is built with the Alembic migrations, and
each test runs inside a transaction that is rolled back afterwards.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from cryptography.fernet import Fernet


def _test_database_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    base = os.environ.get(
        "DATABASE_URL", "postgresql+psycopg://trekiva:trekiva@localhost:5432/trekiva"
    )
    prefix, _, _ = base.rpartition("/")
    return f"{prefix}/trekiva_test"


# Must be set before any app module reads settings.
os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": _test_database_url(),
        "ENCRYPTION_KEYS": Fernet.generate_key().decode(),
        "SHOPIFY_CLIENT_ID": "test-client-id",
        "SHOPIFY_CLIENT_SECRET": "test-client-secret-0123456789abcdef",
        "SHOPIFY_SHOP_ALLOWLIST": "",
        "ALLOW_MOCK_CARRIERS": "true",
        "LOG_JSON": "false",
        "REDIS_URL": "redis://127.0.0.1:1/0",  # unreachable on purpose: tests never need Redis
    }
)
os.environ.pop("DEV_AUTH_BYPASS_SHOP", None)

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.core import db as core_db  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.crypto import get_encryptor  # noqa: E402
from app.models import Shop  # noqa: E402
from app.schemas.settings import ShopSettings  # noqa: E402
from tests.harness import TaskQueue  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parents[1]
SHOP_DOMAIN = "trekiva-test.myshopify.com"


def _ensure_database(url: str) -> None:
    parsed = make_url(url)
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": parsed.database}
        )
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    admin.dispose()


@pytest.fixture(scope="session")
def engine():  # type: ignore[no-untyped-def]
    from alembic import command
    from alembic.config import Config

    url = get_settings().database_url
    _ensure_database(url)
    engine = core_db.get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine) -> Iterator[Session]:  # type: ignore[no-untyped-def]
    """A session whose commits are savepoints inside one outer transaction (rolled back)."""
    connection = engine.connect()
    outer = connection.begin()
    factory = sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    core_db.set_session_factory(factory)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        core_db.set_session_factory(None)
        outer.rollback()
        connection.close()


@pytest.fixture
def shop(db: Session) -> Shop:
    shop = Shop(
        shop_domain=SHOP_DOMAIN,
        access_token_enc=get_encryptor().encrypt("shpat_test_token"),
        scopes="read_orders",
        automation_enabled=True,
        settings=ShopSettings().model_dump(mode="json"),
    )
    db.add(shop)
    db.flush()
    return shop


@pytest.fixture(autouse=True)
def task_queue(monkeypatch: pytest.MonkeyPatch) -> TaskQueue:
    queue = TaskQueue()
    monkeypatch.setattr("app.workers.enqueue.enqueue", queue)
    return queue
