# Boundary rules: when code, when a model, when a human, when a browser

Every new capability is classified before it is built, and the
classification is recorded in the job's `schema.py` docstring and pinned by an
eval.

## The decision table

| Question, asked in order | If yes | Example |
|---|---|---|
| 1. Is the answer binary-correct and computable from structured inputs? | **Code.** No model on the path, ever. | Three-way payment match, aging bands, 1099 thresholds, due dates, check-sequence gaps, PO burn, tie-outs, every money value |
| 2. Is the input unstructured (PDF, image, email prose, a scanned form)? | **Model extracts into a typed schema; code validates and cross-checks; a failed validation is a review card, never a guess.** | Invoice and receipt extraction, remittance emails, COI parsing (ACORD 25), vendor notices |
| 3. Is it a judgment with no ground truth in the data? | **Model proposes with a confidence; code applies thresholds and registries; below threshold parks a card.** | Is this a receipt, which vendor is this sender, which pages form one document, which category |
| 4. Does it move money, send anything external, or mutate a system of record? | **Approval card.** Approve and reject record only; the owning job executes on its next run with the original computed parameters. A tenant policy table may name specific actions as unattended. | QBO writes, payments, emails, payroll submission, filings |
| 5. Is it prose for a human to read? | **Model drafts from facts computed in code; a deterministic fallback renders if the model fails; the draft cannot mint findings, severities, or numbers.** | Auditor advisory, close narrative, triage note |
| 6. Does the target system have no API and the data exists only in a UI? | **Last resort: a document or report-inbox lane first; a browser lane only opt-in, local, headed, rate-limited, and never for banks.** | QuickBooks bank feed, a customer payment portal, Buildertrend, Toast exports |

Two rules sit above the table. **Money fields are `Decimal` strings in every schema and are re-parsed in code;** a model never writes a number that lands in a ledger. **A model call site ships with a fixture implementation** so the suite runs with no network and no key.

## The four constructs every obligation maps to

Everything a small business's back office must do lands in one of four shapes the engine already has:

| Construct | What it is | Owner action |
|---|---|---|
| **Scheduled job** | Deterministic, idempotent, run-keyed, leaves a trace on failure | None unless it parks a card |
| **Auditor lens** | Independent check over the ledger, files, host, or an external system; checklist-not-alarm; once-only; triage memory | Answer once; snooze or acknowledge |
| **Parsing lane** | Mail fetch or folder watch, deterministic detection first, model extraction second, code validation third, card on doubt | Approve the proposal or fix the source |
| **Reminder** | A dated obligation the engine cannot execute (sign, file under the owner's login, renew) with the evidence attached | The act itself |

The obligations catalog assigns every recurring obligation of a US small business to one of these four.

## The policy table is the unit of delegation

The bank sweep set the shape: `[qbo_sweep].unattended = ["match", "pair"]` names exactly what the machine may do without a card, and the lane refuses anything outside it. The engine generalizes it. Every lane that can act carries a `[<lane>].unattended` list in `tenant.toml`, defaulting to empty, and widening delegation is a data-only PR the owner can read on one screen. The auditor's `approvals` lens reports every unattended act by lane and count, so delegation is visible every morning.

## Evals gate everything, including the model half

The code half already has the rule that an incident gets an eval before its fix. The engine extends the same gate to the model half: every job type that calls a model owns a labeled eval set under `evals/<job>/`, every skill (a prose contract a model executes) owns a fixture-driven harness ("given this audit report, the note must contain these sections and never these words"), and a model or tier change in `tenant.toml` is refused at startup unless the eval results for that model are on file. A prompt edit becomes a gated change like any other.

