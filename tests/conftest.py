import pytest
from galleryedge_ops import db

PW = "FAKE-test-passphrase-123"


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c
