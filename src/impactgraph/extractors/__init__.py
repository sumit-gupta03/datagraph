from .base import Extractor
from .python_extractor import PythonExtractor
from .dbt_extractor import DbtExtractor
from .git_extractor import ChangeSet, changed_node_ids, collect_changes

__all__ = [
    "Extractor",
    "PythonExtractor",
    "DbtExtractor",
    "ChangeSet",
    "changed_node_ids",
    "collect_changes",
]

try:  # sqlglot is an optional extra
    from .sql_extractor import SqlExtractor  # noqa: F401

    __all__.append("SqlExtractor")
except ImportError:  # pragma: no cover
    pass
