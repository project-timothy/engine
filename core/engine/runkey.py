"""Declarative run keys and the undeclared-input audit (issue #153).

Invariant 3 says every job is safe to re-run because its run key is a digest
of its inputs. Five separate incidents this summer were one disease: a key
that missed an input (the vendor registry, a config mapping, the resolved
month, a code version), so a changed input replayed the prior result
(#117, #121, #135, #137, #143). Each fix patched one key by hand.

This module makes the inputs a declaration instead of a hand-rolled hash:

* :class:`RunKey` — a job lists what its run depends on (params, config
  paths, files, the vendor registry, arbitrary values, a code version) and
  the key is a uniform digest of those parts. Every ``add`` also *declares*
  the input.
* The input trail — while the runner executes ``key(ctx)`` and ``run(ctx)``
  it traces what the job actually reads: tenant-config leaf attributes
  (through :class:`TracedConfig`), ``ctx.params`` lookups (through
  :class:`TracedParams`), and vendor-registry loads (``touch``).
* :func:`undeclared` — the audit: every input ``run`` touched must be
  declared by ``key`` (or read by ``key`` itself, which makes it part of the
  digest by construction). Under ``ENGINE_KEY_AUDIT=strict`` (the test
  suite, via the root conftest) an undeclared input turns the run into an
  error result, so a job that grows a new config read without folding it
  into its key fails its own evals. Production runs leave the variable unset
  and never trace.

Declaration coverage is by dotted prefix: declaring ``expenses`` covers
``expenses.category_accounts``; declaring a leaf covers exactly that leaf.
"""

from __future__ import annotations

import contextvars
import errno
import hashlib
import json
import os
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

KEY_AUDIT_ENV = "ENGINE_KEY_AUDIT"
KEY_AUDIT_LOG_ENV = "ENGINE_KEY_AUDIT_LOG"  # optional: append "agent/job: inputs" lines here
CLOUD_ONLY = "cloud-only"
MISSING = "missing"


# ---------- the trail ---------------------------------------------------------


class InputTrail:
    """What one phase (key or run) read and declared."""

    __slots__ = ("declared", "touched")

    def __init__(self) -> None:
        self.touched: set[str] = set()
        self.declared: set[str] = set()


_TRAIL: contextvars.ContextVar[InputTrail | None] = contextvars.ContextVar(
    "engine_input_trail", default=None
)


def touch(path: str) -> None:
    """Record that the active phase read ``path`` (no-op when not tracing)."""
    trail = _TRAIL.get()
    if trail is not None:
        trail.touched.add(path)


def declare(path: str) -> None:
    trail = _TRAIL.get()
    if trail is not None:
        trail.declared.add(path)


@contextmanager
def tracing(trail: InputTrail) -> Iterator[InputTrail]:
    token = _TRAIL.set(trail)
    try:
        yield trail
    finally:
        _TRAIL.reset(token)


def audit_mode() -> str:
    return os.environ.get(KEY_AUDIT_ENV, "").strip().lower()


class UndeclaredInputError(RuntimeError):
    """A job's run read inputs its run key does not declare (issue #153)."""


def _covered(path: str, declared: set[str]) -> bool:
    for d in declared:
        if path == d or path.startswith(d + "."):
            return True
    return False


def log_undeclared(agent: str, job: str, missing: list[str]) -> None:
    """Append an audit line when ENGINE_KEY_AUDIT_LOG names a file (a
    migration aid: one suite run lists every job's undeclared inputs)."""
    target = os.environ.get(KEY_AUDIT_LOG_ENV)
    if target and missing:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(f"{agent}/{job}: {', '.join(missing)}\n")


def undeclared(key_trail: InputTrail, run_trail: InputTrail) -> list[str]:
    """Inputs ``run`` touched that ``key`` neither declared nor read."""
    declared = key_trail.declared | key_trail.touched
    return sorted(t for t in run_trail.touched if not _covered(t, declared))


# ---------- traced views ------------------------------------------------------


class TracedConfig:
    """Attribute proxy over a pydantic config model that records leaf reads.

    ``ctx.tenant.expenses.category_accounts`` touches
    ``expenses.category_accounts``; nested models come back wrapped so the
    dotted path grows; anything else (str, list, dict, ...) is returned raw.
    Pydantic's own ``model_*`` methods count as reading the whole model.
    """

    __slots__ = ("_obj", "_prefix")

    def __init__(self, obj: BaseModel, prefix: str = "") -> None:
        object.__setattr__(self, "_obj", obj)
        object.__setattr__(self, "_prefix", prefix)

    def __getattr__(self, name: str) -> Any:
        obj = object.__getattribute__(self, "_obj")
        prefix = object.__getattribute__(self, "_prefix")
        value = getattr(obj, name)
        if name.startswith("_"):
            return value
        if name.startswith("model_") and callable(value):
            touch(prefix or "*")
            return value
        path = f"{prefix}.{name}" if prefix else name
        if isinstance(value, BaseModel):
            return TracedConfig(value, path)
        touch(path)
        return value

    def __setattr__(self, name: str, value: Any) -> None:  # pragma: no cover - config is read-only
        raise AttributeError("tenant config is read-only inside a job")

    def __repr__(self) -> str:
        return f"TracedConfig({object.__getattribute__(self, '_obj')!r})"


def unwrap(obj: Any) -> Any:
    """The raw model behind a :class:`TracedConfig` (identity otherwise)."""
    if isinstance(obj, TracedConfig):
        return object.__getattribute__(obj, "_obj")
    return obj


