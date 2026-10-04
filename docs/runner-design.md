# The runner: `core/llm/runner.py`

Phase 7 row 7.16. Sibling of
`docs/model-seam-design.md` (the gateway, row 7.8), which this note builds
on. Decision record: `docs/decisions/2026-09-12-runner-contract.md`
(one-way door: the interface every consumer and every later runner targets).
Design only; the code PR follows its merge.

## Why a runner

The gateway answers one question per call. The agentic lanes (triage, the
build lane, proposal PRs) need a session: a skill, a bundle of inputs, a
bounded set of tools, and a note at the end. The runner is that seam, so the
product runs those lanes with an API key, no Claude Code binary, no Max seat.

## The interface

```python
from core.llm.runner import run, Skill, ContextBundle, ToolAllowlist, Budget

result = run(skill, context, allowlist, runner=runner, budget=budget)
result.status      # FINISHED | SKIPPED | FAILED
result.note        # the note text, the deliverable
```

`run()` is the one entry point and owns what must be identical whatever
runner executes: the tool gate, the transcript, the budget clock, the inputs
digest, and the result. A `Runner` is a Protocol with `name: str` and
`run(session: Session) -> RunnerResult`; `Session` is the runner-facing view
(`skill, context, gate, budget, transcript, model`). Type sketches, pydantic
(`Decimal` is fine here: none of these is a model output schema):

```python
class Skill(BaseModel):          # Skill.load(path); flat "key: value" frontmatter only
    path: Path; name: str; description: str        # the spec's two required fields
    frontmatter: dict[str, str]; body: str; sha256: str

class ContextItem(BaseModel):
    name: str; kind: Literal["text", "file"]
    text: str | None = None; path: Path | None = None   # files are copied into the stage
    sha256: str

class ContextBundle(BaseModel):
    items: list[ContextItem]     # ORDER IS PART OF THE CONTRACT
    def digest(self) -> str: ... # sha256 over (name, kind, sha256) in order

class FileTools(BaseModel):  root: Path; write: bool = True    # read_file, write_file, list_files
class ShellTool(BaseModel):  root: Path; argv_prefixes: list[list[str]]; max_seconds: int = 600
class GitTool(BaseModel):    root: Path; subcommands: list[Literal["status", "diff", "log",
                                 "add", "commit", "fetch", "rev-parse", "switch", "push"]]
class GhTool(BaseModel):     root: Path; subcommands: list[Literal["pr create", "issue edit",
                                 "pr comment"]]; gates: list[Literal["eval_first"]] = []
class PytestTool(BaseModel): root: Path; max_seconds: int = 1800

class ToolAllowlist(BaseModel):
    files: FileTools | None = None; shell: ShellTool | None = None
    git: GitTool | None = None; gh: GhTool | None = None; pytest: PytestTool | None = None
    def digest(self) -> str: ... # sha256 of model_dump_json(sort_keys=True)

class Budget(BaseModel):
    max_turns: int; max_seconds: int
    max_usd: Decimal | None = None; max_tokens: int | None = None   # input + output

class RunnerResult(BaseModel):
    status: Literal["FINISHED", "SKIPPED", "FAILED"]
    stop_reason: str   # note | precondition | turns | seconds | usd | tokens | transport | validation
    note: str                         # FINISHED: the model's note; FAILED: its last progress
    files_changed: list[FileChange]   # path, sha256_before, sha256_after (None = absent)
    commands_run: list[CommandRun]    # tool, argv, cwd, exit_code, seconds
    refused: list[RefusedCall]        # turn, tool, argv or path, reason
    input_tokens: int; output_tokens: int; usd: Decimal | None; turns: int; wall_seconds: int
    model: str                        # the id the policy resolved; data, never a literal
    runner: str; inputs_digest: str; transcript_path: Path
```

The gateway's `CallRecord` already supplies `input_tokens`, `output_tokens`,
`usd`, `model`, and `latency_ms` per call; the loop sums them over its
turns, the SDK runner reads the same counts from the SDK's result message.
`files_changed` is computed by hashing the file root before and after the
session (the worktree is the truth), never from tool-call bookkeeping, so
both runners report the same thing however a file got written. `Skill.load`
reads flat `key: value` frontmatter only (nested or list values raise
`SkillFormatError`; no YAML dependency); Claude-specific fields ride along as
strings and both runners ignore them.

## The two runners

