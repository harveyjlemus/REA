import os
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "examples"))
os.environ["FEF_NO_DOTENV"] = "1"
for key in ("SERPER_API_KEY", "MILLIONVERIFIER_API_KEY", "GETLEADS_API_KEY"):
    os.environ.pop(key, None)

import pytest  # noqa: E402

from lib.config import load_config  # noqa: E402


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Keep the cross-run domain memory out of the real repo during tests."""
    monkeypatch.setenv("FEF_CACHE_DIR", str(tmp_path / "fef-cache"))
