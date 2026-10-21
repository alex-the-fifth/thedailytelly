"""Run the isolated HTTP regression suite: python server/tests/smoke.py."""
from pathlib import Path
import pytest

if __name__ == "__main__":
    raise SystemExit(pytest.main([str(Path(__file__).with_name("test_platform.py")), "-q"]))