**`ClaudeAgentSdkRunner`** (`core/llm/adapters/claude_sdk.py`, the `[claude]`
extra from row 7.15, imported lazily; absent extra = SKIPPED `precondition`).
Skill body = system prompt, rendered bundle = the one user prompt, `cwd` =
the file root, no setting sources: the skill file is the whole brief. The
allowlist maps two ways. `allowed_tools` stays EMPTY and `disallowed_tools`
removes every SDK tool the allowlist does not map (`Read`, `Glob`, `Grep`
for file tools, plus `Write`, `Edit`, `MultiEdit`, `NotebookEdit` when
writes are allowed; `Bash` when shell, git, gh, or pytest is present). Every
call then reaches our `ToolGate` through a `PreToolUse` hook, which answers
deny with the gate's reason, and the SDK's `can_use_tool` callback answers
the permission ask from the verdict the hook already recorded, so a
headless session never blocks on a prompt. Enforcement is the gate's
verdict, never the SDK's prompt or permission mode. Verified against the
pinned SDK (0.2.152) in the code PR: `query()` routes both `can_use_tool`
and `hooks` to the transport, so the one-shot call the extraction sites use
is enough and `ClaudeSDKClient` is not needed; but `can_use_tool` fires only
when the CLI would otherwise ask (a tool named in `allowed_tools` shadows
it, with a `CanUseToolShadowedWarning`, and an auto-approved read never
reaches it), which is why the gate sits in the hook and `allowed_tools` is
empty. A `Bash` call is parsed with `shlex` into ONE argv; shell operators
are refused, the same rule as the loop's shell tool. `max_turns` and `model`
come from the budget and the policy; `max_buffer_size` is 32 MB (incident
2026-09-04). The message stream is written to the transcript event by event.

**`GatewayLoopRunner`** (`core/llm/runner.py`): one `complete()` per turn
with output model `Turn`, the always-available floor:

```python
# one pydantic model per tool call, discriminated by `tool`:
#   read_file(path)  write_file(path, content)  list_files(glob)
#   shell(argv)  git(args)  gh(args)  pytest(args)
class Turn(BaseModel):
    reason: str = ""                 # one sentence, transcript only
    progress: str | None = None      # the running note, kept when a budget stops the run
    call: ReadFile | WriteFile | ListFiles | Shell | Git | Gh | Pytest | None = None
    note: str | None = None          # the final note; exactly one of call / note
```

Messages: `system` = skill body + a tool manual generated from the allowlist
(the model is told only about tools it has) + the schema turn the gateway
adds; `user` = the rendered bundle. Each turn appends the Turn JSON as an
`assistant` message and a `ToolResult` JSON (`tool, ok, exit_code, stdout,
stderr, refused_reason`) as the next `user` message; stdout and stderr are
cut to 16 KB head plus tail in the message and kept whole in the transcript.
The loop ends when a Turn carries `note`, or a budget stops it. A Turn that
fails validation gets the gateway's one retry; a second failure ends the run
FAILED `validation`.

Why Turn-as-structured-output: the gateway validates every reply and retries
once, so every turn gets that for free, and JSON-mode providers (the local
tier) can drive it. The alternative, native tool use (`tools=` on the wire,
`tool_use` blocks) added to the `Adapter` protocol, is higher fidelity on
Anthropic and OpenAI but widens the one-way-door gateway interface and
leaves JSON-mode providers without a loop; it can arrive later as an adapter
optimization mapping the Turn schema onto native tool definitions without
touching this contract. [NEEDS REVIEW] Whether a cheap-tier model holds the
protocol across forty turns is a 7.13 eval question, not a contract question.

Identical between the runners: `RunnerResult`, the `ToolGate` and its
refusal text, the transcript vocabulary, `files_changed` from the worktree
hash, the budget clock. Different: who holds the conversation, how a call is
expressed (SDK tool names or the Turn union), where token counts come from.

## Enforcement

`ToolGate.check(call) -> Allowed | Refused(reason)` is the one gate; nothing
executes without passing it. Per tool: file paths resolve (symlinks
followed) inside `root`, `.git/` is never written; shell argv starts with an
allowed prefix, one plain argv, no operators, `cwd = root`, subprocess
environment reduced to `PATH`, `HOME`, and the stage variables (no provider
key reaches a model-directed process); git only the declared subcommands,
`push` only `-u origin <current branch>`, never `main`, never `--force`; gh
only the three subcommands `GhTool` can declare; pytest only paths inside
`root` and `-q -x -k -p no:cacheprovider`. `gh pr merge` is not a tool:
`GhTool.subcommands` is a closed `Literal`, so no allowlist can admit it.

A refused call is appended to `result.refused`, written to the transcript,
and returned to the model as a `ToolResult` with `ok=false` and the reason,
so the session can route around it or stop. A refusal costs a turn;
`max_turns` bounds a session that keeps asking.

