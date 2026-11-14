# Art direction and house style

Use for: any new piece, keeping a project's art consistent, critiquing art (yours, GPT's or the user's).

## Settle the brief first (one line, then go)
Subject · purpose (token, portrait, map, concept for a mini, game asset, texture) · style · aspect/size · must-haves.
If the project already has a style guide (project memory or a doc), follow it without asking.

## House style guide (write one per project once the user likes a direction; save it to project memory with REMEMBER)
1. Medium and rendering: e.g. "painterly digital oil, visible brushwork, soft edges" or "clean ink line + flat colour".
2. Palette: 5-8 named colours with roles (shadows, highlights, accent). Say what's forbidden (e.g. no neon).
3. Lighting: key light direction and mood (e.g. "low warm key from the left, cool rim").
4. Shapes and proportions: realistic / heroic (8-head) / stylised (6-head, large hands).
5. Line and texture: line weight, paper or canvas grain, level of detail.
6. Composition habits: framing, camera height, negative space.
7. A "style suffix": one sentence you append to every PAINT prompt in this project.
8. Reference picks: the 2-3 best pieces so far (file names) that define the look.

## Consistency tricks
- Reuse the style suffix word for word. Keep the seed for variations of the same subject.
- Character sheets: paint the same character with the same descriptive sentence (face, hair, build, outfit, colours)
  every time; a "turnaround" (front, side, back on one canvas, plain background) is the best reference for 3D.
- Rework instead of re-rolling: PAINT ... | from last | strength 0.3-0.5 for small changes, 0.6-0.8 for bigger ones.

## Critique checklist (say it plainly, best first)
Read at a glance (silhouette, focal point) · anatomy and hands · perspective and scale · lighting is consistent ·
palette matches the style guide · details that are wrong for the setting (lore!) · artefacts (extra fingers, melted
text, merged objects). Then one concrete fix per problem and redo the piece if it isn't right.

## D&D specifics
- Tokens: square canvas, subject centred, head and shoulders or full body filling ~80%, plain or transparent-friendly
  background; the user's VTT crops a circle. 512x512 or 1024x1024.
- Battle maps: top-down, flat even lighting, no perspective, grid-friendly; tell the user the intended grid
  (e.g. 1024 px = 20 squares of ~51 px) so they can align it. Paint 1216x832 or 1024x1024, upscale outside if needed.
- Portraits for handouts: 832x1216, rich background, mood lighting.
