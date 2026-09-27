"""The runner's tools: the allowlist models, the tool-call models, and the
one gate every call passes (``docs/runner-design.md``, "Enforcement").

``ToolGate.check(call)`` is the only place a tool call is judged; nothing
executes without it, whichever runner holds the conversation. Per tool:

- file tools resolve their path (symlinks followed) inside ``root`` and
  never write under ``.git/``;
- shell runs one plain argv that starts with a declared prefix, no shell
  operators, ``cwd = root``, the subprocess environment reduced to ``PATH``,
  ``HOME``, and the named stage variables, so no provider key reaches a
  model-directed process;
- git runs only the declared subcommands, and ``push`` only as
  ``-u origin <current branch>``, never ``main``, never forced;
- gh runs only the three subcommands :class:`GhTool` can declare, and
  ``pr create`` behind the ``eval_first`` gate when the allowlist names it,
  or behind the ``proposal`` gate when the diff is proposal-shaped;
- pytest runs only paths inside ``root`` and the flags ``-q -x -k`` and
  ``-p no:cacheprovider``.

``gh pr merge`` is not a tool: ``GhTool.subcommands`` is a closed ``Literal``
and a :class:`Gh` call names a subcommand outside it fails validation, so
neither an allowlist nor a model turn can express it. Likewise ``merge``,
``rebase``, ``reset``, and ``stash`` are absent from :data:`GitSubcommand`.

The ``proposal`` gate (row 7.18) is the narrower of the two, never a way
around ``eval_first``: it admits a PR that carries exactly one new design
note under ``docs/proposals/``, exactly one new eval under the top-level
``evals/`` tree, nothing else at all, and a ``pytest`` run in this session
that named that eval and FAILED. A failing eval is the point: it is the
decision the owner merges, and the ``evals/`` tree sits outside pytest's
``testpaths`` so merging red never turns the suite red. A diff that touches
source is not proposal-shaped, so it still answers to ``eval_first`` and its
green suite.

A refused call is recorded on the gate (``refused``), written to the
transcript, and handed back as a :class:`ToolResult` with ``ok=false`` and
the reason, so the session can route around it or stop.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, Field, model_validator

from core.llm.transcript import Transcript

# ---- the allowlist ------------------------------------------------------------

GitSubcommand = Literal[
    "status", "diff", "log", "add", "commit", "fetch", "rev-parse", "switch", "push"
]
GhSubcommand = Literal["pr create", "issue edit", "pr comment"]
GhGate = Literal["eval_first", "proposal"]

GIT_SUBCOMMANDS: tuple[str, ...] = get_args(GitSubcommand)
GH_SUBCOMMANDS: tuple[str, ...] = get_args(GhSubcommand)

PROPOSAL_DOC_DIR = "docs/proposals"
"""Where a proposal's design note lives; it is also the PR body."""
PROPOSAL_EVAL_DIR = "evals"
"""The top-level tree for evals that FAIL by design. It is outside pytest's
``testpaths`` on purpose: the owner merges the red eval as the decision and
the suite stays green until the build lane implements the row."""
PROPOSAL_SECTIONS = ("Design", "Failing eval")
_CANDIDATE_LINE = re.compile(r"^Candidate:[ \t]+([A-Za-z0-9][A-Za-z0-9._-]{3,63})[ \t]*$", re.M)

PYTEST_FLAGS = frozenset({"-q", "-x"})
PYTEST_FLAGS_WITH_VALUE = {"-k": None, "-p": "no:cacheprovider"}
PYTEST_ARGV = ("uv", "run", "pytest")

_SHELL_OPERATORS = frozenset({"&&", "||", "|", ";", "&", ">", ">>", "<", "<<", "2>", "2>&1"})
_SHELL_OPERATOR_FRAGMENTS = ("$(", "`", "|", ";", "&", ">", "<")
_SUBSTITUTION_FRAGMENTS = ("$(", "`")
_FORCE_FLAGS = frozenset({"--force", "-f", "--force-with-lease", "--force-if-includes"})
_HASH_SKIP_DIRS = frozenset(
    {".git", "__pycache__", ".venv", ".pytest_cache", ".ruff_cache", "node_modules"}
)


class FileTools(BaseModel):
    """``read_file``, ``write_file``, ``list_files`` rooted at ``root``."""

    root: Path
    write: bool = True


class ShellTool(BaseModel):
    root: Path
    argv_prefixes: list[list[str]]
    max_seconds: int = 600