`eval_first` (row 7.17's gate) is a named `GhTool` gate, not a special case:
`gh pr create` is refused unless (a) `git diff --name-only` against the base
plus untracked files includes a new or changed path under `tests/` or an
`evals/` directory, and (b) the last recorded `pytest` in this session
exited 0 after the last `write_file`. The refusal text names which half
failed.

## Transcripts

One JSONL file per run at `<transcript_dir>/<inputs_digest>.jsonl`, the same
events from both runners: `run_start` (skill sha, bundle digest, allowlist
digest, runner, model, budget), `prompt` (the rendered system and user text,
once), `turn` (n, the raw model output as validated, the `CallRecord`),
`tool_call`, `tool_result` (full output), `refused`, `run_end` (the
`RunnerResult` minus the path). `turn.raw` is what a replay feeds back: the
loop through `FixtureAdapter` seeded with the recorded Turn replies, the SDK
runner through the same message handling fed the recorded stream. That is
how the row's acceptance runs offline, and how the 7.14 harness grows from
note contracts to whole-session replays.

`redact()` runs on every event before it is written: the value of every
environment variable the tenant's provider config names (`<redacted:VAR>`),
bearer and API-key shaped strings (`sk-`, `ghp_`, `github_pat_`, `Bearer `),
and TIN-shaped strings (EIN `dd-ddddddd`, SSN `ddd-dd-dddd`) as
`<redacted:tin>`. The eval reuses the W-9 lane's planted-TIN pattern
(`test_no_tin_shaped_content_ever_leaves_the_document`): a fake TIN in a
context file appears nowhere in the transcript or the result.

## Budgets and the wall clock

Four stops, checked before every turn and after every tool: `max_turns`
(one `complete()` or one SDK assistant message), `max_seconds` (the runner's
own clock from `run_start`), `max_usd` (summed `CallRecord.usd`),
`max_tokens`. A budget stop yields FAILED with `stop_reason` naming the
budget, `note` = the last `progress` the model wrote, and every count as of
the stop; a tool in flight finishes or is killed at its own `max_seconds`,
never mid-write. `max_usd` with no pricing on the policy is SKIPPED
`precondition` before any call. FINISHED requires a note whose first line
carries no unfinished marker (`preliminary`), the wrapper's rule in Python.

The runner's clock sits inside the wrapper's: the headless wrapper keeps its
3 h kill (exit 124) as the outer fence for a process that hangs where no
Python runs (a consent prompt, a stuck subprocess); `max_seconds` sits below
it (2 h for triage) so the slow case ends with a FAILED result and a
transcript instead of a killed process.

## The run key

`inputs_digest = sha256(skill.sha256, context.digest(), allowlist.digest(),
runner.name, model, RUNNER_VERSION)[:16]`. It names the transcript and rides
the result. The calling job feeds it into its own `RunKey` as
`key.value("runner", inputs_digest)`, so the engine's idempotency (matching
key = no work) covers runner sessions like any other input; the runner never
skips on a prior transcript, it only reports `prior_transcript` when one
exists with the same digest. Same skill, bundle, allowlist, runner, and
model: the same key, and a re-run is recognizable.

## Consumers

**7.6, the build lane.** Today a Claude Code session runs the build skill by
hand; the wrapper creates the worktree, the session edits, tests, and opens
the PR. On the runner the wrapper stays, the skill file is the brief, the
allowlist is `files` + `shell` (ruff and the two lints) + `git` + `gh` with
`eval_first` + `pytest`, and the PR body sections the harness asserts are a
note contract like any other. The wrapper's exit codes do not change.

**7.17, triage ported (SHIPPED 2026-09-17).** The wrapper's contract is
untouched: it still stages the reports, installs a finished note, writes its
own SKIPPED and FAILED markers, kills at 3 h. What changed: it calls
`engine runner run` instead of the `claude` binary (`core/llm/runner_cli.py`,
the entry point row 7.16 deliberately left out); the params build the bundle
from the stage (the last three audit reports and the last three triage notes,
as `dir:` items), the policy's `audit_triage` tier picks the runner, and the
CLI writes `result.note` to the note stage. The eval-first rule moved from
prose into `eval_first`, which this CLI admits on every `gh` tool with no way
to switch it off. The harness proves the note sections unchanged; the skill
file moved to `skills/audit-triage/` with a symlink at the old tenant path,
so `~/.claude/skills/audit-triage` and `/audit-triage` need no re-linking.

Two things the design note did not anticipate, both decided in the build
(`docs/decisions/2026-09-17-the-triage-lane-on-the-runner.md`):

- **The wrapper makes the worktree.** `git worktree` is not a runner
  subcommand and `git switch main` is refused, so a session working in the
  dev tree could never hand it back on main and the next night would skip on
  a busy tree. The wrapper cuts one off `origin/main` under the dev tree's
  gitignored `.claude/worktrees/`, names it as the runner's root, and removes
  it afterwards unless it carries a commit.
