import pytest

from demo.fake_hey import fake_server


@pytest.fixture
def server():
    with fake_server() as running:
        yield running
