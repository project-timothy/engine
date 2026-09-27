# Pure-Python core with a model seam; the Claude Agent SDK is an optional extra
Date: 2026-09-11
Type: One-way door

PRD v2 section 5.1: the product core is pure Python behind one model
gateway, the Claude Agent SDK becomes an optional extra installed as
`[claude]`, and no product path depends on Claude Code or a Claude Max
seat.

Why: today the provider is not a tenant setting. Three of the four live
model sites are Claude-only through the Agent SDK, which spawns the Claude
Code binary and authenticates with a Max seat, and two lanes (the 06:00
audit triage and the Thursday bank-feed sweep) are prose skills executed
by a Claude Code session. Anthropic's terms are explicit: third parties
may not offer claude.ai login for products built on the SDK, and the
Chrome integration is off under API-key auth. A product that any business
can install on any machine cannot carry that dependency.

The shape the PRD fixes:

- One gateway module (`core/llm/`) with adapters for Anthropic native
  (Messages API with structured outputs), OpenAI-compatible (OpenAI,
  Gemini's compatibility endpoint, OpenRouter, vLLM, Ollama, the existing
  local gateway), and an optional `claude_agent_sdk` adapter for the
  agentic lanes only. No hosted gateway sits in the money path by default.
- A per-tenant policy table (`[llm.tiers]`, `[llm.jobs]`, `[llm.budget]`)
  names the model per job type; `w9_detect` stays `deterministic`.
- Structured output enforced at the seam: schema to providers that support
  constrained decoding, pydantic validates regardless, one retry with the
  validation error appended, then a review card. Money stays `Decimal`
  strings.
- Telemetry on every call (model id, tokens, dollars) so the
  self-improvement question "does this loop return more than it costs" has
  numbers outside transcripts.
- A runner abstraction (`core/llm/runner.py`) for the agentic lanes with
  three runners in order of tenant preference: the Claude Agent SDK
  adapter when the tenant holds an Anthropic key, OpenHands headless, and
  a minimal in-repo loop over the gateway as the always-available floor.
  The tool allowlist is enforced in Python, not in the prompt.

Provider independence and data locality are the reasons for the seam, not
cost: a two-person firm at 50 to 200 documents a month spends under $2 a
month on extraction at any tier.

Why one-way: every model call site, the policy table, and the eval sets
target the seam, and the phase 7 exit criterion is the demo tenant running
the full daily loop and a nightly audit in a container on a non-Mac host
with only API keys, no Claude Code, no Max seat.

The gateway's function signature and validation contract are a separate
decision, `2026-09-11-model-gateway-interface.md` (row 7.8); this file
records the product decision that requires it.
