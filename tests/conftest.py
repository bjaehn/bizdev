from datetime import date, timedelta
from pathlib import Path

import pytest

from bd.config import load_config
from bd.models import ScoringSignal

AS_OF = date(2026, 10, 15)
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def cfg():
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def as_of():
    return AS_OF


def sig(type_, age, conf="High", lines=("Strategy & Operations",), as_of=AS_OF):
    return ScoringSignal(type=type_, event_date=as_of - timedelta(days=age), confidence=conf,
                         service_lines=tuple(lines))
