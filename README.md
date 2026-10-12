# multimc-mcp

An MCP server that sets up [MultiMC](https://multimc.org) from one request. Ask for a mod
("I want to play Warrior Cats Events") and it creates a ready instance: the right Minecraft
version, the right loader (NeoForge / Forge / Fabric / Quilt), the mod, and every mod it
requires, all at versions that work together.

It's a sidecar: it never modifies MultiMC itself. It reads and writes MultiMC's data folder in
the formats MultiMC uses (`instance.cfg`, `mmc-pack.json`, `instgroups.json`), and MultiMC picks
new instances up on its own, even while it's open.

Mods come from **Modrinth** (no key) and **CurseForge** (optional; needs your own CurseForge
API key in `CURSEFORGE_API_KEY`). For a tool like this, CurseForge asks you to apply for a
3rd-party key ([form linked from their docs](https://docs.curseforge.com/rest-api/)); a self-serve
"CurseForge for Studios" console key works for lookups but gets 403 on search. Per CurseForge's
3rd-party API terms, nothing from their API is cached: responses aren't memoised and CurseForge
files aren't kept in the download cache once they're in the instance.

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

Needs [uv](https://docs.astral.sh/uv/) (it brings its own Python 3.12) and Claude Code or another MCP client.
Works on Linux, Windows and macOS.

**Linux / macOS**

```bash
git clone https://github.com/shrimpwagon/multimc-mcp.git ~/multimc-mcp
cd ~/multimc-mcp && uv sync
claude mcp add -s user multimc -- ~/multimc-mcp/.venv/bin/multimc-mcp
```

**Windows (PowerShell)**

```powershell
git clone https://github.com/shrimpwagon/multimc-mcp.git $HOME\multimc-mcp
cd $HOME\multimc-mcp; uv sync
claude mcp add -s user multimc -- $HOME\multimc-mcp\.venv\Scripts\multimc-mcp.exe
```

That's all you need: mods come from Modrinth, no key required. CurseForge support is optional
(see above); to turn it on, add `-e CURSEFORGE_API_KEY=<your key>` before the `--`.

**Finding MultiMC.** It looks in the usual place for each OS: `~/.local/share/multimc` on Linux,
`MultiMC.app/Data` on macOS, and on Windows (where MultiMC is portable) a `MultiMC` folder in your
home, Desktop, Downloads, Documents, `C:\` or Program Files. If MultiMC is open it also asks the
running copy where it lives. Otherwise set `MULTIMC_DIR` to the folder that holds `multimc.cfg`
(`-e MULTIMC_DIR=...` in the `claude mcp add` line).

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

## License

Apache-2.0, same as MultiMC. Unofficial; not affiliated with or endorsed by the MultiMC project.
