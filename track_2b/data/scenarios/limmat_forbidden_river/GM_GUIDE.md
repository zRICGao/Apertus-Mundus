# GM guide — Limmat: The Forbidden River

The prepared choices exercise actual backend rules. Generate a new branch when
you want improvisation; select the authored choices below for a repeatable tour.
Rules run on committed scenes, not on unsaved editor text or speculative options.
The local deterministic conditions need no inference. Semantic conditions and
the opt-in generated epilogue use your configured model. Their offline tests use
controlled provider responses and do not establish model quality.

## A complete demonstration route

Select the **Field Arrival** party. Start from scene 1, Arrival — First Light.
The numbers below are scene IDs, useful when inspecting the scene tree.

| Step | Scene / choice | Expected committed result |
| --- | --- | --- |
| 1 | 1 → 2, meet Gao Xing | Handbook, Rune coin, Midnight ticket and one Calming tonic; orientation recorded once |
| 2 | 2 → 3, ShopVille | Locker clue and new lore entry; the hatch remains locked |
| 3 | 3 → 4 → 5 | Move along registered public stairs and streets; ordinary travel spends no HP/SAN |
| 4 | 5 → 6, explicitly present handbook and ticket | Hard trigger moves Field Arrival to scene 7 / room 106; consumes the ticket; costs 6 SAN once |
| 5 | 7 → 8 → 13 | Reach the underground hub and see the seal; no finale options without evidence |
| 6 | 13 → 12 | Take one Treaty copy; unlock source 3 and record the treaty state |
| 7 | 12 → 10 → 12 | Xu Sijie's exercise costs 4 HP; receive Resonance vial and UZH verdant mark |
| 8 | 12 → 4 → 5 → 11 → 9 | Ordinary surface route to Liu Zisen's authorized calibration; pay 4 SAN; receive Calibration core and ETH access card |
| 9 | 9 → 11 → 17 | Deliberate safehouse rest restores up to 8 HP/SAN, each capped at 100; Campus Watch remains unchanged |
| 10 | 17 → 4 → 12 → 13 | All three evidence items are present; source 8 and three finale proposals appear once |
| 11 | Choose 14, 15 or 16 | Commit exactly one repair and consume its two prepared components |
| 12 | Return via 12 → 4 → 17 | Optional mirror suggestion; decline it or inspect scene 23 |

With no optional tonic, breach, danger or extra rest, the route reaches the finale
with **HP 100 / SAN 98**. The Field Arrival protagonist starts at 100/100; Campus
Watch starts at 90/90 and keeps those values throughout this route.

| Finale | Cost | Expected protagonist values | Shared seal outcome |
| --- | --- | --- | --- |
| 14: Geometric answer | 10 SAN | HP 100 / SAN 88 | `eth` |
| 15: Living answer | 10 HP | HP 90 / SAN 98 | `uzh` |
| 16: Narrow truce | 5 HP + 5 SAN | HP 95 / SAN 93 | `neutral` |

Every finale requires the Treaty copy, Calibration core and Resonance vial,
completed evidence prerequisites, an unresolved seal and enough resource to
remain above zero after paying its cost. Both prepared components are consumed;
the Treaty copy remains. An already committed ending cannot become a second
ending by revisiting another proposal. The result is a limited repair, not an
instant end to the treaty or proof of a guilty individual.

## Rule catalogue

Every rule starts unfired and is scoped to Field Arrival unless marked otherwise.
One-shot rules have `max_fire_count: 1`. Dependencies use trigger IDs. Conditions
demonstrate AND, OR, NOT, items, stats, scenes, typed entity states and semantic
judgements. Semantic uncertainty does not count as success.

| ID | Rule | Gate and effect |
| --- | --- | --- |
| 1 | Orientation | Scene 2; grants four recorded items, flag, memory and scene text |
| 2 | Locker 1855 | Scene 3 + Rune coin + rule 1; documents a discovered possibility without opening it |
| 3 | Treaty appendix | Scene 12; grants Treaty copy, sets typed state and enables source 3 |
| 4 | Controlled fold | Scene 6 + handbook + ticket + rule 1; one ticket, 6 SAN, source 7, awakened state, chapter flag and hard navigation to 7 |
| 5 | 114-second protocol | When scene 7 is committed after rule 4; adds a warning and scoped memory, never inferred gaze damage |
| 6 | ETH calibration | Scene 9; 4 SAN, core, ETH credential, typed evidence state, Liu Zisen trust and personal memory |
| 7 | UZH resonance | Scene 10; 4 HP, vial, UZH credential, typed evidence state and Xu Sijie trust |
| 8 | Three proposals | Scene 13 + all three items + rules 3/6/7 + unresolved seal; enables source 8 and injects three options |
| 9 | ETH ending | Scene 14 + evidence + SAN >10; consumes components and excludes rules 10/11 |
| 10 | UZH ending | Scene 15 + evidence + HP >10; consumes components and excludes rules 9/11 |
| 11 | Neutral ending | Scene 16 + evidence + HP/SAN >5; consumes components and excludes rules 9/10 |
| 12 | Rest | Scene 17 + HP or SAN below 100 + no deadlock; +8 each capped at 100, 60 real seconds cooldown, at most twice |
| 13 | Critical condition | HP or SAN <=20 outside the safehouse; warns and records a flag, without inventing a rescue or moving the party |
| 14 | Public breach | Confirmed visible cast at crowded HB or Central; records exposure, flag and Liu Jiao fear; does not charge a spell cost again |
| 15 | Mirror suggestion | Scene 17 + Mirror invitation; soft suggestion to scene 23, which can be declined |
| 16 | Mirror observation | Scene 23; records an ambiguous observation without naming Li's home plane |
| 17 | Campus Watch report | Scene 27, team 2 only; private team-2 state and memory, no access to the protagonist's private chat |
| 18 | Tonic | Item + confirmed decision to drink + no deadlock; consume one tonic and recover up to 5 SAN, capped at 100 |
| 19 | Laboratory warning | Scene 22; adds an unresolved threat warning and lore without claiming victory or materials |
| 20 | Actual maintenance entry | Scene 18; records entry rather than mere discovery |
| 21 | Gao Xing leaves the desk | Awakened state + rule 4; updates his entity presence and personal memory |
| 22 | Opt-in generated epilogue | Scene 23 + exact item `GM epilogue permit` + rule 16; generates a soft destination; provider failure rolls back consumption and the rule |
| 23 | Core-elevator credential | Scene 25 + completed ETH calibration; issue one Abyss Geek key |
| 24 | Offline computation core | Scene 28 + Treaty copy + completed ETH calibration; issue one Advanced computation core |
| 25 | Living token | Scene 21 + Treaty copy + completed UZH exercise; issue one Living bone token |
| 26 | Forbidden analysis | Awakened + confirmed deliberate analysis of the view beyond the cabin's blinds during the crossing; SAN collapse, irreversible deadlock, flag and memory |

