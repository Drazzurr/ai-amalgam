---
name: 3d-model-study
description: Use when Jacob wants a 3D model file studied in detail, rebuilt or replicated, converted between formats, or prepared for a game mod (for example Skyrim). Covers OBJ, FBX, glTF/GLB, STL, NIF and similar.
---

# 3D Model Study & Replication

You read model files, measure everything about them, and rebuild or convert them with numbers that prove how close the result is. **Never claim "exact" without measuring it.**

## 1. What's possible (be honest with Jacob)

- **From a model file: yes, in fine detail.** Text formats (OBJ, glTF JSON, PLY ASCII) can be read directly. Binary formats (GLB, FBX, STL, NIF) need code: Python with libraries like trimesh, numpy, pygltflib, or Blender's Python API. Check what's installed before you start.
- **Exact copy:** keep the original geometry and convert it without changes. This is lossless, and the right choice when the goal is "the same model in a different format or game".
- **Rebuild:** write a Blender Python script that recreates the model, then measure the difference. Good for learning how it's built, cleaning it up, or remaking it differently.
- **From pictures only:** approximate. You can estimate proportions and shapes from several angles, but not exact geometry. Say so up front.
- Code is needed for real measurement. In a setup where Claude can't run code (like AI Amalgam, where shell tools are blocked), plan and write the scripts there and run them in the Claude app, Claude Code, or Blender on Jacob's PC.

## 2. Study report

For every model, measure and report:

| Area | Details |
|---|---|
| File | Format, version, units, up-axis, scale |
| Size | Bounding box (x, y, z), center, real-world size |
| Geometry | Vertex, edge, face counts; tris vs quads vs ngons; separate meshes and objects |
| Topology | Edge loops around joints and face, poles, non-manifold edges, holes, duplicate verts, flipped normals |
| Normals | Smooth/hard edges, custom split normals |
| UVs | UV sets, overlaps, seams, texel density, unused space |
| Materials | Material slots, shader type, texture maps (color, normal, roughness, metal, emissive) and resolutions |
| Rig | Skeleton (bone count, names, hierarchy), weight painting (max influences per vertex), rest pose |
| Extras | Shape keys/morphs, LODs, animations, collision meshes |

Save reports next to the work, or in the chat if Jacob prefers.

## 3. Rebuild workflow

1. Study the original (section 2).
2. Plan the build: base shapes first, then main forms, then details, matching the original's topology flow where possible.
3. Write the Blender Python script in stages; run and check after each stage.
4. **Measure the match:** vertex/face counts, bounding box difference, and surface distance (sample points on both surfaces; report average and maximum distance, e.g. Chamfer and Hausdorff). Also compare UV layout and normals.
5. Report: "Average deviation 0.2 mm, max 1.4 mm at the left ear" is a result. "Looks identical" is not.
6. Iterate on the worst areas until Jacob's tolerance is met.

## 4. Porting into a game (Skyrim and others)

- **Scale and axes:** convert units and up-axis to the target game. Look up the game's unit scale before converting; don't assume it.
- **Skeleton:** match the target skeleton's bone names and hierarchy (for Skyrim followers, the standard humanoid skeleton or XPMSSE as Jacob's setup requires). Re-weight or transfer weights; check deformation at elbows, knees, shoulders and hips in test poses.
- **Mesh limits:** check the target's limits on bones per vertex, vertex count per shape and partition rules; split meshes if needed.
- **Textures:** convert to the target format (Skyrim uses DDS, typically BC7 or BC1 for color and BC5 or BC7 for normal maps); check normal map convention (green channel up or down); resize to power-of-two sizes.
- **Export:** for Skyrim, NIF via Blender's NifTools or Outfit Studio, then test in game (or NifSkope first).
- **Head and face models** often need the game's own tools (for Skyrim, the Creation Kit for FaceGen data). Say when a step needs a tool you can't run.

## 5. Ownership

Models from games and artists belong to their makers. Studying and converting them for personal use is one thing; publishing a mod that includes another game's assets usually needs the owner's permission. When a project uses ripped or purchased assets, remind Jacob once to check the license or mod policy before publishing, and keep credit notes for every source.
