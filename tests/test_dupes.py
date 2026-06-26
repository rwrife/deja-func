"""Tests for `deja dupes`: clustering (dupes.py), serialization, rendering, CLI.

The redundancy report (PLAN.md §8 #1): group near-identical functions so you can
see "you have 6 date parsers." Fixtures bake in intentional duplicates.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deja.cli import main
from deja.dupes import (
    DEFAULT_THRESHOLD,
    Cluster,
    find_clusters,
    pair_score,
)
from deja.parsers import FunctionRecord
from deja.render import format_clusters
from deja.serialize import clusters_to_dict


def _rec(
    name: str,
    *,
    doc: str = "",
    qualname: str = "",
    file: str = "m.py",
    line: int = 1,
    sig: str = "()",
):
    return FunctionRecord(
        name=name,
        file=file,
        line=line,
        signature=sig,
        docstring=doc,
        lang="python",
        qualname=qualname or name,
    )


# -- pairwise scoring ------------------------------------------------------


def test_pair_score_high_for_near_identical_twins() -> None:
    a = _rec("parse_iso_date", doc="Parse an ISO date string.", sig="(s: str)")
    b = _rec("parse_date_iso", doc="Parse an ISO date string into a date.", sig="(text: str)")
    assert pair_score(a, b) >= DEFAULT_THRESHOLD


def test_pair_score_low_for_unrelated_functions() -> None:
    a = _rec("slugify", doc="Make a URL-safe slug.", sig="(text: str) -> str")
    b = _rec("compute_tax", doc="Compute sales tax for an order.", sig="(amount: float) -> float")
    assert pair_score(a, b) < DEFAULT_THRESHOLD


def test_pair_score_is_symmetric() -> None:
    a = _rec("send_email", doc="Send an email message.", sig="(to: str) -> bool")
    b = _rec("email_send", doc="Send an email to a recipient.", sig="(addr: str) -> bool")
    assert pair_score(a, b) == pair_score(b, a)


def test_pair_score_in_range() -> None:
    a = _rec("a", doc="x", sig="(s: str)")
    b = _rec("b", doc="y", sig="(t: int)")
    assert 0.0 <= pair_score(a, b) <= 100.0


def test_pair_score_without_docstrings_relies_on_name() -> None:
    # No docstrings on either side: identical names should still score very high.
    a = _rec("slugify", line=1)
    b = _rec("slugify", line=2, file="other.py")
    assert pair_score(a, b) >= DEFAULT_THRESHOLD


# -- clustering ------------------------------------------------------------


def _dupe_records() -> list[FunctionRecord]:
    """A fixture inventory with one intentional 3-function dupe cluster."""
    return [
        _rec("parse_iso_date", doc="Parse an ISO 8601 date string.", sig="(s: str)", line=1),
        _rec(
            "parse_date_iso",
            doc="Parse an ISO 8601 date string into a date object.",
            sig="(text: str)",
            line=10,
        ),
        _rec("parse_iso", doc="Parse an ISO date string.", sig="(value: str)", line=20),
        _rec("add", doc="Add two integers.", sig="(a: int, b: int) -> int", line=30),
        _rec("slugify", doc="Make a URL-safe slug.", sig="(text: str) -> str", line=40),
    ]


def test_find_clusters_groups_the_duplicates() -> None:
    clusters = find_clusters(_dupe_records())
    assert len(clusters) == 1
    names = {m.name for m in clusters[0].members}
    assert names == {"parse_iso_date", "parse_date_iso", "parse_iso"}


def test_find_clusters_excludes_singletons() -> None:
    clusters = find_clusters(_dupe_records())
    all_members = {m.name for c in clusters for m in c.members}
    # The lone, unrelated functions never appear in any cluster.
    assert "add" not in all_members
    assert "slugify" not in all_members


def test_find_clusters_sorted_by_size_desc() -> None:
    records = [
        # 3-member cluster (date parsers)
        _rec("parse_iso_date", doc="Parse an ISO date.", sig="(s: str)", line=1),
        _rec("parse_date_iso", doc="Parse an ISO date value.", sig="(t: str)", line=2),
        _rec("parse_iso", doc="Parse an ISO date string.", sig="(v: str)", line=3),
        # 2-member cluster (email senders)
        _rec("send_email", doc="Send an email message.", sig="(to: str) -> bool", line=4),
        _rec("email_send", doc="Send an email message now.", sig="(to: str) -> bool", line=5),
    ]
    clusters = find_clusters(records)
    assert len(clusters) == 2
    assert [c.size for c in clusters] == [3, 2]


def test_threshold_controls_sensitivity() -> None:
    records = _dupe_records()
    # A strict threshold dissolves the *looser* links (the 3-way cluster shrinks
    # or vanishes); a permissive one keeps the obvious cluster intact.
    strict = find_clusters(records, threshold=99.5)
    loose = find_clusters(records, threshold=60.0)
    # Loose catches the full 3-parser pile...
    assert loose and loose[0].size >= 3
    # ...while strict keeps at most the single tightest pair (never all three),
    # proving the knob actually tightens grouping.
    strict_max = max((c.size for c in strict), default=0)
    assert strict_max < 3


def test_find_clusters_complete_linkage_resists_chaining() -> None:
    # A≈B and B≈C clear the threshold, but A≉C falls below it. Single-linkage
    # would chain all three; complete-linkage must NOT — the far member stays out.
    a = _rec(
        "send_email_message",
        doc="Send an email message to a recipient.",
        sig="(to: str) -> bool",
        line=1,
    )
    b = _rec("send_email", doc="Send an email message.", sig="(to: str) -> bool", line=2)
    c = _rec("send_sms", doc="Send an sms message.", sig="(to: str) -> bool", line=3)
    # Sanity-check the intended score relationships hold for this fixture.
    assert pair_score(a, b) >= 75.0
    assert pair_score(b, c) >= 75.0
    assert pair_score(a, c) < 75.0

    clusters = find_clusters([a, b, c], threshold=75.0)
    assert len(clusters) == 1
    names = {m.name for m in clusters[0].members}
    # The tight pair clusters; the far member (send_sms) is excluded.
    assert names == {"send_email_message", "send_email"}


def test_find_clusters_unites_fully_mutual_members() -> None:
    # When every pair clears the threshold, all members belong together.
    records = [
        _rec("alpha_one", doc="Handle alpha one task.", sig="(s: str)", line=1),
        _rec("alpha_two", doc="Handle alpha two task.", sig="(s: str)", line=2),
        _rec("alpha_three", doc="Handle alpha three task.", sig="(s: str)", line=3),
    ]
    clusters = find_clusters(records, threshold=70.0)
    assert clusters
    assert clusters[0].size == 3


def test_find_clusters_empty_and_singleton_inputs() -> None:
    assert find_clusters([]) == []
    assert find_clusters([_rec("solo")]) == []


def test_cluster_members_sorted_by_location() -> None:
    clusters = find_clusters(_dupe_records())
    members = clusters[0].members
    locs = [(m.file, m.line) for m in members]
    assert locs == sorted(locs)


def test_cluster_score_reflects_tightness() -> None:
    clusters = find_clusters(_dupe_records())
    assert clusters
    assert 0.0 <= clusters[0].score <= 100.0
    # The date-parser cluster is tight; score should clear the default cutoff.
    assert clusters[0].score >= DEFAULT_THRESHOLD


# -- serialization ---------------------------------------------------------


def test_clusters_to_dict_stable_shape() -> None:
    clusters = find_clusters(_dupe_records())
    doc = clusters_to_dict(clusters, threshold=DEFAULT_THRESHOLD)
    assert doc["schema_version"] == 1
    assert doc["threshold"] == DEFAULT_THRESHOLD
    assert doc["count"] == 1
    cluster = doc["clusters"][0]
    assert cluster["size"] == 3
    assert isinstance(cluster["score"], float)
    member = cluster["members"][0]
    # Bare record shape: location + signature, no search-only score/breakdown.
    assert set(member) == {"name", "qualname", "file", "line", "signature", "docstring", "lang"}
    assert "score" not in member
    assert "breakdown" not in member


def test_clusters_to_dict_empty() -> None:
    doc = clusters_to_dict([], threshold=80.0)
    assert doc["count"] == 0
    assert doc["clusters"] == []
    assert doc["threshold"] == 80.0


# -- rendering -------------------------------------------------------------


def test_format_clusters_no_color_lists_members() -> None:
    clusters = [
        Cluster(
            members=(
                _rec("parse_iso_date", doc="Parse a date.", file="d.py", line=1, sig="(s: str)"),
                _rec("parse_date_iso", doc="Parse a date.", file="d.py", line=9, sig="(t: str)"),
            ),
            score=88.0,
        )
    ]
    out = format_clusters(clusters, color=False)
    assert "parse_iso_date" in out
    assert "d.py:1" in out
    assert "d.py:9" in out
    assert "×2" in out
    assert "\033[" not in out  # no ANSI when color is off


def test_format_clusters_empty_is_friendly() -> None:
    out = format_clusters([], color=False)
    assert out  # non-empty header even with zero clusters
    assert "lean" in out.lower()


# -- CLI -------------------------------------------------------------------


def _dupe_repo(root: Path) -> None:
    (root / "dates.py").write_text(
        '''
def parse_iso_date(s: str):
    """Parse an ISO 8601 date string into a date."""
    ...


def parse_date_iso(text: str):
    """Parse an ISO 8601 date string into a date object."""
    ...


def parse_iso(value: str):
    """Parse an ISO date string."""
    ...


def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b
''',
    )


def test_cli_dupes_builds_index_and_reports(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _dupe_repo(tmp_path)
    rc = main(["dupes", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "parse_iso_date" in captured.out
    assert "dates.py:" in captured.out
    # index was auto-built on first run
    assert (tmp_path / ".dejafunc" / "index.json").is_file()


def test_cli_dupes_clean_repo_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    (tmp_path / "lean.py").write_text(
        '''
def slugify(text: str) -> str:
    """Make a URL-safe slug."""
    return text


def compute_tax(amount: float) -> float:
    """Compute sales tax for an order total."""
    return amount * 0.1
''',
    )
    rc = main(["dupes", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "lean" in captured.out.lower()


def test_cli_dupes_missing_dir_exits_2(capsys: pytest.CaptureFixture) -> None:
    rc = main(["dupes", "/no/such/path/deja"])
    assert rc == 2


def test_cli_dupes_threshold_flag_can_suppress(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _dupe_repo(tmp_path)
    # An impossibly strict threshold should report nothing.
    rc = main(["dupes", str(tmp_path), "--threshold", "100"])
    assert rc == 1


def test_cli_dupes_bad_threshold_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _dupe_repo(tmp_path)
    rc = main(["dupes", str(tmp_path), "--threshold", "150"])
    assert rc == 2
    assert "threshold" in capsys.readouterr().err


def test_cli_dupes_limit_caps_clusters(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    (tmp_path / "many.py").write_text(
        '''
def parse_iso_date(s: str):
    """Parse an ISO date string."""
    ...


def parse_date_iso(t: str):
    """Parse an ISO date value."""
    ...


def send_email(to: str) -> bool:
    """Send an email message."""
    ...


def email_send(to: str) -> bool:
    """Send an email message now."""
    ...
''',
    )
    rc = main(["dupes", str(tmp_path), "--json", "--limit", "1"])
    captured = capsys.readouterr()
    assert rc == 0
    doc = json.loads(captured.out)
    assert doc["count"] == 1


def test_cli_dupes_json_emits_stable_document(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _dupe_repo(tmp_path)
    rc = main(["dupes", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    doc = json.loads(captured.out)
    assert doc["schema_version"] == 1
    assert doc["count"] >= 1
    assert doc["clusters"][0]["size"] >= 2
    member = doc["clusters"][0]["members"][0]
    assert member["file"].endswith("dates.py")


def test_cli_dupes_json_clean_repo_empty_and_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    (tmp_path / "lean.py").write_text(
        '''
def only_one(x: int) -> int:
    """The one and only function here."""
    return x
''',
    )
    rc = main(["dupes", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert rc == 1
    doc = json.loads(captured.out)
    assert doc["count"] == 0
    assert doc["clusters"] == []
