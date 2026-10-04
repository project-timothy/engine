# evals: the red tree

Evals here FAIL by design. Each one is a decision the owner merged before
anything was built: a proposal PR (phase 7 row 7.18) carries a design note
under `docs/proposals/` and exactly one eval under `evals/proposals/`, and
merging them is how the owner says "build this".

This tree is deliberately OUTSIDE pytest's `testpaths` (`pyproject.toml`
names `tests`, `core`, `auditor`), so a red eval here never turns the suite
red. Run one by naming it:

```
uv run pytest evals/proposals/test_<slug>.py
```

The build lane's job is to make it pass and then move it where CI will keep
it honest: `tests/` for a unit test, `core/**/evals/` or `auditor/evals/`
for an incident or agent regression. An eval that goes green and stays here
is not protecting anything, so it does not stay here.

Nothing under this tree is imported by the engine, the auditor, or the
tenants.
