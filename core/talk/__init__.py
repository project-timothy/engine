"""Tim's own voice (Standalone Tim, shot 1).

Until now the conversation was held by a person's own Claude or ChatGPT,
which called Tim's tools through the door. ``core.talk`` holds it on the
box: the person's words in, the same tools called as them through
``Tools.call``, Tim's words out. The door for a person's own AI stays.

- :mod:`core.talk.loop`: one message, one reply (:func:`reply`).
- :mod:`core.talk.store`: the conversation, kept in files beside the books
  for 90 days (:class:`Conversation`), and what waits on her tap
  (:class:`Waiting`).
- :mod:`core.talk.hold`: which calls run and which wait for her own tap
  (card V2); :func:`tapped` runs a held call on her Yes.
- :mod:`core.talk.meter`: every model call priced and counted against the
  ministry's monthly cap, outside any job (:class:`Meter`); the soft line
  and the month's spend by job type (``engine spend``, card V3).
- :mod:`core.talk.speeds`: which model a message runs on, by rule (card V3).

Boundary classification (docs/boundary-rules.md): the model chooses which
tool to call and puts the answer in words; every figure, every write, and
every permission check stays in the tools (invariant 2).
"""

from .loop import JOB, Reply, Turn, reply, tapped, words
from .meter import Meter
from .speeds import voice_on, voice_on_for
from .store import Conversation, Waiting

__all__ = [
    "JOB",
    "Conversation",
    "Meter",
    "Reply",
    "Turn",
    "Waiting",
    "reply",
    "tapped",
    "voice_on",
    "voice_on_for",
    "words",
]
