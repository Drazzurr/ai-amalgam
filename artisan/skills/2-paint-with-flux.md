# Painting with FLUX.1 dev (PAINT:)

## Prompt shape (natural sentences, most important first)
"<subject and action>, <key details: clothing, materials, colours>, <setting>, <lighting>, <camera/composition>,
<style/medium suffix>."
- Be concrete: "a weathered half-orc blacksmith in a leather apron, soot on her forearms" beats "a cool orc".
- FLUX follows long prompts well (up to ~3 sentences) and can render short text in quotes ("a sign that says 'The
  Drunken Wyrm'"). It has no negative prompt: describe what you want instead ("plain light grey background").
- Composition words that work: "full body", "three-quarter view", "close-up portrait", "wide establishing shot",
  "top-down view", "centered", "rule of thirds", "shallow depth of field".

## Sizes (multiples of 16, ~1 megapixel)
1024x1024 square · 832x1216 portrait · 1216x832 landscape · 896x1152 / 1152x896 · 768x1344 tall banner.
Bigger than ~1.5 MP is slower on a 12 GB card and FLUX starts repeating things.

## Variations and fixes
- Same seed + small prompt change = controlled variation. New seed = new composition.
- PAINT: <changed prompt> | from last | strength 0.35 → keeps layout and colours, changes details.
- Strength 0.7+ keeps only the rough layout. Use it to restyle a sketch the user attached ("from <file name>").

## Timing on the user's RTX 4070
~25-40 s per 1024px picture once loaded; the first one after a break also loads the model (up to a minute or two).
Make 1-2 pictures, look, then refine; don't fire off many at once.
