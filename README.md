# multimc-mcp

An MCP server that sets up [MultiMC](https://multimc.org) from one request. Ask for a mod
("I want to play Warrior Cats Events") and it creates a ready instance: the right Minecraft
version, the right loader (NeoForge / Forge / Fabric / Quilt), the mod, and every mod it
requires, all at versions that work together.

It's a sidecar: it never modifies MultiMC itself. It reads and writes MultiMC's data folder in
the formats MultiMC uses (`instance.cfg`, `mmc-pack.json`, `instgroups.json`), and MultiMC picks
new instances up on its own, even while it's open.

Mods come from **Modrinth** (no key) and **CurseForge** (needs a Core API key in
`CURSEFORGE_API_KEY`).

## Tools

| Tool | What it does |
|---|---|
| `plan_setup(mods, mc_version?, loader?)` | Read-only. Finds the best Minecraft + loader every mod supports (stable releases first, then a Java that's installed, then newest), picks each mod's version and walks required dependencies. Returns a `plan_id`. |
| `create_instance(plan_id, name, group?, memory_mb?)` | Downloads and hash-checks everything, builds the instance in a staging folder, then moves it into place in one step. |
| `install_modpack(modpack, ...)` | Whole Modrinth (`.mrpack`) or CurseForge (`manifest.json`) modpack as a new instance. |
| `add_mods`, `update_mods`, `set_mod_enabled` | Change an existing instance. Installed jars are identified by hash (Modrinth sha1, CurseForge fingerprint). Updates back the old jars up to `<instance>/.mcp-backup/`. |
| `rename_instance`, `match_folders_to_names` | Rename an instance *and its folder* (MultiMC's own rename leaves the folder, which is the instance ID), updating groups and the selected instance. |
| `list_instances`, `get_instance`, `search_mods`, `multimc_status` | Look around. |
| `launch_instance(instance, server?)` | Start it (`MultiMC -l`). |

## Install

```bash
git clone git@github.com:shrimpwagon/multimc-mcp.git ~/repos/multimc/mcp-server
cd ~/repos/multimc/mcp-server && uv sync
claude mcp add -s user multimc -e CURSEFORGE_API_KEY='<key>' -- ~/repos/multimc/mcp-server/.venv/bin/multimc-mcp
```

`MULTIMC_DIR` overrides where MultiMC's data folder is (default `~/.local/share/multimc`, or a
portable `~/MultiMC`).

## Safety rules it follows

- Never touches `accounts.json`, and never changes an instance whose game is running.
- Group changes and renames only while MultiMC is closed, because MultiMC keeps the group list
  and instance IDs in memory and would overwrite them.
- Search results only count when the name really matches, so "dragon survival" never quietly
  becomes "Dragon Survival Compatibility". Otherwise the plan says so and lists the closest hits.
- Some CurseForge authors block third-party downloads. Those files are listed as
  `manual_downloads` (page link + destination folder), never fetched around the block.

## Tests

```bash
uv run pytest
```
