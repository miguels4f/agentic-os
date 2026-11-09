# Map specification

The renderer accepts UTF-8 JSON with this shape:

```json
{
  "title": "How a new customer gets onboarded",
  "subtitle": "From sign-up to the first useful result",
  "width": 1400,
  "height": 850,
  "nodes": [
    {
      "id": "signup",
      "x": 70,
      "y": 170,
      "w": 250,
      "h": 150,
      "step": "1",
      "title": "Customer signs up",
      "body": "Provides the minimum information needed to begin.",
      "kind": "entry",
      "badge": "OBSERVED",
      "details": [
        {"label": "Executor", "value": "Public sign-up form"},
        {"label": "Input contract", "value": "name, email, consent"},
        {"label": "Output contract", "value": "validated registration request"},
        {"label": "Restrictions", "value": "required fields; rate limit; no payment data"},
        {"label": "Handoff", "value": "request is stored, then the validation worker is notified"}
      ]
    }
  ],
  "edges": [
    {
      "from": "signup",
      "to": "validate",
      "label": "details submitted",
      "style": "solid"
    }
  ],
  "notes": [
    {
      "x": 950,
      "y": 610,
      "w": 350,
      "title": "Why this matters",
      "body": "Bad input is stopped before it creates cleanup work."
    }
  ],
  "legend": true
}
```

## Fields

Top-level `title`, `nodes`, and `edges` are required. `subtitle`, `width`, `height`, `notes`, and `legend` are optional. The default canvas is 1400 × 850.

Each node requires `id`, `x`, `y`, `title`, and `body`. Width and height default to 250 × 145. `step` is optional. `kind` controls color and may be `entry`, `process`, `storage`, `external`, or `risk`. `badge` is optional and should normally be `OBSERVED`, `INFERRED`, or `UNKNOWN`.

`details` is an optional list of `{label, value}` rows rendered below the body. Use architecture labels such as `Executor`, `Skill / procedure`, `Input contract`, `Output contract`, `Restrictions`, `Handoff`, and `Evidence`. Do not add empty or repetitive rows. Detailed nodes usually need a height of 360–520 px and a width of at least 430 px. The renderer raises an error instead of silently clipping details that exceed the node height.

Each edge requires `from` and `to`, referencing node IDs. `label` is optional. `style` may be `solid` or `dashed`. The renderer connects the closest sensible horizontal or vertical sides of the two boxes.

Each note requires `x`, `y`, and `body`. `title`, `w`, and `h` are optional.

## Layout guidance

- Leave at least 70 px between boxes.
- Reserve roughly 120 px at the top for title and subtitle.
- Keep edge labels away from node borders.
- Use a clear reading order. Place failure paths below the happy path when possible.
- Increase the canvas instead of squeezing long labels.
- Aim for no more than 45 characters in a node title and 110 characters in its body.
- A title wraps at about (node width − 70) ÷ 10 characters; each extra title line pushes the body down about 23 px, and the renderer refuses a node whose text no longer fits. Widen or heighten the node rather than shortening meaning away.
- Edge labels sit in a 148 px box centred on the arrow. When boxes are only 70–90 px apart, keep labels to about 18 characters or they hide behind the nodes.
- In a detail map, prefer 2–3 wide columns or one vertical sequence. Keep at least 90 px between detailed cards so arrows and contract labels remain readable.
- Put the handoff payload on the edge label and the complete handoff rule in the source node's `details`.

The SVG automatically wraps node and note text. It does not automatically rearrange nodes, so visual inspection is required.
