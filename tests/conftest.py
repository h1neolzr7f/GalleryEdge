import pytest
from galleryedge_ops import db

PW = "FAKE-test-passphrase-123"


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


import os
import shutil
import socket
import subprocess
import time
import urllib.request


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture(scope="session")
def libsql_url(tmp_path_factory):
    """A real libSQL server: $GE_TEST_LIBSQL_URL (CI service) or a throwaway local sqld."""
    url = os.environ.get("GE_TEST_LIBSQL_URL")
    if url:
        yield url
        return
    binp = os.environ.get("SQLD_BIN") or shutil.which("sqld") or os.path.expanduser("~/.turso/sqld")
    if not os.path.exists(binp):
        pytest.skip("no sqld available")
    port = _free_port()
    d = tmp_path_factory.mktemp("sqld")
    proc = subprocess.Popen([binp, "--http-listen-addr", f"127.0.0.1:{port}", "--db-path", str(d / "db")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/health", timeout=0.5); break
        except Exception:
            time.sleep(0.1)
    yield url
    proc.terminate(); proc.wait(5)


@pytest.fixture
def libsql(libsql_url):
    from galleryedge_db.client import HranaClient
    c = HranaClient(libsql_url)
    for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%fts_%' AND name NOT LIKE '_litestream%'").rows:
        c.execute(f'DROP TABLE IF EXISTS "{name}"')
    c.execute("DROP TABLE IF EXISTS works_fts")
    return c
