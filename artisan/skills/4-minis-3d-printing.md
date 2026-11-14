# 3D-printed miniatures (D&D and tabletop)

## Pipeline
1. PAINT a clean source (see the picture-to-3D skill): dynamic but compact pose, weapon close to the body.
2. SCULPT it. 3. Clean and prep in Blender (headless with the Blender tools, or the user's Blender). 4. Export STL.

## Scale (agree it with the user once, then remember it)
- 28 mm "heroic": an average human is ~28 mm to the eyes (~30-32 mm to the top of the head).
- 32 mm: ~32-35 mm tall for an average human. Bigger creatures scale from there (ogre ~2x).
- D&D bases: Medium 25 mm round, Large 50 mm, Huge 75 mm, Gargantuan 100 mm+. Make the base part of the model or a
  separate part with a peg.
- Set Blender to millimetres (Scene → Units: Metric, Unit Scale 0.001, Length mm) before scaling, and measure.

## Make it printable
- Watertight (manifold) mesh: 3D Print Toolbox → Check All; fix non-manifold edges, holes, flipped normals.
- Thin parts snap: keep blades, spears, staffs, fingers at least ~0.8-1 mm thick at 28-32 mm (resin can show 0.5 mm,
  but it breaks); thicken in Blender (Solidify / scale along the thin axis) or bulk them up in the source picture.
- 28-32 mm figures print SOLID. Hollow only big pieces (~50 mm+): 1.5-2 mm walls, with drain holes of at least
  2.5 mm near the bottom, and tell the user to cure the inside.
- Split big or awkward models (cloak, weapon arm, base) with keyed joints: a 2-3 mm peg and a hole ~0.2 mm wider.
- Merge everything that touches into one shell per part; no internal faces.
- FDM printers need everything chunkier (~1.5 mm minimum features); resin is the default for minis.

## Export
STL (binary) in millimetres, one file per part, named clearly ("Ranger_body_32mm.stl"). Supports are added in the
user's slicer (Lychee, Chitubox): say which way to orient it (usually tilted 30-45°, face away from supports).
