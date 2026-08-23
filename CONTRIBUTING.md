# Contributing to datagraph

Thanks for your interest! Contributions of all kinds are welcome — extractors,
bug fixes, docs, and real-world feedback on the impact model.

## Getting started

```bash
git clone https://github.com/sumit-gupta03/datagraph
cd datagraph
python -m venv .venv && . .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
pytest
```

## Project principles

1. **The graph is deterministic.** Extractors parse real artifacts (AST,
   manifest.json, SQL, git diff). No LLM is ever involved in building nodes or
   edges — the AI layer only explains an already-computed analysis.
2. **Edges are semantic.** Every `EdgeType` declares which way impact flows
   (`IMPACT_DIRECTION` in `graph/model.py`). If you add an edge type, add its
   direction and a test proving propagation works both ways it should
   (and doesn't the way it shouldn't).
3. **Core stays light.** Heavy dependencies belong behind optional extras
   (`[sql]`, `[ai]`), guarded by lazy imports with a helpful error message.

## Adding an extractor

The most valuable contributions are new extractors (Airflow DAGs, AWS Lambda,
OpenLineage import, Looker, ...). The recipe:

1. Subclass `datagraph.extractors.base.Extractor` and implement
   `extract() -> ImpactGraph`.
2. Follow the node id conventions in the README (`file:...`, `func:...`,
   `dbt:...`, `table:...`) so fragments merge across extractors.
3. Emit `CONTAINS` edges from source files to the things defined in them —
   that's what makes `datagraph diff` map git changes to your nodes.
4. Add a pytest module with a small synthetic fixture (see
   `tests/test_dbt_extractor.py` for the pattern). No network, no real
   warehouse — tests must run offline.

## Pull requests

- One logical change per PR; include tests.
- `pytest` must pass on Python 3.9+ (CI runs Ubuntu and Windows).
- Keep public API changes (`datagraph/__init__.py`) noted in the PR
  description.

## Reporting bugs

Open an issue with: the artifact that was parsed (a minimal manifest.json /
snippet), the command you ran, expected vs. actual blast radius.
