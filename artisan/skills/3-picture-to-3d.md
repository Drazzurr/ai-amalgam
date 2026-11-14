# Picture to 3D model (SCULPT:)

TRELLIS.2 builds a textured mesh (.glb with colour, roughness, metal and normal maps) from ONE picture.

## Make the source picture right (this decides the result)
- One subject, whole object in frame with a little margin; nothing cut off (feet, weapon tips, tail).
- Plain light grey or white background (the template removes it), soft even lighting, no strong cast shadows.
- Front three-quarter view (about 30-45°) shows depth; straight-on front loses the sides.
- Neutral, readable pose for characters: arms slightly away from the body (A-pose) if it will be rigged; a dynamic
  pose is fine for a mini, but avoid limbs crossing in front of the body.
- Matte materials; avoid glass, mirrors, smoke, hair blowing everywhere, tiny text.
Prompt pattern: "<character description>, full body, standing in a relaxed A-pose, front three-quarter view, plain
light grey studio background, soft even lighting, highly detailed, <style suffix>."

## After SCULPT
- It's a single mesh, triangles, often 100k-1M faces. Look at it in Blender (File → Import → glTF 2.0).
- Typical clean-up: delete floating bits (Select Linked, delete), Merge by Distance, fill holes (3D Print Toolbox →
  Check All / Make Manifold), Decimate to a budget, recalculate normals.
- Back sides it couldn't see are guessed: check them; repaint or sculpt fixes if needed.
- For a better back: paint a back view of the same character with the same seed/prompt, and compare.
- Slow? It takes a few minutes on a 12 GB card. Resolution presets above 1024 may not fit in 12 GB.
