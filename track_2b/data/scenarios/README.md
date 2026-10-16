# Bundled English campaigns

These source folders are copied into the application's configured `campaigns/`
directory when the application starts. Existing folders are preserved, including
modified copies of these examples. Installing a folder does not load it into or
replace the active world. Choose a campaign in the normal campaign picker.

| Campaign | Source folder | Purpose |
| --- | --- | --- |
| Limmat — The Forbidden River | [limmat_forbidden_river](limmat_forbidden_river/README.md) | A playable English adaptation and feature demonstration |
| Blank Campaign | [blank_campaign](blank_campaign/README.md) | One editable opening scene, with no setting, characters, map or rules |

The existing lighthouse demo is retained separately under `data/demo/` for the
isolated deployment smoke flow. These two examples are also included in Docker
images and clean submission exports.

The Limmat example adapts the user-supplied campaign and its six knowledge
documents. Its 16 named characters, organizational offices and relationships are
preserved. Names use English romanization; original names remain entity aliases.
The original duplicate Publicity lore entry is represented once. Magical events,
institutional roles and the secret timetable are campaign fiction.

To use an updated example when you already have a modified installed copy, preserve
your save first, then copy the source to a new campaign folder. Restarting will
not replace your edits. The blank template has no inherited campaign data; loading
it clears the current world's previous scenario state through the normal load flow.
