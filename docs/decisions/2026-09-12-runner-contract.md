# The runner contract
Date: 2026-09-12
Type: One-way door

Every agentic session in the engine goes through one function,
`core.llm.runner.run(skill, context, allowlist, *, runner, budget) ->
RunnerResult`, and every runner is a `Runner` with `name` and one method,
`run(session) -> RunnerResult`. `run()` owns what must be identical whatever
runner executes: the `ToolGate` (every tool call passes one Python check
before it executes; a refused call is recorded and returned to the model as
a refusal), the JSONL transcript with its redaction, the budget clock
(turns, seconds, usd, tokens), the inputs digest (skill sha, context digest,
allowlist digest, runner name, model id), and the result (status FINISHED,
SKIPPED, or FAILED with a named stop reason; note text; files changed by
worktree hash; commands run; refused calls; tokens; usd; turns; wall
seconds; transcript path). Runners own only how a conversation is held.

Two runners ship in the code PR: `ClaudeAgentSdkRunner` (the `[claude]`
extra, lazy import; the allowlist enforced through the SDK's tool-permission
callback, never its prompt) and `GatewayLoopRunner` (one `complete()` per
turn against a `Turn` output model that carries either one tool call or the
final note). An OpenHands runner is a later row behind the same contract.

Why one-way: the interface is what rows 7.6, 7.17, and 7.18 call, what the
7.14 harness replays, and what every later runner implements. Changing its
shape after those land means touching all of them at once. It extends
`2026-09-11-model-gateway-interface.md`: the loop runner is a caller of
`complete()`, not a change to it, and the gateway's `CallRecord` is the
runner's token and usd source.

Choices inside the decision, made in this row and not specified by the plan:

- The result type is `RunnerResult`, not `RunResult`: `core.engine.result`
  already owns `RunResult` for jobs, and the job that calls the runner
  returns one of those.
- The in-repo loop's turn protocol is structured output (`Turn`) on the
  existing `complete()`, one turn per call, not native tool use; native
  tool use can arrive later as an adapter optimization behind the same
  `Turn` schema.
- `gh pr merge` is not a tool: `GhTool.subcommands` is a closed literal of
  `pr create`, `issue edit`, `pr comment`, so no allowlist can admit it.
- `eval_first` is a named gate on `GhTool`, not a special case in a skill:
  `gh pr create` needs a new or changed test file and a passing pytest run
  after the last edit.
- `files_changed` comes from hashing the file root before and after, never
  from tool-call bookkeeping, so both runners report the same truth.
- The runner never skips on a prior transcript; the calling job's `RunKey`
  carries `inputs_digest` and the engine's idempotency does the skipping.
- Budgets stop the run FAILED with the model's last `progress` as the note;
  the runner's clock sits inside the headless wrapper's 3 h kill.
- Skill frontmatter is parsed as flat `key: value` lines only; no YAML
  dependency enters the core.

Supersedes nothing; `docs/runner-design.md` is the design behind it.
