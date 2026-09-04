"""Tests for the PyYAML `on:` key parsing fix.

PyYAML (YAML 1.1) treats bare `on` as the boolean True.  The fix in
_parse_workflow_yaml uses `doc.get("on") or doc.get(True, {})` to handle both
quoted and unquoted `on:` keys.
"""

import sys
from pathlib import Path

# Make the src package importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from cicd_inventory.providers.github import GitHubProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_provider = GitHubProvider(token="test-token")  # __init__ only builds headers; no network


def _parse(yaml_text: str) -> list[str]:
    """Parse a YAML snippet and return the extracted on_triggers list."""
    record = _provider._parse_workflow_yaml(yaml_text, org="test-org", repo="test-repo", filename="test.yml")
    assert record is not None, "Expected a WorkflowRecord, got None"
    return record.on_triggers


# ---------------------------------------------------------------------------
# Trigger extraction cases
# ---------------------------------------------------------------------------

CASES: list[tuple[str, list[str]]] = [
    # 1 – bare mapping (most common form in real workflows)
    (
        "on:\n  push:\n  pull_request:\nname: Test\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n",
        ["push", "pull_request"],
    ),
    # 2 – bare list form
    (
        "on: [push, pull_request]\nname: Test\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n",
        ["push", "pull_request"],
    ),
    # 3 – bare scalar (single trigger)
    (
        "on: push\nname: Test\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n",
        ["push"],
    ),
    # 4 – quoted key (already worked before the fix; must keep working)
    (
        '"on":\n  push:\n  schedule:\n    - cron: "0 0 * * *"\n'
        "name: Test\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n",
        ["push", "schedule"],
    ),
    # 5 – workflow_dispatch only
    (
        "on: workflow_dispatch\nname: Test\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n",
        ["workflow_dispatch"],
    ),
    # 6 – push with branch filter (sub-keys must be ignored; only event name returned)
    (
        "on:\n  push:\n    branches: [main, develop]\n"
        "  pull_request:\n    types: [opened, synchronize]\n"
        "name: Test\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n",
        ["push", "pull_request"],
    ),
    # 7 – release trigger
    (
        "on:\n  release:\n    types: [published]\n"
        "name: Test\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n",
        ["release"],
    ),
    # 8 – schedule + workflow_dispatch (common combination)
    (
        "on:\n  schedule:\n    - cron: '0 6 * * 1'\n  workflow_dispatch:\n"
        "name: Test\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n",
        ["schedule", "workflow_dispatch"],
    ),
]


@pytest.mark.parametrize(
    "yaml_text,expected",
    CASES,
    ids=[
        "bare-mapping",
        "bare-list",
        "bare-scalar",
        "quoted-key",
        "workflow_dispatch",
        "push-with-filters",
        "release",
        "schedule-and-dispatch",
    ],
)
def test_trigger_parsing(yaml_text: str, expected: list[str]) -> None:
    assert _parse(yaml_text) == expected
