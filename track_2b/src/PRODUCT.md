# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

GM users who run and edit narrative roleplaying sessions. The current redesign also supports demonstration to Hack Apertus judges.

## Product Purpose

Apertus Mundus provides narrative simulation and team actions in one persistent world. Scene prose, characters, branching decisions, world knowledge and rules remain connected.

## Capabilities and Constraints

Preserve the current Vue frontend, Python API contracts, real campaign content, scene editing, generation, teams, maps, timelines, memory, world entities, knowledge and saves. No framework migration or replacement backend is requested. English is the interface and bundled demo language.

The user explicitly requires two choices when leaving modified scene editing: save or discard changes. Do not add an unsaved badge or a keep-draft choice. Distinguish valid campaigns from unrelated JSON files. Organize tools under Live Session, World Reference and Rule Authoring.

The user also requests a friendly device choice after launch, phone access without typing network commands, and synchronization. The connection page offers the local GM workbench and an opt-in LAN QR link for either the same responsive GM workbench or separate NPC communication. Paired clients share saved world state; drafts remain on their device and stale scene saves must be rejected.

## Brand Commitments

The user requests technical minimalism with light fantasy narrative character and colors drawn from https://hackapertus.ch/. Visual upgrade is the primary task. The user confirmed Apertus Mundus as the primary product name.

## Evidence on Hand

README.md describes the runtime. index.html is the existing GM workspace. campaigns/ contains real campaign material. The isolated tests/serve_ui_fixture.py uses a temporary database and simulated generation.

## Product Principles

- Make the current narrative and next action easy to find.
- Preserve real functionality while replacing the visual system.
- Protect scene edits and reject unrelated files before loading.
- Keep world knowledge and rule authoring discoverable without crowding the running scene.