- **`triage.toml` is not a bundle item.** It is in the file root, so the
  session reads it with a tool if it needs it, and the bundle stays the
  reports.

**7.18, proposal PRs (SHIPPED 2026-09-17).** A `recurrence` finding whose
candidate is named becomes a design note under `docs/proposals/` plus one
failing eval under the top-level `evals/` tree, and nothing else. The shape
is not a narrower allowlist in the end but a second gate: `GhTool.gates`
carries `["eval_first", "proposal"]`, and `pr create` is judged by the shape
of the diff, so a proposal that also carries an implementation is refused by
one gate or the other. The lane asks for it with `--param proposal=1`; the
CLI has no way to drop `eval_first` from the pair.

Once-only is keyed on lens 19's candidate id (`auditor/findings.py`,
`candidate_id` over the source lens and condition), which rides the finding
as data and ends its detail line as `candidate: <text> [<id>]`. The gate
refuses a proposal whose id another note under `docs/proposals/` already
carries, so a merged proposal closes the question in Python; an unmerged one
is closed by the previous triage note in the bundle. The full reasoning is
`docs/decisions/2026-09-17-proposal-prs-with-an-eval-budget.md`.

## First hand run 2026-09-17

Row 7.16 merged the runner with no caller, so nothing had ever driven it
against a live model. Row 7.17's precondition was to fix that before wiring
the 06:00 lane to it. Three sessions ran by hand from the lane worktree
through `engine runner run` on the `ClaudeAgentSdkRunner` against the owner's
Max seat (the `seat` tier, model id `default`). Verbatim:

```
$ uv run --extra claude engine runner run <tenant> \
    tests/skills/fixtures/hand-run/SKILL.md --adapter sdk \
    --param job=audit_triage --param file:subject=docs/decisions/README.md \
    --param max_turns=6 --param max_seconds=300 --param note=<stage>/note.md

runner/hand-run @ <tenant>: FINISHED (note)
  runner: claude_agent_sdk  model: default  turns: 1  tokens: 2 in / 298 out
  usd: 0.204282  wall: 10s
  files changed: 0  commands: 0  refused: 0
```

Wall clock 10 s (`time` agreed: 10.899 s total). The note came back as the
session's last message, was written to the note path, and reads as one
paragraph that names a detail only a reader of the file would have: the three
rebases of 2026-09-10 that froze the decision log.

Two more, with tools, on the same seat, to prove the gate live before a
scheduled lane leaned on it (root = a scratch directory outside the repo):

```
# tools=write, shell="echo"
runner/edit-and-run @ <tenant>: FINISHED (note)
  runner: claude_agent_sdk  model: default  turns: 4  tokens: 6 in / 370 out
  usd: 0.2639185  wall: 10s
  files changed: 1  commands: 1  refused: 0
     -> notes/hello.txt written (sha recorded, before=None), argv ["echo","hello"] exit 0

# the same skill and task, but shell="ls": the echo has no allowed prefix
runner/edit-and-run @ <tenant>: FINISHED (note)
  runner: claude_agent_sdk  model: default  turns: 7  tokens: 6 in / 1067 out
  usd: 0.15560200000000002  wall: 19s
  files changed: 1  commands: 1  refused: 1
  refused shell: argv ['echo', 'hello'] starts with no allowed prefix (ls)
```

What the three runs establish, beyond "it runs": the `PreToolUse` hook
answers a denied call with a reason the session RECEIVES and routes around
(the third session ran `ls` instead and still finished with a note, no hang
and no permission prompt); `files_changed` comes back from the worktree hash
with the real sha; a refused call never executes and is on the result. Every
session wrote its JSONL transcript under the named transcripts directory, and
the tenant's key variable never appears in one.

One finding worth knowing before anyone reads a token count: **the SDK's
`usage.input_tokens` excludes cache-creation tokens**, so `result.input_tokens`
read 2 while the same result's `cache_creation_input_tokens` was 19,559. The
usd figure is the SDK's own `total_cost_usd` and is unaffected, so the
budget's `max_usd` stop is honest; `max_tokens` is not, and a lane that wants
a real token bound needs the cache counters added to the charge.
[NEEDS REVIEW] whether to fold `cache_creation_input_tokens` and
`cache_read_input_tokens` into `Session.charge` (it changes what every SDK
result reports, so it is its own row, not a side effect of this one).

## Not in this row

The OpenHands adapter (a later row, a third `Runner` behind the same
contract); parallel tool calls (one call per turn); streaming; compaction of
old turns in the loop; anything that changes a scheduled job (7.17 is the
first wiring, on a coding day with the owner).
