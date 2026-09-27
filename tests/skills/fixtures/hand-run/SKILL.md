---
name: hand-run
description: The smallest real skill, written for row 7.17's hand run: read the one item in the context bundle and answer with a one-paragraph note. No tools, no repository edits, no network beyond the model itself.
---

# Hand run

You are a runner session with NO tools. Everything you need is already in
the context bundle below: one file, quoted in full.

Do exactly this:

1. Read the bundle item named `subject`.
2. Reply with ONE message that is the note, in this shape:

```
# Hand run note

<one paragraph, three sentences at most, saying what the file is and what it
claims. Name one concrete detail from it so the reader can tell you read it.>
```

Rules:

- The note is your final message. Nothing else is the deliverable.
- Never write a file, run a command, or ask for a tool: there are none, and
  a request for one costs a turn and is refused.
- The first line must never carry the word preliminary: the runner treats a
  note that does as an unfinished run.
