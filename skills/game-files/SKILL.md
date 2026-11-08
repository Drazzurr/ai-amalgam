---
name: game-files
description: Use when Jacob wants game data read or explained straight from the game's own files (Skyrim, Baldur's Gate 3, Cyberpunk 2077, The Witcher 3, Mass Effect, Dragon Age): archives, plugins, packages, meshes, textures, dialogue and scripts.
---

# Reading game files

In AI Amalgam you can read game files directly with READ lines (see the GAME FILES section of your instructions): `READ: <full path> :: <what>`. Reading is read-only and runs right away. Outside Amalgam, the same readers are in Amalgam's `gamefiles` Python package.

## How to work

1. **Start broad, then narrow.** Read the file with no `<what>` for an overview, then `list <text>` to find things, then `extract` or `record`/`export` for detail.
2. **Archives first, then the file inside.** Extract the inner file (it lands in the extracted folder), then READ that extracted path.
3. **Pictures:** textures are converted to .png. Open the .png with your file reader to see it.
4. **Never change game files.** If Jacob wants a change, make it in a mod folder or the code folder, never in the game install.
5. **Say how sure you are.** The readers decode the common parts of each format. If a value looks odd or a section can't be read, say so and suggest the dedicated tool below.
6. **Mind the size.** Big archives have tens of thousands of files, so filter with `list <text>`.

## By game

**Skyrim SE (and other Creation Engine games)**
- `.esp/.esm/.esl` plugins: overview (masters, record counts); `list WEAP iron`; `record <EditorID or FormID>` shows every subrecord. If the plugin is localized, names are string ids: look them up in `Strings/<plugin>_English.strings` (`get <id>`).
- `.bsa` archives (Data folder, e.g. `Skyrim - Meshes0.bsa`): `list <text>`, `extract <path>`.
- `.nif` meshes: block types, node/bone names, texture paths. Geometry: import into Blender with the PyNifly add-on (use the Blender connection if it's on).
- `.pex` compiled scripts: string table only. Real code: the `.psc` sources (Data/Scripts/Source) are plain text.
- `.dds` textures: converted to .png.
- Deeper editing: xEdit (SSEEdit), Creation Kit, Outfit Studio, NifSkope.

**Baldur's Gate 3**
- `.pak` archives (Data folder, e.g. `Shared.pak`, `Gustav.pak`, `Models.pak`, `Textures.pak`): `list <text>`, `extract <path>`.
- `.lsx` (XML) is read directly. `.lsf/.lsb` are converted to .lsx with LSLib (needs "Install helpers" in the Apps card).
- `.loca` text (`Localization/English/english.loca` inside `English.pak`): `find <text>` or `get <handle>`.
- `.gr2` models: converted to .glb/.dae with LSLib, then import into Blender.
- Stats are plain text (`Public/<mod>/Stats/Generated/Data/*.txt`).

**Cyberpunk 2077**
- `.archive` files (`archive/pc/content`): files are stored by path hash. `list` shows names only where WolvenKit's hash list knows them; `extract <full path>` works with the exact game path (e.g. `base\characters\...\x.mesh`). Needs the game folder added (for the Oodle DLL) and works on Windows only.
- CR2W files (.ent, .app, .mesh, .ink...): overview lists object classes and referenced files; `json` converts the whole file with WolvenKit CLI.
- `.reds` and `.script` sources are plain text.

**The Witcher 3**
- `.bundle` files (`content/content0/bundles`): `list`, `extract`.
- CR2W files (.w2ent, .w2mesh, .journal...): object classes, referenced files, names.
- `.ws` scripts (`content/content0/scripts`) are plain text and the best source for game logic.
- Deeper: WolvenKit 7 (the Witcher 3 edition).

**Mass Effect (Legendary Edition and originals)**
- `.pcc` / `.sfm` / `.upk` packages: overview, `exports <text>`, `export <object>` (property values, TLK string numbers, references), `imports`, `names`. Legendary Edition packages are Oodle-compressed: add the game folder so the reader can use the game's own Oodle DLL (Windows only). ME1/ME2 originals may need python-lzo (installed by the helpers).
- `.tlk` talk tables: `find <text>`, `get <string id>` (dialogue lines for the TLK numbers you see in packages).
- Package layouts vary between games; when properties don't decode, say so and suggest Legendary Explorer.

**Dragon Age: Origins / Dragon Age II**
- `.erf` archives (`packages/core/data`, `modules`): `list`, `extract`. DA2 compressed entries are handled best-effort.
- GFF resources (.utc creatures, .uti items, .dlg dialogue, .are areas, .tlk text...): a tree of numbered fields. Field labels are numbers; say what a field likely is from context, and say that you're inferring it.
- **Inquisition and The Veilguard** use Frostbite (.sb/.toc/.cas). Those can't be read here; suggest Frosty Toolsuite.

## Anything else

Unknown formats get the file's start in hex plus the readable text inside. Use `hex <offset> <length>` and `strings <text>` to investigate, and explain your guesses about the structure.
