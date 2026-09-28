---
name: draw
description: Draw one cited architecture diagram (C4 context, C4 containers, cloud deployment, data flow with trust boundaries and PII, or a sequence for a named flow) from the spine, challenging its gaps first
code: DG
added: 2026-09-28
type: prompt
---

# Draw a diagram

There is nothing to draw until the architecture exists: with no spine, say `bmad-architecture` comes first (and the principles before it, capability AP), and stop. With several spines, ask which one.

For a new diagram, ask the paper size (A4, A3 or Letter) and orientation, defaulting to A4 portrait, and record the answer in the model's `page`; a redraw keeps the model's `page` unless {user_name} changes it. Ask which type if they didn't say, and for a sequence, which flow.

The result is `<name>.model.json` and `<name>.drawio` in the diagrams folder, plus `.png` and `.svg` when draw.io desktop is installed. `<name>` is the type (`c4-context`, `c4-containers`, `deployment`, `data-flow`) or `sequence-<flow>`.

Load the type's reference for its elements and what each needs as evidence:

| Type | Reference |
| --- | --- |
| C4 system context | `references/c4-context.md` |
| C4 containers | `references/c4-containers.md` |
| Cloud deployment | `references/deployment.md` |
| Data flow, trust boundaries, PII/PHI | `references/data-flow.md` |
| Sequence for a named flow | `references/sequence.md` |

Get the spine's ADs with `uv run scripts/spine.py {project-root} --spine <spine>` and read the sources. Any `duplicates` it reports are a defect in the spine (one id defined twice): raise them before anything else, because citations to that id are ambiguous.

**Challenge before drawing.** A diagram is only as good as the architecture behind it, and the cheapest moment to fix the architecture is before it's drawn. Write the model first (`uv run scripts/build-drawio.py --help` gives its schema), or open the existing one on a redraw: it's where the challenge keeps its state, so nothing agreed or deferred depends on the conversation surviving. Then put to the user what this diagram needs and the documents don't settle: the gaps, the contradictions between ADs, and the choices that look wrong for what the system has to do (a database placed inside a cluster, "multi-region" with no data replication stated, an admin path with no separation). Include anything the diagram would show breaking a principle in `architecture.md` (an AD that departs from one without saying why). Take the most consequential first, one or two at a time, and say briefly why each matters. Where you think a choice is mistaken, say so and why; the user decides.

Each answer becomes AD text: an amendment to the AD it concerns, or a new AD numbered `next_ad`. Show the exact wording and add it to the spine only once the user approves it. If the spine is in git, remind them the change is uncommitted, since the diagram's stamp records that. Anything the user defers goes into the model's gaps as soon as they defer it. After each round, say how many open points remain and ask whether to keep going or draw now with the rest as gaps.

On a redraw, the model's existing gaps count as already raised: list them once, and challenge only what the drift check names (ADs changed, added or removed since the diagram's stamp) and what those changes newly open.

Then finish the model from the spine as amended. The model is the diagram's source: update it rather than starting over, and keep the gaps that still hold. Then run `uv run scripts/build-drawio.py {project-root} <folder>/<name>.model.json`. It checks every cite before drawing anything (each AD exists in the spine, each cited section exists in its file), and the org drawing rules, and lists every problem. Fix them in the model, and never by giving an element a cite its source doesn't support: an element with nothing to cite moves to the gaps. Replacing an existing `.drawio` needs `--force`, and loses any hand edits in it, so ask first.

The build fits the diagram to the chosen paper on one page, or across two when one would shrink it too far, and says which in `page`. If `page.readable` comes back false, the text would print too small on the chosen paper: offer `page.suggest`, the smallest paper, orientation and page count that carries it readably, and rebuild on the user's choice. Offer a split (one region in detail and the rest summarised, one containers diagram per system boundary, one data flow per trust boundary) only when the user would rather keep the smaller paper, and never squeeze the layout. If the build refuses a diagram because something would still overlap, shorten the longest labels or split the diagram.

When the draw.io MCP server is connected, offer to open the finished diagram with `open_drawio_xml` for a preview. Tell the user that layout touch-ups belong in draw.io but content changes belong in the model, because the next rebuild regenerates the file from it.

Report the files written, the exports (or why they were skipped), and the gaps, each phrased as the question the architecture still has to answer.
