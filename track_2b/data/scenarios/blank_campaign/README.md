# Blank Campaign

This is a clean, loadable English starter. It contains one empty scene named
**Opening Scene**, selected as the starting scene. Everything else is empty:
worldview, memories, characters, teams, lorebook, triggers, map, knowledge,
relationships, flags and dynamic states. It contains no hidden Limmat data.

1. Load **Blank Campaign** in the campaign picker.
2. Write the world premise and opening scene in the workbench.
3. Create at least one player character. Create a team if needed.
4. Add locations and passages in the map editor, then bind scenes to locations.
5. Add knowledge documents and rule triggers when your setting needs them.
6. Export under a new campaign name to preserve this starter.

`map.json` deliberately contains empty room and passage arrays. No placeholder
knowledge document is loaded into retrieval. If creating a folder-based campaign
by hand, add UTF-8 `.md` or `.txt` files under a new `knowledge/` subfolder. With
an empty `rag_library`, the loader imports those files automatically.