The forbidden view is a dangerous optional source-story rule, not part of the
standard route. Opening the map, reading its warning, proposing a hypothetical
action or being uncertain never activates it. The example's SAN maximum is 100;
the -100 consequence empties that reserve. Deadlock blocks ordinary safehouse and
tonic recovery. Do not also charge this scripted consequence in a generated
branch's side effects.

Rule 5 does not recursively fire merely because rule 4 moved the party. It runs
when the cabin itself is committed or checked; its essential warning is already
in the cabin's authored text. The same principle prevents arbitrary recursive
chains of scene transitions.

## Optional locked routes

Use normal map/AI movement with explicit revelation, unlock and movement actions.
Static narrative options do not pretend that a locked passage has opened.
The engine retains/consumes one matching credential from its actual owner's
inventory. Do not also remove it in a separate branch effect. Unlocking a door is
shared world state; its consumption and the moving party's position are scoped.

| Passage | Connection | Credential and source |
| --- | --- | --- |
| 202 | ShopVille → Maintenance Route 1855 | Rune coin from rule 1; reveal this hidden edge before unlocking |
| 210 | Interchange → Logical Cooling Corridor | ETH access card from rule 6 |
| 211 | Interchange → Disordered Greenhouse | UZH verdant mark from rule 7 |
| 214 | Discarded Records → Polyhedral Core | Advanced computation core from rule 24; reveal the shaft first |
| 215 | Quantum Array Room → Polyhedral Core | Abyss Geek key from rule 23 |
| 217 | Verdant Temple → Flesh-Shaping Laboratory | Living bone token from rule 25 |

For a deterministic map check while Field Arrival is in ShopVille (room 102), the
following branch effects discover the hatch, use the real coin and enter room
103. In normal play let the selected-branch review and commit path apply them:

```json
{
  "map_actions": {
    "reveal_edge": {"edge_id": 202},
    "unlock_edge": {"edge_id": 202, "key_used": "Rune coin"},
    "movement": {"target_room_id": 103}
  }
}
```

To test discovery without entry, omit `movement`. The room/edge may become known,
but the party stays in place. Use existing room IDs to avoid generating duplicate
versions of HB, Central or the safehouse. Multi-floor passages are explicitly
stairs, elevators or portals rather than ordinary same-floor doors.

## Knowledge, NPCs and persistence

Sources 3, 7 and 8 begin hidden and become enabled through the story. Source 9 is
GM-only and stays excluded from public retrieval even after it is enabled. Edit
the Markdown files, then reload the folder to rebuild their source evidence.
Imported embeddings are intentionally empty; configured retrieval can reindex
the English documents. No original Chinese vectors are reused for English text.

Use Gao Xing, Liu Zisen or Xu Sijie for NPC chat. Their stable entity IDs prevent
new copies with the same name; original Chinese names also resolve as aliases.
Private replies do not automatically become another party's memory. Phone
pairing and synchronization use the application's ordinary connection workflow.
The scenario does not bypass pairing, merge parties or publish drafts.

Export after a lesson or ending. Reloading that versioned save preserves item
consumption, triggered counts, shared outcome, private scopes and entity IDs.
Loading Blank Campaign clears these data through the same loader. The blank
template is a new campaign, not a recovery mechanism for a failed character.

## Repeatability

From the repository root:

```sh
python -m unittest discover -s track_2b/tests -v
```

The bundled regressions exercise actual rule transactions, all endings, missing
items, hard/soft behavior, capped recovery and cooldown, party isolation, map
unlocking, hidden-source boundaries, uncertain versus confirmed AI conditions,
generated-node failure rollback, export/load and a genuinely blank reset. Real
model prose, judgement quality, browser interaction and two-device latency still
require an interactive run with configured services.
