"""Tests for `runs-on:` normalisation and per-workflow parse resilience.

GitHub allows a mapping form of `runs-on:` for runner groups, which is neither a
string nor a list.  Hitting one used to raise a pydantic ValidationError that
aborted the entire extraction run.
"""

import sys
from pathlib import Path

# Make the src package importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from cicd_inventory.providers.github import GitHubProvider

_HEADER = "on: push\nname: Test\njobs:\n  build:\n"


def _runs_on(job_body: str) -> str | list[str]:
    """Parse a single-job workflow and return the job's normalised runs_on."""
    provider = GitHubProvider(token="test-token")
    record = provider._parse_workflow_yaml(
        _HEADER + job_body + "    steps: []\n",
        org="test-org",
        repo="test-repo",
        filename="test.yml",
    )
    assert record is not None, "Expected a WorkflowRecord, got None"
    assert provider.parse_errors == []
    return record.jobs[0].runs_on


CASES: list[tuple[str, str | list[str]]] = [
    # 1 – the regression: runner group mapping with a scalar label
    ("    runs-on:\n      group: cvmfs-publish\n      labels: cvmfs\n", ["group:cvmfs-publish", "cvmfs"]),
    # 2 – runner group mapping with a list of labels
    (
        "    runs-on:\n      group: cvmfs-publish\n      labels: [cvmfs, self-hosted]\n",
        ["group:cvmfs-publish", "cvmfs", "self-hosted"],
    ),
    # 3 – group only
    ("    runs-on:\n      group: cvmfs-publish\n", ["group:cvmfs-publish"]),
    # 4 – labels only
    ("    runs-on:\n      labels: [self-hosted, linux]\n", ["self-hosted", "linux"]),
    # 5 – empty mapping falls back to the unknown sentinel
    ("    runs-on: {}\n", "unknown"),
    # 6 – plain string (the overwhelmingly common form) is untouched
    ("    runs-on: ubuntu-latest\n", "ubuntu-latest"),
    # 7 – list form is untouched
    ("    runs-on: [self-hosted, cvmfs]\n", ["self-hosted", "cvmfs"]),
    # 8 – explicit null, e.g. a placeholder value
    ("    runs-on:\n", "unknown"),
    # 9 – key absent entirely (reusable-workflow style job)
    ("", "unknown"),
]


@pytest.mark.parametrize(
    "job_body,expected",
    CASES,
    ids=[
        "group-scalar-label",
        "group-list-labels",
        "group-only",
        "labels-only",
        "empty-mapping",
        "plain-string",
        "list-form",
        "null-value",
        "missing-key",
    ],
)
def test_runs_on_normalisation(job_body: str, expected: str | list[str]) -> None:
    assert _runs_on(job_body) == expected


def test_unparseable_workflow_is_skipped_not_fatal() -> None:
    """A workflow the model rejects is recorded and skipped, not raised."""
    provider = GitHubProvider(token="test-token")
    record = provider._parse_workflow_yaml(
        "on: push\nname: Test\njobs:\n  build:\n    name: {a: 1}\n    runs-on: ubuntu-latest\n    steps: []\n",
        org="test-org",
        repo="test-repo",
        filename="broken.yml",
    )
    assert record is None
    assert len(provider.parse_errors) == 1
    assert "test-org/test-repo/broken.yml" in provider.parse_errors[0]


def test_non_mapping_document_is_skipped_not_fatal() -> None:
    """Valid YAML that is not a mapping must not raise either."""
    provider = GitHubProvider(token="test-token")
    assert provider._parse_workflow_yaml("- just\n- a list\n", org="o", repo="r", filename="list.yml") is None
    assert len(provider.parse_errors) == 1


def test_non_yamlerror_from_safe_load_is_recorded() -> None:
    """PyYAML raises a bare ValueError on an out-of-range date, not a YAMLError."""
    provider = GitHubProvider(token="test-token")
    text = "on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    env:\n      CUTOFF: 2024-02-30\n    steps: []\n"
    assert provider._parse_workflow_yaml(text, org="o", repo="r", filename="date.yml") is None
    assert len(provider.parse_errors) == 1
    assert "ValueError" in provider.parse_errors[0]


def test_malformed_yaml_is_recorded() -> None:
    provider = GitHubProvider(token="test-token")
    assert provider._parse_workflow_yaml("on: [push\n", org="o", repo="r", filename="bad.yml") is None
    assert len(provider.parse_errors) == 1
