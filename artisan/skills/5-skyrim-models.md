# Skyrim-ready models (SE/AE)

## Tools
Blender with PyNifly (BadDogSkyrim's add-on; imports and exports Skyrim LE/SE and Fallout 4 nifs, skinned and
unskinned) · NifSkope to inspect · Outfit Studio / BodySlide for armour on bodies · the Package mod button to ship.

## Scale and size
- Import a vanilla mesh of the same kind (READ/extract it from the game's BSA) and match its size; don't guess units.
  A human is about 128 Skyrim units tall.
- Rough triangle budgets: small clutter 300-2k · weapons 2-8k · shields 2-6k · armour pieces 8-30k · heads and
  followers' outfits up to ~40k total · statics as needed but use LODs for big ones.

## Textures (DDS, power-of-two sizes: 512, 1024, 2048; 4096 only for big hero assets)
| Map | Suffix | Notes | Compression |
|---|---|---|---|
| Diffuse (base colour, baked AO) | none | alpha = opacity if needed | BC1 (no alpha) or BC7 (alpha) |
| Normal | _n | DirectX style (Blender is OpenGL: flip the green channel); specular/gloss in the alpha (white = shiny) | BC7 |
| Environment mask | _m | for metal reflections with an env map | BC7 or BC1 |
| Glow | _g | emissive | BC1/BC7 |
Paths in the nif are relative to Data: textures\YourMod\Thing.dds. Keep all files under one mod folder name.

## Converting a TRELLIS.2 model
1. Import the .glb, check the back, clean up (merge, delete floaters), decimate to the budget above.
2. UV: TRELLIS gives a UV + baked textures; re-bake to a clean 2048 map if the layout is messy.
3. Save diffuse/normal from the glTF material as PNG, convert to DDS (BC1/BC7 as above, normal green flipped,
   specular in the normal's alpha: invert roughness).
4. Weapons/clutter: apply transforms, set origin like the vanilla mesh, export with PyNifly (game: Skyrim SE).
5. Armour/clothing: conform to the body in Outfit Studio (CBBE or 3BA as the user uses), copy bone weights from the
   body, make _0 and _1 weights, BodySlide project if it should follow body sliders. Uses the XPMSSE skeleton when
   the user's setup has it.
6. Static objects need collision (a simple convex shape) to be solid in game.
7. Put it in the code folder with the Data layout (meshes\..., textures\...), then Package mod.
