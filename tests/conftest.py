from collections.abc import Iterator

import pytest
from sqlalchemy import Engine

from app.db.models import build_engine


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    eng = build_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    yield eng
    eng.dispose()
