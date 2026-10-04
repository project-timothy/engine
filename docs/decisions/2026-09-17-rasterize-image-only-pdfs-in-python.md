# Rasterize image-only PDFs in Python, with pypdfium2
Date: 2026-09-17
Type: One-way door

A new external dependency is the owner's call (CLAUDE.md, PRD section 8). This
is that call, taken on 2026-09-17 with the go for issue #296.

## What it buys

A scanned PDF is a picture of a page wrapped in PDF structure. Nothing in the
engine could turn one into something a model looks at, so each adapter failed
on it differently:

- the Claude Agent SDK seat rendered its own pages by shelling out, which is
  the single reason that session kept `Bash` when issue #265 was settled on
  2026-09-17. `allowed_tools` pre-approves rather than restricts, so a session
  with a shell is a session that can do anything on this host, and the honest
  thing to write in the design note was that the confinement was an
  instruction in a prompt;
- `anthropic_messages` leaned on the provider rasterizing server-side;
- `openai_compat` had nothing at all. A small open model behind vLLM or Ollama
  has no PDF reader, so a scanned invoice on the local tier was not a weak
  extraction, it was no extraction. That is a phase 9 blocker: the product
  cannot claim a model seam that only works when the model is Claude.

With the ENGINE rendering, every adapter sends pictures, and the seat drops to
`Read` with `Bash` refused outright through `disallowed_tools`.

## The dependency

`pypdfium2 == 5.13.0`, a core dependency (not an extra: the AP path is the
main path, and a host with no renderer would silently send scans nobody can
read).

- **Licence.** BSD-3-Clause and Apache-2.0. The Python API is dual licensed
  Apache-2.0 or BSD-3-Clause; the PDFium binary inside the wheel is
  BSD-3-Clause, the same Chromium PDF engine every browser ships. Compatible
  with AGPL-3.0 distribution of the core (PRD v2, the open-product plan).
- **No system package.** The wheel carries the renderer, so nothing is added
  to the Dockerfile, to `engine doctor`, or to any install document.
- **Wheels for the boxes this runs on.** `macosx_13_0_arm64` (the Mini),
  `manylinux_2_17_x86_64` and `manylinux_2_17_aarch64` (the container on
  either architecture), plus musl builds for an Alpine host that is not ours.
  All are `py3-none`, so a Python upgrade needs no new wheel.

## What was rejected

**`pdftoppm` (poppler-utils) run by the engine as a subprocess.** No Python
dependency, and it is what the live sessions used. It is a SYSTEM package: the
Dockerfile installs it, `engine doctor` has to report it missing, every
install document names it, and a host that lacks it goes back to sending
unreadable PDFs with no way to tell from the code that it has. The issue
priced both; this is the one that survives an install on somebody else's box.

**Pillow, to write the PNG.** PDFium hands back a raw bitmap and the usual way
to save it is `bitmap.to_pil().save(...)`. That is a second dependency, a large
one, for the simplest case of a format the standard library already has the
two pieces of: `zlib` deflates, `struct` packs, and a CRC comes off
`zlib.crc32`. `core/llm/rasterize.py` writes 8-bit RGB (or greyscale), filter
type 0, one IDAT, and a test reads the file back chunk by chunk and checks
every checksum.

## The numbers, and why

- **150 DPI.** A US Letter page comes out 1275 by 1650, near the long edge
  Claude's vision stack works at and past what reading 8 point type needs. 72
  is the PDF's own unit and unreadable; 300 quadruples the bytes for detail no
  invoice carries.
- **6 pages.** An AP document is an invoice, a receipt, or a statement page,
  and the live 2026 run is one to three pages. Six covers the long ones and
  stops a 300-page scan from filling a disk. What is cut is named in the note
  the model reads, so it reports the gap instead of guessing.
- **12 MB of PNG per document.** The seat reads the pages back through the CLI
  transport, which base64-inflates by about 4/3: 12 MB becomes roughly 16 MB
  against the adapter's 32 MB buffer, leaving the transcript room
  (docs/lessons.md, "Oversize inputs fail fast"). The API adapters inline the same bytes in a
  request body. The first page always renders whatever the budget says.

## What stays the owner's

`MAX_TURNS` is still 12. The issue allows it back down now that the model
spends no turns rendering, but the evidence for 12 was live
(docs/lessons.md, "Budget for the hardest input") and lowering it wants live evidence too, not an
argument. It is a one-line change on a coding day once a week of 08:00 runs
has been through the new path.
