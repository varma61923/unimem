"""The architecture gate rejects missing extraction, even under surface budgets."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.graphify_audit import audit

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "graphify_audit.py"


def node(identity, filename="unimem/cli.py"):
    return {"id": identity, "source_file": filename, "_callable": True}


@pytest.mark.parametrize(
    "graph",
    [
        {"nodes": [], "links": []},
        {"nodes": [node("root"), node("root")], "links": []},
        {"nodes": [node("helper", "unimem/helper.py")], "links": []},
    ],
)
def test_incomplete_extraction_cannot_pass_integrity(graph, tmp_path):
    source = tmp_path / "graph.json"
    output = tmp_path / "audit.json"
    source.write_text(json.dumps(graph), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--graph", str(source), "--output", str(output), "--max-unreachable", "318"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert json.loads(output.read_text())["integrity_violations"]


def test_reachability_and_dangling_edges_are_preserved():
    result = audit(
        {
            "nodes": [node("root"), node("used", "unimem/helper.py"), node("candidate", "unimem/optional.py")],
            "edges": [{"source": "root", "target": "used"}, {"source": "used", "target": "absent"}],
        }
    )
    assert result["missing_endpoints"] == [["used", "absent"]]
    assert [row["id"] for row in result["possible_unreachable"]] == ["candidate"]
    assert not result["integrity_violations"]


def test_missing_source_tree_is_a_policy_failure(tmp_path):
    source = tmp_path / "graph.json"
    output = tmp_path / "audit.json"
    source.write_text(json.dumps({"nodes": [node("root")], "links": []}), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--graph",
            str(source),
            "--output",
            str(output),
            "--source-root",
            str(tmp_path),
            "--max-modules",
            "398",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert json.loads(output.read_text())["policy_violations"]
