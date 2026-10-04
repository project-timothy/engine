# Incident template

Copy this for every new incident. The shape matches the lessons corpus so the
incident-to-eval converter (`core/learning/incident_to_eval.py`) can parse it.
Hard rule 8: the eval lands before the fix merges. The fix PR includes the new
eval (failing or skipped-until-the-module-exists) and the code that makes it
pass.

```markdown
## YYYY-MM-DD — One-line title

**What happened.** The concrete event. Dates, amounts, file names, what the
system did versus what it should have done.

**Root cause.** Why it happened, one level below the symptom.

**Bad pattern.** The general anti-pattern to regress against. This is what the
eval asserts no longer occurs. State it so it reads independently of this one
incident.

**Fix.** What changed. Name the engine module or tenant rule.

**Enforcement.** Where the guardrail lives now: an eval id, a code path, a
tenant rule file. An incident with no enforcement location is not done.
```

## Turning an incident into an eval

1. Write the incident entry above.
2. Run the converter to scaffold a skipped stub, or hand-author a richer case
   under `core/evals/seed/` (or the owning agent's `evals/`).
3. Build a synthetic, tenant-free fixture under the suite's `fixtures/`. Never
   use a real business, person, vendor, or tool-account name (the
   bleed-through lint rejects them under `core/`).
4. If the engine module that makes the assertion pass does not exist yet, mark
   the test `@pytest.mark.phase2` and `@pytest.mark.skip(reason="Phase N: <module>")`.
   Unskipping it is that phase's definition of done.
5. The assertion checks that the *bad pattern* no longer occurs, not that one
   specific input works.
