import pytest

from .state_support import release_owners


@pytest.fixture(autouse=True)
def _release_executor_owners():
    yield
    release_owners()
