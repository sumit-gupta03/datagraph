import shutil
import subprocess
import textwrap

import pytest

from datagraph import PythonExtractor, changed_node_ids, collect_changes

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "db.py").write_text(
        textwrap.dedent(
            """
            def load_customers():
                return [1, 2, 3]

            def load_orders():
                return []
            """
        ),
        encoding="utf-8",
    )
    (repo / "api.py").write_text(
        textwrap.dedent(
            """
            import db

            def customers_endpoint():
                return db.load_customers()
            """
        ),
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def test_uncommitted_change_maps_to_function(git_repo):
    graph = PythonExtractor(git_repo).extract()
    # modify only load_customers' body
    text = (git_repo / "db.py").read_text(encoding="utf-8")
    (git_repo / "db.py").write_text(
        text.replace("return [1, 2, 3]", "return [1, 2, 3, 4]"), encoding="utf-8"
    )
    changes = collect_changes(git_repo)
    assert "db.py" in changes.files
    ids = changed_node_ids(graph, changes)
    assert "file:db.py" in ids
    assert "func:db.py::load_customers" in ids
    # the untouched function should not be flagged as changed
    assert "func:db.py::load_orders" not in ids


def test_impact_of_diff(git_repo):
    graph = PythonExtractor(git_repo).extract()
    text = (git_repo / "db.py").read_text(encoding="utf-8")
    (git_repo / "db.py").write_text(
        text.replace("return [1, 2, 3]", "return []"), encoding="utf-8"
    )
    changes = collect_changes(git_repo)
    ids = changed_node_ids(graph, changes)
    affected = graph.impact(ids)
    assert "func:api.py::customers_endpoint" in affected
