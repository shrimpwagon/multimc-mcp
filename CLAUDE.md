# CLAUDE.md: multimc-mcp

Python MCP server (official `mcp` SDK **v2**: `from mcp.server.mcpserver import MCPServer`, the class
FastMCP was renamed to) that sets up MultiMC instances. See README.md for the tools. Background on
MultiMC's formats and why this is a sidecar instead of a Launcher fork: `../CLAUDE.md`.

## Layout (`src/multimc_mcp/`)

| File | Role |
|---|---|
| `server.py` | Tool definitions and the agent-facing `INSTRUCTIONS`. |
| `resolver.py` | Request → project matching, (MC, loader) pair ranking, recursive dependency walk. |
| `sources.py` | One interface over Modrinth + CurseForge; CurseForge IDs are prefixed `cf:` / `cf:file:`. |
| `modrinth.py`, `curseforge.py` | API clients. CurseForge data is converted to Modrinth-shaped dicts. |
| `meta.py` | meta.multimc.org (what MultiMC can launch), Mojang Java requirements, installed JDKs. |
| `launcher.py` | MultiMC data dir, instances, groups, renames, running detection, launching. |
| `install.py` | Verified download cache (`~/.cache/multimc-mcp`), atomic instance builds, modpack readers. |
| `inifile.py` | MultiMC's INI dialect (escapes `\n \t \\ \#`, keys sorted). |

## Working on it

- `uv run pytest` (offline). Live check without touching real instances: point `MULTIMC_DIR` at a
  scratch folder holding a `multimc.cfg` with `InstanceDir=instances`, then drive the server over stdio
  with `mcp.client.stdio` (v2 client attributes are snake_case: `server_info`, `is_error`,
  `structured_content`; list results come back as one content block per item).
- MultiMC's source is the spec: `../Launcher/launcher/` (`InstanceList.cpp`, `minecraft/PackProfile.cpp`,
  `minecraft/MinecraftInstance.cpp`, `InstanceImportTask.cpp`, `settings/INIFile.cpp`, `FileSystem.cpp`).
- **CurseForge key quirk (2026-10-12):** the key works for every endpoint except `GET /v1/mods/search`
  (403). Without search, CurseForge mods resolve only by numeric ID (`cf:420799`) or by fingerprint of
  an installed jar; slug/URL lookups fail cleanly with a hint. Re-test with
  `curl -H "x-api-key: $K" 'https://api.curseforge.com/v1/mods/search?gameId=432&searchFilter=jei'`.
  The key is in `~/.claude/CREDENTIALS.md` (CurseForge section).
- Don't bypass CurseForge's "third-party downloads disabled" (no edge.forgecdn.net URL building);
  those files go to `manual_downloads`.
- Deployed (user-scope `claude mcp add`) on `shawn`, `cassie`, `cami`. Each box has its own clone at
  `~/repos/multimc/mcp-server` and venv; update with `git pull && uv sync`.
