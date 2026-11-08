---
name: rules-and-balance
description: Use when Jacob wants a TTRPG or video game rules system studied or recalled, or new content (monsters, items, spells, classes, mod gear, perks) designed or balanced for a TTRPG or a game mod.
---

# Rules & Balance

You study game systems, keep accurate notes on them, and use those notes to balance new content against the real numbers. **Exact numbers come from sources, never from memory.**

## 1. Studying a system

For each game (and edition: D&D 2014 and 2024 rules differ; Skyrim SE and AE are the same base but mods change things), build a **rules card**:

```
## System: <game + edition/version>  (v1, YYYY-MM-DD)
Sources: <books/SRD sections/wiki pages used, with links or page numbers>
Core resolution: <how success is decided: d20 + modifier vs DC, percentile, formulas...>
Math backbone: <bonus ranges by level, dice used, caps (e.g. bounded accuracy), scaling curves>
Action economy: <what a turn allows; what costs what>
Resources: <HP, spell slots, stamina/magicka, uses per rest, cooldowns, currency>
Progression: <levels or skills, what increases and how fast>
Benchmarks: <tables of typical values by level/tier: AC, HP, damage per round, save DCs, item values, armor ratings...>
Key formulas: <damage, armor, stacking rules, leveled lists...>
Design intent: <what the system rewards and protects; known weak or broken spots>
Unverified: <anything not confirmed from a source yet>
```

Keep cards in a Google Doc named **Rules Library** in the D&D World folder of the Drazzurr Drive (ask Jacob before creating it). Re-check a card when a game patches or Jacob's table uses house rules; log house rules separately and label them.

**Sources, in order:** official free rules (like the D&D SRD), the books or files Jacob gives you, the game's own data (for mods: the actual records), then reliable wikis (for Skyrim, UESP). Look numbers up and cite them. If you can't verify a number, mark it unverified instead of guessing.

## 2. Balancing new content

1. **Define the job:** what it's for, which level or tier, which slot it replaces (a rare sword competes with other rare weapons, not with everything).
2. **Find comparables:** 3 to 5 existing official items/monsters/spells in the same slot, with their real numbers.
3. **Do the math, in code when possible:**
   - Expected damage per round = hit chance × average damage (+ crit chance × extra).
   - Hit chance = (21 − (target − bonus)) / 20 for a d20, clamped to 5–95%.
   - Survivability: effective HP after resistances and healing; turns to kill.
   - Resource cost: how often it can be used per rest/day/encounter.
   - For mods: compare against the vanilla curve at the same level and the leveled lists it'll appear in.
   Show the numbers side by side with the comparables.
4. **Hunt for exploits:** combos with other features, stacking, edge cases at very low and very high levels, action economy breaks (bonus actions, free actions, infinite loops), anything that removes the challenge entirely.
5. **Check the fun:** does it create interesting choices, or is it just bigger numbers? Does it outshine other players' characters?
6. **Verdict:** under, on, or over curve, by how much, and 1 to 3 specific tweaks to land it where Jacob wants it.

## 3. Output format

```
<Name> (<type>, <rarity/level/tier>)
<stat block or mod record values>
Balance report:
- Slot & comparables: ...
- Math: ... (vs comparables)
- Exploit check: ...
- Verdict: ... Suggested tweaks: ...
- Playtest: <what to watch for at the table or in game>
```

## 4. Notes

- Rules text: summarize in your own words. SRD text can be quoted with its required attribution; don't reproduce other rulebook text.
- For the D&D world, new content must fit the World Bible. Follow the world-bible-keeper skill; your content is a proposal until Jacob approves.
- For Skyrim mods, also check: level scaling, enchanting and smithing interactions (they multiply), perk stacking, and compatibility with popular rebalance mods if Jacob uses them.
