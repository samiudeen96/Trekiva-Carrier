"""Database engine and session management.

A single synchronous SQLAlchemy layer is shared by FastAPI (run in its threadpool) and the
Celery workers, so business logic is written exactly once.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, MetaData, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings

log = logging.getLogger(__name__)

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_pool_size * 2,
    )


_session_factory: sessionmaker[Session] | None = None


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


def set_session_factory(factory: sessionmaker[Session] | None) -> None:
    """Override the session factory (used by tests)."""
    global _session_factory
    _session_factory = factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commits on success, rolls back on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session


# --- after-commit hooks -------------------------------------------------------------------------
# Work handed to Celery must only be enqueued once the data it depends on is committed; otherwise
# a fast worker could read the previous state. `on_commit` defers a callback until the session's
# transaction commits, and drops it if the transaction rolls back.

_AFTER_COMMIT = "trekiva_after_commit"


def on_commit(session: Session, callback: Callable[[], object]) -> None:
    session.info.setdefault(_AFTER_COMMIT, []).append(callback)


@event.listens_for(Session, "after_commit")
def _run_after_commit(session: Session) -> None:
    callbacks = session.info.pop(_AFTER_COMMIT, [])
    for callback in callbacks:
        try:
            callback()
        except Exception:
            log.exception("after-commit callback failed")


@event.listens_for(Session, "after_rollback")
def _discard_after_commit(session: Session) -> None:
    session.info.pop(_AFTER_COMMIT, None)
