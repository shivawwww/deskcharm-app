import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from copilot.audit import AuditLog  # noqa: E402
from copilot.cad import MockCAD  # noqa: E402
from copilot.orchestrator import Orchestrator  # noqa: E402


@pytest.fixture
def orch(tmp_path):
    return Orchestrator(AuditLog(tmp_path / "audit.jsonl"), MockCAD(tmp_path / "cad"), archive_dir=tmp_path / "calls")