class TracedParams(dict):
    """A ``ctx.params`` view that records every lookup as ``param:<name>``."""

    def __getitem__(self, key: str) -> Any:
        touch(f"param:{key}")
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        touch(f"param:{key}")
        return super().get(key, default)

    def __contains__(self, key: object) -> bool:
        touch(f"param:{key}")
        return super().__contains__(key)

    def items(self):
        touch("param:*")
        return super().items()

    def keys(self):
        touch("param:*")
        return super().keys()

    def values(self):
        touch("param:*")
        return super().values()

    def __iter__(self):
        touch("param:*")
        return super().__iter__()


# ---------- the builder -------------------------------------------------------


def _jsonable(obj: Any) -> Any:
    obj = unwrap(obj)
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, set | frozenset):
        return sorted(_jsonable(x) for x in obj)
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return hashlib.sha256(obj).hexdigest()
    raise TypeError(f"run key cannot encode {type(obj).__name__}")


def canonical(obj: Any) -> str:
    """A canonical JSON rendering: same value, same text, regardless of order
    of dict insertion. Lists keep their order (callers sort where order is
    incidental)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_jsonable)


def file_digest(path: Path) -> str:
    """Content digest of a file, or a stable sentinel when the bytes are not
    readable here: ``cloud-only`` for a dataless placeholder (EDEADLK, the
    2026-07-09 lesson) and ``missing`` for a path that is not a file. The
    sentinel keeps the key total; the moment content lands the digest
    replaces it and the run re-fires."""
    path = Path(path)
    if not path.is_file():
        return MISSING
    try:
        data = path.read_bytes()
    except OSError as exc:
        if exc.errno == errno.EDEADLK:
            return CLOUD_ONLY
        raise
    return hashlib.sha256(data).hexdigest()


def _resolve(root: Any, dotted: str) -> Any:
    node = root
    for part in dotted.split("."):
        node = getattr(node, part)
    return node


class RunKey:
    """Declare a job's inputs once; get a uniform digest.

    Parts are recorded in call order, so a job's key function reads as its
    input list. The tenant slug, job name, code version, and shadow/live
    mode are always the first four parts.
    """

    def __init__(self, ctx: Any, job: str, *, version: str = "1") -> None:
        self._ctx = ctx
        self._parts: list[str] = [
            f"tenant:{ctx.tenant_slug}",
            f"job:{job}",
            f"version:{version}",
            "mode:" + ("shadow" if ctx.shadow else "live"),
        ]

    # -- parts -------------------------------------------------------------

    def add(self, label: str, value: Any) -> RunKey:
        """A raw part. ``value`` is canonicalized (models, paths, sets ok)."""
        self._parts.append(f"{label}={canonical(value)}")
        return self

    def param(self, name: str, default: Any = "") -> Any:
        """Fold a ``--param`` into the key and return its value."""
        value = self._ctx.params.get(name, default)
        declare(f"param:{name}")
        self.add(f"param:{name}", "" if value is None else str(value))
        return value

    def env(self, var: str, default: str = "") -> str:
        """Fold an environment override into the key and return it."""
        value = os.environ.get(var, default)
        self.add(f"env:{var}", value)
        return value

    def config(self, *paths: str) -> RunKey:
        """Fold tenant-config values (dotted paths) into the key and declare
        them. A section path (``expenses``) covers every leaf beneath it."""
        for path in paths:
            value = _resolve(self._ctx.tenant, path)
            declare(path)
            self.add(f"config:{path}", value)
        return self

    def ignore(self, *paths: str, reason: str) -> RunKey:
        """Declare config the run reads but the key deliberately omits.
        ``reason`` is documentation at the call site; it is not hashed."""
        if not reason.strip():
            raise ValueError("ignore() needs a reason")
        for path in paths:
            declare(path)
        return self

    def file(self, path: Path | str, *, label: str | None = None) -> RunKey:
        p = Path(path)
        self.add(f"file:{label or p.name}", file_digest(p))
        return self

    def files(
        self,
        paths: Iterable[Path | str],
        *,
        label: str = "files",
        digest: Callable[[Path], str | None] | None = None,
    ) -> RunKey:
        """A set of files by name + content (order-independent). ``digest``
        lets an agent route through its own read seam (evals simulate a
        cloud-only placeholder there); ``None`` from it means cloud-only."""
        fn = digest or file_digest
        entries = sorted(f"{Path(p).name}:{fn(Path(p)) or CLOUD_ONLY}" for p in paths)
        self.add(label, entries)
        return self

    def vendors(self, registry: BaseModel) -> RunKey:
        """The vendor registry is a run input wherever it is loaded (#135,
        #117, the 2026-07-20 lesson): the whole registry, uniformly."""
        declare("registry:vendors")
        self.add("registry:vendors", registry)
        return self

    def value(self, label: str, obj: Any) -> RunKey:
        return self.add(label, obj)

    def rows(self, label: str, rows: Iterable[Any]) -> RunKey:
        """Ledger rows (tuples, dicts, Row objects) in the order given."""
        self.add(label, [list(r) if isinstance(r, tuple) else _row(r) for r in rows])
        return self

    def stamp(self) -> RunKey:
        """Every look is a fresh look: a second-granularity wall-clock part
        for jobs that must never replay (close preflight/packet)."""
        self.add("stamp", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"))
        return self

    # -- output ------------------------------------------------------------

    @property
    def parts(self) -> list[str]:
        return list(self._parts)

    def digest(self) -> str:
        return hashlib.sha256("\n".join(self._parts).encode("utf-8")).hexdigest()


def _row(r: Any) -> Any:
    if hasattr(r, "keys"):  # sqlite3.Row or dict
        return {k: r[k] for k in r.keys()}
    return r
