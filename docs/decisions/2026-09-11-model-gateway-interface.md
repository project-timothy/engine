# The model gateway interface
Date: 2026-09-11
Type: One-way door

Every model call in the engine goes through one function,
`core.llm.complete(job_type, messages, output_model, *, adapter, model,
attachments=None, timeout_s=120, pricing=None) -> GatewayResult`, and every
provider is an `Adapter` with one method, `complete(bundle, schema) ->
RawReply`. The gateway owns validation (pydantic, one retry with the error
appended, then `GatewayValidationError` carrying both raw replies), the
money rule (money is a `DecimalString`; an output model with a `Decimal`
field is refused before any call), and telemetry (one `CallRecord` per call,
persisted by the caller). Adapters own only the wire format.

Why one-way: the interface is the seam every later adapter, every call site
(rows 7.10 to 7.12), the policy table (7.9), the eval sets (7.13), and the
auditor's vendored copy (7.12) target. Changing its shape after those land
means touching all of them at once.

Choices inside the decision, made in this row and not specified by the plan:

- The schema reaches the model twice: structurally where the provider has a
  slot (Anthropic `output_config.format`), and as text in a system turn on
  every call, so JSON-mode providers see the shape too.
- `Decimal` fields in an output model are refused, not tolerated, because
  pydantic would coerce a JSON float into one silently.
- `pricing` is an optional keyword on `complete()`; `usd` is `None` without
  it. The gateway never guesses a price; the policy table supplies it (7.9).
- Transport failures are not retried by the gateway. `GatewayTransportError`
  carries `cause` and `transient` in the `ExtractionError` taxonomy so the
  caller's existing redial policy keeps working.
- Attachments ride the first user turn, so a retry's correction turns never
  displace the document.
- The Anthropic adapter tightens the schema for constrained decoding
  (`additionalProperties: false` on every object, numeric and length bounds
  stripped); pydantic enforces the bounds on the way back.

Supersedes nothing; `docs/model-seam-design.md` is the design behind it.