class GitTool(BaseModel):
    root: Path
    subcommands: list[GitSubcommand]


class GhTool(BaseModel):
    root: Path
    subcommands: list[GhSubcommand]
    gates: list[GhGate] = Field(default_factory=list)
    base_ref: str = "origin/main"
    """What ``eval_first`` and ``proposal`` diff against: the former to find a
    new or changed test file, the latter to prove the whole diff is a design
    note and one eval."""


class PytestTool(BaseModel):
    root: Path
    max_seconds: int = 1800


class ToolAllowlist(BaseModel):
    files: FileTools | None = None
    shell: ShellTool | None = None
    git: GitTool | None = None
    gh: GhTool | None = None
    pytest: PytestTool | None = None
    env_passthrough: list[str] = Field(default_factory=list)
    """Environment variable NAMES copied into every subprocess (the stage
    variables a wrapper exports). ``PATH`` and ``HOME`` always pass."""

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True).encode("utf-8")
        ).hexdigest()

    def roots(self) -> list[Path]:
        seen: list[Path] = []
        for tool in (self.files, self.shell, self.git, self.gh, self.pytest):
            if tool is not None and tool.root not in seen:
                seen.append(tool.root)
        return seen

    def tool_names(self) -> list[str]:
        names: list[str] = []
        if self.files:
            names += ["read_file", "list_files"] + (["write_file"] if self.files.write else [])
        if self.shell:
            names.append("shell")
        if self.git:
            names.append("git")
        if self.gh:
            names.append("gh")
        if self.pytest:
            names.append("pytest")
        return names


# ---- the calls ----------------------------------------------------------------


class ReadFile(BaseModel):
    tool: Literal["read_file"] = "read_file"
    path: str


class WriteFile(BaseModel):
    tool: Literal["write_file"] = "write_file"
    path: str
    content: str


class ListFiles(BaseModel):
    tool: Literal["list_files"] = "list_files"
    glob: str = "**/*"


class Shell(BaseModel):
    tool: Literal["shell"] = "shell"
    argv: list[str]

    @model_validator(mode="after")
    def _non_empty(self) -> Shell:
        if not self.argv:
            raise ValueError("shell needs an argv")
        return self


class Git(BaseModel):
    tool: Literal["git"] = "git"
    args: list[str]

    @model_validator(mode="after")
    def _non_empty(self) -> Git:
        if not self.args:
            raise ValueError("git needs a subcommand")
        return self


class Gh(BaseModel):
    tool: Literal["gh"] = "gh"
    args: list[str]

    @property
    def subcommand(self) -> str:
        return " ".join(self.args[:2])

    @model_validator(mode="after")
    def _closed_subcommands(self) -> Gh:
        if self.subcommand not in GH_SUBCOMMANDS:
            raise ValueError(
                f"gh has no subcommand {self.subcommand!r} here; only "
                + ", ".join(repr(s) for s in GH_SUBCOMMANDS)
            )
        return self


class Pytest(BaseModel):
    tool: Literal["pytest"] = "pytest"
    args: list[str] = Field(default_factory=list)


ToolCall = Annotated[
    ReadFile | WriteFile | ListFiles | Shell | Git | Gh | Pytest, Field(discriminator="tool")
]

COMMAND_TOOLS = ("shell", "git", "gh", "pytest")


def call_target(call: ReadFile | WriteFile | ListFiles | Shell | Git | Gh | Pytest) -> str:
    """The path or argv a call names, for the refusal record."""
    if isinstance(call, ReadFile | WriteFile):
        return call.path
    if isinstance(call, ListFiles):
        return call.glob
    return " ".join(call_argv(call))


def call_argv(call: Shell | Git | Gh | Pytest) -> list[str]:
    if isinstance(call, Shell):
        return list(call.argv)
    if isinstance(call, Git):
        return ["git", *call.args]
    if isinstance(call, Gh):
        return ["gh", *call.args]
    return [*PYTEST_ARGV, *call.args]


# ---- results ------------------------------------------------------------------


class ToolResult(BaseModel):
    tool: str
    ok: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    refused_reason: str | None = None

    def for_model(self, cap: int = 16 * 1024) -> str:
        """The JSON the model sees: stdout and stderr cut to head plus tail
        at ``cap`` bytes; the transcript keeps them whole."""
        data = self.model_dump()
        data["stdout"] = _cut(self.stdout, cap)
        data["stderr"] = _cut(self.stderr, cap)
        return json.dumps(data)


