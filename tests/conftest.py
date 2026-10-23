import pytest

from agent import baseline as baseline_mod
from agent.ledger import Ledger
from agent.tools import Toolbox
from tests import fakes


@pytest.fixture
def world_backends():
    return fakes.backends()


@pytest.fixture
def base(world_backends):
    b, _ = world_backends
    return baseline_mod.compute(b, end_offset_min=10)


@pytest.fixture
def tb(world_backends, base):
    b, _ = world_backends
    return Toolbox(b, Ledger(), base)
