# Rigging and animation

## General (art, renders, minis posing)
- Rigify (built into Blender): add the Human meta-rig, fit bones to the model, Generate Rig, Parent with Automatic
  Weights, then fix weights where it creases (shoulders, hips, fingers).
- Posing a mini: rig, pose, apply the armature modifier, then do the print prep (a posed mesh needs a new check for
  thin parts and contact with the base).
- Turntable preview: a Blender script that adds a camera on a circle and renders 8-16 frames or a short MP4 (good for
  showing the user a 3D result in the chat as pictures).
- Mixamo (free Adobe website) auto-rigs and animates humanoids; the user has to upload there themselves.

## Game models
- Never invent a skeleton for a game: use the game's own (for Skyrim the vanilla or XPMSSE skeleton) and weight to it.
  Armour copies weights from the body in Outfit Studio (see the Skyrim skill).
- Skyrim animations are Havok .hkx: authoring them needs the community tool chains (e.g. Blender to HKX converters)
  and a behaviour framework (Nemesis / Pandora, or Open Animation Replacer for swaps). PyNifly's animation export is
  still in development. Plan animation work with the user before starting; most mods reuse existing animations.
- Keep a clean rest pose (A-pose or T-pose matching the game's skeleton) and apply scale before skinning.