def _cut(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    half = cap // 2
    return f"{text[:half]}\n...[{len(text) - cap} characters cut]...\n{text[-half:]}"


class FileChange(BaseModel):
    path: str
    sha256_before: str | None
    sha256_after: str | None


class CommandRun(BaseModel):
    tool: str
    argv: list[str]
    cwd: str
    exit_code: int | None
    seconds: float


class RefusedCall(BaseModel):
    turn: int
    tool: str
    target: str
    reason: str


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str = ""


ALLOWED = Verdict(True)


# ---- subprocess plumbing ------------------------------------------------------


@dataclass(frozen=True)
class CompletedRun:
    exit_code: int
    stdout: str
    stderr: str
    seconds: float


def reduced_env(passthrough: list[str] | tuple[str, ...] = ()) -> dict[str, str]:
    """``PATH``, ``HOME``, and the named stage variables that are set. Nothing
    else from the parent reaches a model-directed process."""
    env: dict[str, str] = {}
    for name in ("PATH", "HOME", *passthrough):
        value = os.environ.get(name)
        if value is not None and name not in env:
            env[name] = value
    return env


def run_subprocess(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
) -> CompletedRun:
    """Module-level so a test can stand in for it. A timeout kills the child
    and reports exit 124, the shell convention the wrappers already use."""
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return CompletedRun(
            124,
            (exc.stdout or b"").decode("utf-8", "replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or ""),
            f"timed out after {timeout}s",
            time.perf_counter() - started,
        )
    except FileNotFoundError as exc:
        return CompletedRun(127, "", str(exc), time.perf_counter() - started)
    return CompletedRun(proc.returncode, proc.stdout, proc.stderr, time.perf_counter() - started)


# ---- the worktree hash --------------------------------------------------------


def hash_tree(root: Path) -> dict[str, str]:
    """``relative path -> sha256`` for every file under ``root``, skipping
    ``.git`` and the usual caches. The truth ``files_changed`` is computed
    from, whichever runner wrote the files."""
    root = Path(root)
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _HASH_SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def diff_trees(before: dict[str, str], after: dict[str, str]) -> list[FileChange]:
    changes: list[FileChange] = []
    for rel in sorted(set(before) | set(after)):
        if before.get(rel) != after.get(rel):
            changes.append(
                FileChange(path=rel, sha256_before=before.get(rel), sha256_after=after.get(rel))
            )
    return changes


# ---- the gate -----------------------------------------------------------------


def _inside(root: Path, candidate: Path) -> Path | None:
    """The resolved path when it lies inside ``root`` (symlinks followed on
    both sides), else ``None``."""
    base = Path(root).resolve()
    target = candidate if candidate.is_absolute() else base / candidate
    try:
        resolved = target.resolve()
    except OSError:
        return None
    if resolved == base or base in resolved.parents:
        return resolved
    return None


def _under_git_dir(root: Path, resolved: Path) -> bool:
    return ".git" in resolved.relative_to(Path(root).resolve()).parts


class ToolGate:
    """The one gate. ``check`` judges; ``call`` judges, executes, and records
    (the loop runner); ``admit`` and ``settle`` split that in two for a
    runner whose host executes the tool itself (the SDK)."""

    def __init__(self, allowlist: ToolAllowlist, *, transcript: Transcript) -> None:
        self.allowlist = allowlist
        self.transcript = transcript
        self.commands_run: list[CommandRun] = []
        self.refused: list[RefusedCall] = []
        self._seq = 0
        self._last_write_seq = 0
        self._last_pytest_seq = 0
        self._last_pytest_ok = False
        self._pytest_paths: dict[int, tuple[str, ...]] = {}
        self._pending: dict[str, tuple[ToolCall, int]] = {}
        self._verdicts: dict[str, Verdict] = {}

    # -- judging ---------------------------------------------------------------

    def check(self, call: ToolCall) -> Verdict:
        a = self.allowlist
        if isinstance(call, ReadFile | WriteFile | ListFiles):
            if a.files is None:
                return Verdict(False, f"{call.tool} is not in the allowlist (no file tools)")
            return self._check_file(call, a.files)
        if isinstance(call, Shell):
            if a.shell is None:
                return Verdict(False, "shell is not in the allowlist")
            return self._check_shell(call, a.shell)
        if isinstance(call, Git):
            if a.git is None:
                return Verdict(False, "git is not in the allowlist")
            return self._check_git(call, a.git)
        if isinstance(call, Gh):
            if a.gh is None:
                return Verdict(False, "gh is not in the allowlist")
            return self._check_gh(call, a.gh)
        if isinstance(call, Pytest):
            if a.pytest is None:
                return Verdict(False, "pytest is not in the allowlist")
            return self._check_pytest(call, a.pytest)
        return Verdict(False, f"{getattr(call, 'tool', type(call).__name__)} is not a tool")

    def _check_file(self, call: ReadFile | WriteFile | ListFiles, tool: FileTools) -> Verdict:
        if isinstance(call, ListFiles):
            if ".." in Path(call.glob).parts or call.glob.startswith("/"):
                return Verdict(False, f"list_files glob {call.glob!r} leaves the root")
            return ALLOWED
        resolved = _inside(tool.root, Path(call.path))
        if resolved is None:
            return Verdict(False, f"{call.path!r} resolves outside the file root")
        if isinstance(call, WriteFile):
            if not tool.write:
                return Verdict(
                    False, "write_file is not in the allowlist (file tools are read-only)"
                )
            if _under_git_dir(tool.root, resolved):
                return Verdict(False, ".git/ is never written by a tool")
        return ALLOWED

    def _check_shell(self, call: Shell, tool: ShellTool) -> Verdict:
        bad = shell_operator(call.argv)
        if bad:
            return Verdict(False, f"shell operator {bad!r} in argv; one plain argv only")
        for prefix in tool.argv_prefixes:
            if call.argv[: len(prefix)] == prefix:
                return ALLOWED
        return Verdict(
            False,
            f"argv {call.argv[:3]!r} starts with no allowed prefix "
            f"({', '.join(' '.join(p) for p in tool.argv_prefixes) or 'none'})",
        )

    def _check_git(self, call: Git, tool: GitTool) -> Verdict:
        sub = call.args[0]
        if sub.startswith("-"):
            return Verdict(False, f"git option {sub!r} before the subcommand is refused")
        if sub not in GIT_SUBCOMMANDS:
            return Verdict(False, f"git {sub} is not a runner subcommand")
        if sub not in tool.subcommands:
            return Verdict(False, f"git {sub} is not in the allowlist")
        rest = call.args[1:]
        if sub == "push":
            if any(arg in _FORCE_FLAGS or arg.startswith("--force") for arg in rest):
                return Verdict(False, "git push is never forced")
            current = self._current_branch(tool.root)
            if current in (None, "main", "HEAD"):
                return Verdict(False, f"git push from branch {current!r} is refused")
            if rest != ["-u", "origin", current]:
                return Verdict(
                    False, f"git push only as '-u origin {current}' (the current branch)"
                )
        if sub == "switch" and "main" in rest:
            return Verdict(False, "git switch main is refused; the session stays on its branch")
        if sub == "add":
            for arg in rest:
                if not arg.startswith("-") and _inside(tool.root, Path(arg)) is None:
                    return Verdict(False, f"git add {arg!r} is outside the root")
        return ALLOWED

    def _check_gh(self, call: Gh, tool: GhTool) -> Verdict:
        if call.subcommand not in tool.subcommands:
            return Verdict(False, f"gh {call.subcommand} is not in the allowlist")
        if call.subcommand == "pr create":
            if "proposal" in tool.gates:
                paths = self._changed_paths(tool)
                if any(p.startswith(f"{PROPOSAL_DOC_DIR}/") for p in paths):
                    return self._proposal(call, tool, paths)
            if "eval_first" in tool.gates:
                return self._eval_first(tool)
        return ALLOWED

    def _check_pytest(self, call: Pytest, tool: PytestTool) -> Verdict:
        args = list(call.args)
        i = 0
        while i < len(args):
            arg = args[i]
            if arg in PYTEST_FLAGS:
                i += 1
                continue
            if arg in PYTEST_FLAGS_WITH_VALUE:
                value = args[i + 1] if i + 1 < len(args) else None
                want = PYTEST_FLAGS_WITH_VALUE[arg]
                if value is None or (want is not None and value != want):
                    return Verdict(False, f"pytest {arg} {value!r} is not allowed")
                i += 2
                continue
            if arg.startswith("-"):
                return Verdict(False, f"pytest flag {arg!r} is not allowed")
            if _inside(tool.root, Path(arg.split("::")[0])) is None:
                return Verdict(False, f"pytest path {arg!r} is outside the root")
            i += 1
        return ALLOWED

    def _changed_paths(self, tool: GhTool) -> list[str]:
        """Every path this worktree changed against the base, tracked edits
        first and untracked files after, each once."""
        env = reduced_env(self.allowlist.env_passthrough)
        changed = run_subprocess(
            ["git", "diff", "--name-only", tool.base_ref], cwd=tool.root, env=env, timeout=60
        )
        untracked = run_subprocess(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=tool.root,
            env=env,
            timeout=60,
        )
        paths: list[str] = []
        for line in (changed.stdout + untracked.stdout).splitlines():
            path = line.strip()
            if path and path not in paths:
                paths.append(path)
        return paths

    def _proposal(self, call: Gh, tool: GhTool, paths: list[str]) -> Verdict:
        """A proposal PR: one design note, one failing eval, no source.

        Every refusal names the half that failed, because the session has to
        write the reason into its note and route around it."""
        root = Path(tool.root)
        docs = [p for p in paths if p.startswith(f"{PROPOSAL_DOC_DIR}/") and p.endswith(".md")]
        evals = [p for p in paths if _is_proposal_eval(p)]
        others = [p for p in paths if p not in docs and p not in evals]
        if others:
            return Verdict(
                False,
                "proposal: a proposal PR changes nothing but its design note and one failing "
                f"eval; also changed: {', '.join(sorted(others))}",
            )
        if len(docs) != 1:
            return Verdict(
                False,
                f"proposal: exactly one new design note under {PROPOSAL_DOC_DIR}/ "
                f"(found {len(docs)})",
            )
        if len(evals) != 1:
            return Verdict(
                False,
                f"proposal: exactly one failing eval under {PROPOSAL_EVAL_DIR}/ "
                f"(found {len(evals)})",
            )
        doc, eval_path = docs[0], evals[0]
        for path in (doc, eval_path):
            if self._exists_at(tool, path):
                return Verdict(
                    False,
                    f"proposal: {path} is already on {tool.base_ref}; a proposal is written once",
                )
        body = (root / doc).read_text(encoding="utf-8", errors="replace")
        missing = [name for name in PROPOSAL_SECTIONS if not _has_section(body, name)]
        if missing:
            return Verdict(
                False, f"proposal: the design note needs a {' and a '.join(missing)} section"
            )
        found = _CANDIDATE_LINE.search(body)
        if found is None:
            return Verdict(
                False,
                "proposal: the design note needs a 'Candidate: <id>' line naming lens 19's "
                "candidate id",
            )
        candidate = found.group(1)
        prior = _proposal_for(root, candidate, skip=doc)
        if prior is not None:
            return Verdict(
                False,
                f"proposal: candidate {candidate} already has a proposal at {prior}; "
                "a candidate is proposed once",
            )
        if _body_file(call.args) != doc:
            return Verdict(
                False,
                f"proposal: open it with --body-file {doc}; the design note IS the PR body",
            )
        if not self._last_pytest_seq:
            return Verdict(False, "proposal: no pytest run recorded in this session")
        if self._last_pytest_seq < self._last_write_seq:
            return Verdict(False, "proposal: no pytest run since the last write_file")
        if self._last_pytest_ok:
            return Verdict(
                False,
                "proposal: the last pytest run passed; a proposal carries an eval that "
                "fails by design",
            )
        if eval_path not in self._pytest_paths.get(self._last_pytest_seq, ()):
            return Verdict(False, f"proposal: the last pytest run did not name {eval_path}")
        return ALLOWED

    def _exists_at(self, tool: GhTool, path: str) -> bool:
        done = run_subprocess(
            ["git", "cat-file", "-e", f"{tool.base_ref}:{path}"],
            cwd=tool.root,
            env=reduced_env(self.allowlist.env_passthrough),
            timeout=60,
        )
        return done.exit_code == 0

    def _eval_first(self, tool: GhTool) -> Verdict:
        paths = self._changed_paths(tool)
        if not any(_is_test_path(p) for p in paths):
            return Verdict(
                False,
                f"eval_first: no new or changed test file under tests/ or an evals/ directory "
                f"against {tool.base_ref}",
            )
        if not self._last_pytest_seq:
            return Verdict(False, "eval_first: no pytest run recorded in this session")
        if self._last_pytest_seq < self._last_write_seq:
            return Verdict(False, "eval_first: no pytest run since the last write_file")
        if not self._last_pytest_ok:
            return Verdict(False, "eval_first: the last pytest run did not pass")
        return ALLOWED

    def _current_branch(self, root: Path) -> str | None:
        done = run_subprocess(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root,
            env=reduced_env(self.allowlist.env_passthrough),
            timeout=30,
        )
        return done.stdout.strip() or None if done.exit_code == 0 else None

    # -- recording -------------------------------------------------------------

    def refuse(
        self, *, turn: int, tool: str, target: str, reason: str, ref: str | None = None
    ) -> ToolResult:
        self.refused.append(RefusedCall(turn=turn, tool=tool, target=target, reason=reason))
        self.transcript.write(
            "refused",
            {
                "turn": turn,
                "tool": tool,
                "target": target,
                "argv": target.split(),
                "reason": reason,
                "ref": ref,
            },
        )
        if ref is not None:
            self._verdicts[ref] = Verdict(False, reason)
        return ToolResult(tool=tool, ok=False, refused_reason=reason)

    def admit(self, call: ToolCall, *, turn: int, ref: str | None = None) -> Verdict:
        """Judge and record one call without executing it. On allow, the
        write and pytest ledgers advance so ``eval_first`` sees them; the
        host's result arrives later through :meth:`settle`."""
        if ref is not None and ref in self._verdicts:
            return self._verdicts[ref]
        verdict = self.check(call)
        if not verdict.allowed:
            self.refuse(
                turn=turn, tool=call.tool, target=call_target(call), reason=verdict.reason, ref=ref
            )
            return verdict
        self._seq += 1
        if isinstance(call, WriteFile):
            self._last_write_seq = self._seq
        if isinstance(call, Pytest):
            self._pytest_paths[self._seq] = pytest_paths(call.args)
        payload = {"turn": turn, "tool": call.tool, "ref": ref, "seq": self._seq}
        if call.tool in COMMAND_TOOLS:
            payload["argv"] = call_argv(call)
        else:
            payload["target"] = call_target(call)
        self.transcript.write("tool_call", payload)
        if ref is not None:
            self._verdicts[ref] = verdict
            self._pending[ref] = (call, self._seq)
        return verdict

    def verdict_for(self, ref: str) -> Verdict | None:
        return self._verdicts.get(ref)

    def settle(self, ref: str, *, ok: bool, output: str = "") -> None:
        """A host-executed call finished. Command tools become a
        :class:`CommandRun` (exit 0 on success, 1 on a reported error: the
        host reports a flag, not a code); a pytest updates the eval ledger."""
        entry = self._pending.pop(ref, None)
        if entry is None:
            return
        call, seq = entry
        exit_code = 0 if ok else 1
        if call.tool in COMMAND_TOOLS:
            self.commands_run.append(
                CommandRun(
                    tool=call.tool,
                    argv=call_argv(call),
                    cwd=str(self._root_of(call)),
                    exit_code=exit_code,
                    seconds=0.0,
                )
            )
        if isinstance(call, Pytest):
            self._last_pytest_seq = seq
            self._last_pytest_ok = ok
        self.transcript.write(
            "tool_result",
            {"ref": ref, "tool": call.tool, "ok": ok, "exit_code": exit_code, "output": output},
        )

    # -- executing (the loop runner) --------------------------------------------

    def call(self, call: ToolCall, *, turn: int) -> ToolResult:
        verdict = self.check(call)
        if not verdict.allowed:
            return self.refuse(
                turn=turn, tool=call.tool, target=call_target(call), reason=verdict.reason
            )
        self.admit(call, turn=turn)
        result = self.execute(call)
        self.transcript.write(
            "tool_result",
            {
                "turn": turn,
                "tool": call.tool,
                "ok": result.ok,
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )
        return result

    def execute(self, call: ToolCall) -> ToolResult:
        """Run an already-admitted call. Never call this without ``check``."""
        a = self.allowlist
        if isinstance(call, ReadFile):
            path = _inside(a.files.root, Path(call.path))
            if path is None or not path.is_file():
                return ToolResult(
                    tool=call.tool, ok=False, exit_code=1, stderr=f"{call.path}: no such file"
                )
            return ToolResult(
                tool=call.tool,
                ok=True,
                exit_code=0,
                stdout=path.read_text(encoding="utf-8", errors="replace"),
            )
        if isinstance(call, WriteFile):
            path = _inside(a.files.root, Path(call.path))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(call.content, encoding="utf-8")
            return ToolResult(
                tool=call.tool, ok=True, exit_code=0, stdout=f"wrote {len(call.content)} characters"
            )
        if isinstance(call, ListFiles):
            root = Path(a.files.root).resolve()
            found = sorted(
                p.relative_to(root).as_posix()
                for p in root.glob(call.glob)
                if p.is_file() and ".git" not in p.relative_to(root).parts
            )
            return ToolResult(tool=call.tool, ok=True, exit_code=0, stdout="\n".join(found))
        argv = call_argv(call)
        root = self._root_of(call)
        timeout = self._timeout_of(call)
        done = run_subprocess(argv, cwd=root, env=reduced_env(a.env_passthrough), timeout=timeout)
        self.commands_run.append(
            CommandRun(
                tool=call.tool,
                argv=argv,
                cwd=str(root),
                exit_code=done.exit_code,
                seconds=round(done.seconds, 3),
            )
        )
        if isinstance(call, Pytest):
            self._last_pytest_seq = self._seq
            self._last_pytest_ok = done.exit_code == 0
        return ToolResult(
            tool=call.tool,
            ok=done.exit_code == 0,
            exit_code=done.exit_code,
            stdout=done.stdout,
            stderr=done.stderr,
        )

    def _root_of(self, call: ToolCall) -> Path:
        a = self.allowlist
        tool = {"shell": a.shell, "git": a.git, "gh": a.gh, "pytest": a.pytest}.get(call.tool)
        if tool is None:
            tool = a.files
        return Path(tool.root)

    def _timeout_of(self, call: ToolCall) -> int:
        a = self.allowlist
        if isinstance(call, Shell):
            return a.shell.max_seconds
        if isinstance(call, Pytest):
            return a.pytest.max_seconds
        return 600


def shell_operator(argv: list[str], *, shell: bool = False) -> str | None:
    """The first token that is a shell operator, else ``None``. With
    ``shell=True`` (a line a real shell will run, the SDK's Bash) any token
    CONTAINING an operator character counts, since ``shlex`` leaves ``ok;``
    as one token; without it (an argv executed with no shell) only whole
    operator tokens and command-substitution fragments count, so a
    ``python -c`` argument with a ``;`` inside stays one plain argument."""
    fragments = _SHELL_OPERATOR_FRAGMENTS if shell else _SUBSTITUTION_FRAGMENTS
    for token in argv:
        if token in _SHELL_OPERATORS:
            return token
        if any(fragment in token for fragment in fragments):
            return token
    return None


def pytest_paths(args: list[str]) -> tuple[str, ...]:
    """The paths a pytest call named, flags and their values dropped."""
    found: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in PYTEST_FLAGS_WITH_VALUE:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        found.append(arg)
        i += 1
    return tuple(found)


def _is_proposal_eval(path: str) -> bool:
    name = Path(path).name
    return (
        path.startswith(f"{PROPOSAL_EVAL_DIR}/")
        and name.startswith("test_")
        and name.endswith(".py")
    )


def _has_section(body: str, name: str) -> bool:
    pattern = rf"^\s{{0,3}}#{{1,6}}\s+{re.escape(name)}\b"
    return re.search(pattern, body, re.M | re.I) is not None


def _body_file(args: list[str]) -> str | None:
    """The ``--body-file`` value in a ``gh pr create`` argv, either form."""
    for i, arg in enumerate(args):
        if arg == "--body-file" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--body-file="):
            return arg.split("=", 1)[1]
    return None


def _proposal_for(root: Path, candidate: str, *, skip: str) -> str | None:
    """The design note already answering this candidate id, if any."""
    folder = Path(root) / PROPOSAL_DOC_DIR
    if not folder.is_dir():
        return None
    for path in sorted(folder.glob("*.md")):
        relative = path.relative_to(Path(root)).as_posix()
        if relative == skip:
            continue
        found = _CANDIDATE_LINE.search(path.read_text(encoding="utf-8", errors="replace"))
        if found is not None and found.group(1) == candidate:
            return relative
    return None


def _is_test_path(path: str) -> bool:
    parts = Path(path).parts
    return any(part in ("tests", "evals") for part in parts[:-1]) or Path(path).name.startswith(
        "test_"
    )
