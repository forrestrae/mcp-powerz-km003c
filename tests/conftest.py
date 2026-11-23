import pytest


@pytest.fixture(autouse=True)
def capture_dir(tmp_path_factory, monkeypatch):
    """Send capture files to a temporary directory, never to ./captures in the checkout."""
    path = tmp_path_factory.mktemp("captures")
    monkeypatch.setenv("KM003C_CAPTURE_DIR", str(path))
    return path
