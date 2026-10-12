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
- **CurseForge key (2026-10-12):** Shawn's key is a self-serve *CurseForge for Studios* console key
  (meant for game developers). It works for every endpoint except `GET /v1/mods/search` (403 from
  every network tested: house, shop, AWS). CurseForge wants 3rd-party tools to apply via the form at
  https://forms.monday.com/forms/dce5ccb7afda9a1c21dab1a1aa1d84eb?r=use1 (linked from
  docs.curseforge.com/rest-api). Until approved, CurseForge mods resolve only by numeric ID
  (`cf:420799`) or by fingerprinting installed jars. Key: `~/.claude/CREDENTIALS.md`.
- **CurseForge 3rd-party terms forbid saving/caching API data.** So `curseforge.py` always uses ttl=0,
  `Sources` doesn't memoise CurseForge projects, and `Downloader.forget()` deletes CurseForge files
  from the download cache after they're placed. Keep it that way. Modrinth caching is fine.
- Don't bypass CurseForge's "third-party downloads disabled" (no edge.forgecdn.net URL building);
  those files go to `manual_downloads`.
- Deployed (user-scope `claude mcp add`) on `shawn`, `cassie`, `cami`. Each box has its own clone at
  `~/repos/multimc/mcp-server` and venv; update with `git pull && uv sync`.

## Cross-platform testing (verified 2026-10-12)

Runs on Linux, Windows and macOS (`psutil` for processes, per-OS data dir / binary / JDK lookup in
`launcher.py` and `meta.py`). No hosted CI: test on the house-LAN dev machines listed in
`~/.claude/CLAUDE.md` ("Dev/test computers"): `ssh mac` (M1) and `ssh shawn@192.168.1.218` (Windows).
They're Zeltro's release builders, so install alongside and clean up (MultiMC, the clone, uv + its
Python, and on Windows the user-PATH entry the uv installer adds).

What was verified on both: unit tests; plan → create → add_mods → rename against a scratch
`MULTIMC_DIR`; auto-discovery of a real MultiMC (`%USERPROFILE%\MultiMC`, `~/Applications/MultiMC.app/Data`);
and with the real MultiMC running: `multimc_open` true, rename refused, a new instance picked up live
(MultiMC's log shows "Loaded instance ..."). Tips:
- Pre-seed a test `multimc.cfg` with `Language`, `LastHostname` (= `hostname`), an existing `JavaPath`
  and `AutoUpdate=false`, or MultiMC opens its setup wizard (`Application::createSetupWizard`).
  Backslashes in that file must be doubled (MultiMC INI escaping).
- Windows over ssh: send PowerShell `-EncodedCommand` (UTF-16LE base64); processes started in an ssh
  session die when it closes, so start MultiMC and test in one session; run Python with `-X utf8`
  (`-I` ignores PYTHONIOENCODING); PowerShell 5.1 strips `"` from native-command args, so pass JSON via a file.
- macOS MultiMC is x86_64 (Rosetta). Its window shows on the Mac's screen; quit with
  `osascript -e 'tell application "MultiMC" to quit'`.
