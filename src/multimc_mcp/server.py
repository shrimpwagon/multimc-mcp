"""MCP server: set up MultiMC instances (Minecraft + loader + mods) from one request."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .curseforge import CurseForge, api_key, is_cf
from .curseforge import page_url as cf_page_url
from .http import Http, HttpError
from .install import (Downloader, backup, build_instance, cf_components, extract_overrides, instance_cfg,
                      mrpack_components, mrpack_files, place_files, read_cf_manifest, read_mrpack, safe_name)
from .launcher import Launcher, LauncherError
from .meta import LOADERS, Meta, installed_javas, pick_java
from .modrinth import FOLDERS, Modrinth, primary_file
from .resolver import Pick, Plan, ResolveError, Resolver, _norm, best_version, identity, manual_list, names_match
from .sources import Sources

logging.getLogger("httpx").setLevel(logging.WARNING)

INSTRUCTIONS = """\
Sets up and manages Minecraft instances in the user's MultiMC launcher. Mods come from Modrinth and,
when a key is configured, CurseForge.

Typical flow for "I want to play X":
1. plan_setup(mods=[...]): picks the newest Minecraft version + loader (NeoForge/Forge/Fabric/Quilt)
   that every mod supports, the right version of each mod, and every required dependency.
   Pass mc_version/loader only if the user asked for them. Check `unresolved` (and `requested` matches)
   before going on; if a name didn't match, search_mods and retry with the right slug.
2. Briefly tell the user what will be installed, then create_instance(plan_id, name). Name it the way
   the user's other instances are named (list_instances shows them).
3. Relay `manual_downloads`: some CurseForge authors block third-party downloads, so the user must
   download those files themselves and put them in the named folder.
4. launch_instance only if the user wants to play right now (it opens the game on their screen).
Whole modpacks: install_modpack. Existing instances: list_instances, get_instance, add_mods, update_mods,
set_mod_enabled, rename_instance. Never change an instance whose game is running (the tools refuse)."""

mcp = MCPServer("multimc", version=__version__, instructions=INSTRUCTIONS)

_ctx: dict = {}
_plans: OrderedDict[str, Plan] = OrderedDict()


def _deps() -> tuple[Http, Sources, Meta, Resolver, Downloader]:
    if not _ctx:
        http = Http()
        key = api_key()
        sources = Sources(Modrinth(http), CurseForge(http, key) if key else None)
        meta = Meta(http)
        _ctx.update(http=http, sources=sources, meta=meta, resolver=Resolver(sources, meta), dl=Downloader(http))
    return _ctx["http"], _ctx["sources"], _ctx["meta"], _ctx["resolver"], _ctx["dl"]


def _launcher() -> Launcher:
    try:
        return Launcher()
    except LauncherError as e:
        raise ToolError(str(e)) from e


def _fail(e: Exception):
    if isinstance(e, (LauncherError, ResolveError, HttpError)):
        raise ToolError(str(e)) from e
    raise e


def _downloads(picks: list[Pick]) -> list[dict]:
    return [{"url": p.url, "sha1": p.sha1, "sha512": p.sha512, "relpath": f"{p.folder}/{safe_name(p.filename)}"}
            for p in picks if not p.manual]


def _newer(a: dict, b: dict) -> bool:
    """Is version a newer than version b (same project)?"""
    if "_file_id" in a and "_file_id" in b:
        return a["_file_id"] > b["_file_id"]
    return a.get("date_published", "") > b.get("date_published", "")


async def _installed(sources: Sources, inst) -> dict[str, dict]:
    """project id -> {file, version, project} for the instance's mod jars (enabled or not)."""
    mods_dir = inst.game_dir / "mods"
    if not mods_dir.is_dir():
        return {}
    files = [p for p in mods_dir.iterdir() if p.is_file() and p.name.endswith((".jar", ".disabled"))]
    found = await sources.identify(files)
    projects = await sources.projects(list({v["project_id"] for v in found.values()}))
    return {v["project_id"]: {"file": path, "version": v, "project": projects.get(v["project_id"], {})}
            for path, v in found.items()}


READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
CHANGES = ToolAnnotations(destructiveHint=False, openWorldHint=True)


@mcp.tool(annotations=READ_ONLY)
def multimc_status() -> dict:
    """Where MultiMC lives on this machine, whether it's open, which games are running, installed Java
    versions, and which mod sources are enabled."""
    launcher = _launcher()
    return {
        "data_dir": str(launcher.data_dir),
        "instances_dir": str(launcher.instances_dir),
        "multimc_open": launcher.is_running(),
        "running_instances": sorted(launcher.running_instance_folders()),
        "default_java": launcher.cfg.get("JavaPath", "java"),
        "installed_java": {str(k): v for k, v in sorted(installed_javas().items())},
        "default_memory_mb": launcher.cfg.get("MaxMemAlloc"),
        "sources": ["modrinth"] + (["curseforge"] if api_key() else []),
    }


@mcp.tool(annotations=READ_ONLY)
def list_instances() -> list[dict]:
    """All MultiMC instances: name, folder (its ID), Minecraft version, loader, mod counts, group, running."""
    return [i.summary() for i in _launcher().list_instances()]


@mcp.tool(annotations=READ_ONLY)
async def get_instance(instance: str, identify_mods: bool = True) -> dict:
    """Details of one instance (by name or folder): settings, mods (identified on Modrinth/CurseForge
    when identify_mods is true), resource packs, shader packs and worlds."""
    launcher = _launcher()
    try:
        inst = launcher.get_instance(instance)
    except LauncherError as e:
        _fail(e)
    _, sources, *_ = _deps()
    g = inst.game_dir
    mods = sorted(p for p in (g / "mods").glob("*") if p.is_file()) if (g / "mods").is_dir() else []
    known: dict[str, dict] = {}
    if identify_mods and mods:
        try:
            for info in (await _installed(sources, inst)).values():
                p = info["project"]
                known[info["file"].name] = {"title": p.get("title"), "source": p.get("source", "modrinth"),
                                            "version": info["version"].get("version_number")}
        except HttpError:
            pass

    def listing(sub: str) -> list[str]:
        d = g / sub
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    out = inst.summary()
    out.update({
        "path": str(inst.path),
        "notes": inst.cfg.get("notes", ""),
        "java": inst.cfg.get("JavaPath") if inst.cfg.get("OverrideJavaLocation") == "true" else "default",
        "memory_mb": inst.cfg.get("MaxMemAlloc") if inst.cfg.get("OverrideMemory") == "true" else "default",
        "components": [{"uid": c.get("uid"), "version": c.get("version")} for c in inst.components],
        "mod_files": [{"file": p.name, "enabled": not p.name.endswith(".disabled"), **known.get(p.name, {})} for p in mods],
        "resourcepacks": listing("resourcepacks"),
        "shaderpacks": listing("shaderpacks"),
        "worlds": listing("saves"),
        "minutes_played": int(inst.cfg.get("totalTimePlayed", "0") or 0) // 60,
    })
    return out


@mcp.tool(annotations=READ_ONLY)
async def search_mods(query: str, mc_version: str | None = None, loader: str | None = None,
                      project_type: str = "mod", source: str = "all", limit: int = 8) -> list[dict]:
    """Search for mods. project_type: mod, modpack, resourcepack or shader. loader: neoforge, forge,
    fabric, quilt. source: all, modrinth or curseforge."""
    _, sources, *_ = _deps()
    if loader and loader not in LOADERS:
        raise ToolError(f"loader must be one of {', '.join(LOADERS)}")
    wanted = ["modrinth", "curseforge"] if source == "all" else [source]
    out = []
    for src in wanted:
        try:
            hits = await sources.search(query, source=src, mc=mc_version, loader=loader,
                                        project_types=(project_type,), limit=min(limit, 25))
        except HttpError as e:
            _fail(e)
        for h in hits:
            if src == "curseforge":
                out.append({"title": h["title"], "slug": h["slug"], "id": h["id"], "source": src,
                            "description": h.get("description"), "downloads": h.get("downloads"),
                            "url": cf_page_url(h), "third_party_downloads": h.get("allow_distribution")})
            else:
                loader_names = {n for l in LOADERS.values() for n in l.modrinth}
                out.append({"title": h["title"], "slug": h["slug"], "source": src, "description": h.get("description"),
                            "downloads": h.get("downloads"), "loaders": sorted(set(h.get("categories", [])) & loader_names),
                            "newest_minecraft": h.get("versions", [])[-5:],
                            "url": f"https://modrinth.com/{h.get('project_type', 'mod')}/{h['slug']}"})
    return out


@mcp.tool(annotations=READ_ONLY)
async def plan_setup(mods: list[str], mc_version: str | None = None, loader: str | None = None,
                     allow_unstable: bool = False) -> dict:
    """Work out a full setup without changing anything. mods: names, slugs, IDs or URLs (Modrinth or
    CurseForge; empty for a plain instance). mc_version / loader (neoforge, forge, fabric, quilt, vanilla)
    are optional constraints; leave them out to get the newest combination every mod supports.
    allow_unstable also considers alpha builds. Returns a plan_id for create_instance."""
    _, _, _, resolver, _ = _deps()
    try:
        plan = await resolver.plan(mods, mc_version, loader, allow_unstable)
    except (ResolveError, HttpError) as e:
        _fail(e)
    launcher = _launcher()
    plan.java_path, java_warning = pick_java(plan.java_major, launcher.cfg.get("JavaPath") or "java")
    if java_warning:
        plan.warnings.append(java_warning)
    _plans[plan.id] = plan
    while len(_plans) > 50:
        _plans.popitem(last=False)
    out = plan.to_dict()
    out["plan_id"] = plan.id
    out["java"] = plan.java_path or "MultiMC default"
    return out


@mcp.tool(annotations=CHANGES)
async def create_instance(plan_id: str, name: str, group: str | None = None, memory_mb: int | None = None,
                          allow_unresolved: bool = False) -> dict:
    """Create a new MultiMC instance from a plan_setup result: downloads and verifies every file and adds the
    instance (it shows up in MultiMC right away). The folder is named after `name`. group sets the
    MultiMC group (only possible while MultiMC is closed). memory_mb overrides max RAM (default:
    MultiMC's setting, or 6144 for 80+ mods)."""
    plan = _plans.get(plan_id)
    if not plan:
        raise ToolError(f"Unknown or expired plan_id '{plan_id}'. Run plan_setup again.")
    if plan.unresolved and not allow_unresolved:
        raise ToolError("The plan has unresolved items: " + " | ".join(plan.unresolved)
                        + " Fix the request, or pass allow_unresolved=true to install the rest anyway.")
    launcher = _launcher()
    *_, dl = _deps()
    if memory_mb is None and sum(p.folder == "mods" for p in plan.files) >= 80:
        memory_mb = 6144
    files = _downloads(plan.files)
    requested = ", ".join(r["matched"] for r in plan.requested if r["matched"]) or "no mods"
    cfg = instance_cfg(name, f"Set up by the MultiMC MCP server: {requested}.", memory_mb, plan.java_path)

    async def populate(game_dir: Path) -> None:
        await place_files(dl, game_dir, files)

    try:
        result = await build_instance(launcher, name, plan.components(), cfg, populate, group)
    except (LauncherError, HttpError) as e:
        _fail(e)
    result.update({
        "name": name, "minecraft": plan.minecraft,
        "loader": f"{LOADERS[plan.loader].name} {plan.loader_version}" if plan.loader else "vanilla",
        "files_installed": len(files), "manual_downloads": manual_list(plan.files),
        "memory_mb": memory_mb or "default", "multimc_open": launcher.is_running(),
        "next": "It shows up in MultiMC now. MultiMC downloads Minecraft and the loader on first launch.",
    })
    return result


@mcp.tool(annotations=CHANGES)
async def add_mods(instance: str, mods: list[str], allow_unstable: bool = False, dry_run: bool = False) -> dict:
    """Add mods (plus their required dependencies) to an existing instance, matching its Minecraft version and
    loader. Mods already in the instance are recognised and not added twice."""
    launcher = _launcher()
    _, sources, _, resolver, dl = _deps()
    try:
        inst = launcher.get_instance(instance)
        launcher.require_not_running(inst)
        mc, loader = inst.mc_version, inst.loader
        if not mc:
            raise LauncherError(f"Can't tell which Minecraft version '{inst.name}' uses.")
        loader_key = loader[0] if loader and loader[0] in LOADERS else None
        installed = await _installed(sources, inst)
        installed_names = {n for info in installed.values() for n in identity(info["project"])}
        roots, notes = [], []
        for q in mods:
            project, note = await resolver.find_project(q, mc, loader_key)
            if not project:
                notes.append(note)
                continue
            if project["id"] in installed or identity(project) & installed_names:
                notes.append(f"{project['title']} is already installed.")
                continue
            roots.append((project["id"], "requested", None))
        walk = await resolver.walk(mc, loader_key, roots, installed=set(installed),
                                   installed_names=installed_names, allow_unstable=allow_unstable)
    except (LauncherError, ResolveError, HttpError) as e:
        _fail(e)
    picks = list(walk.picks.values())
    out = {
        "instance": inst.name, "minecraft": mc, "loader": f"{loader[0]} {loader[1]}" if loader else "vanilla",
        "to_install": [{"title": p.title, "source": p.source, "version": p.version_number, "file": p.filename,
                        "reason": p.reason} for p in picks if not p.manual],
        "manual_downloads": manual_list(picks), "unresolved": walk.unresolved, "warnings": walk.warnings + notes,
    }
    if dry_run or not picks:
        out["installed"] = False
        return out
    if walk.unresolved:
        raise ToolError("Not installing; unresolved: " + " | ".join(walk.unresolved))
    await place_files(dl, inst.game_dir, _downloads(picks))
    out["installed"] = True
    return out


@mcp.tool(annotations=CHANGES)
async def update_mods(instance: str, apply: bool = False, allow_unstable: bool = False) -> dict:
    """Check an instance's mods for newer builds for the same Minecraft version and loader. With apply=true
    it installs them (plus any new required dependencies) and moves the old jars to
    <instance>/.mcp-backup/<time>/."""
    launcher = _launcher()
    _, sources, _, resolver, dl = _deps()
    try:
        inst = launcher.get_instance(instance)
        if apply:
            launcher.require_not_running(inst)
        mc, loader = inst.mc_version, inst.loader
        loader_key = loader[0] if loader and loader[0] in LOADERS else None
        installed = await _installed(sources, inst)
        versions = await sources.versions_for(list(installed))
        updates = []
        for pid, info in installed.items():
            cur = info["version"]
            unstable = allow_unstable or cur.get("version_type") != "release"
            ptype = info["project"].get("project_type", "mod")
            best = best_version(versions[pid], mc, loader_key, ptype, unstable)
            if best and best["id"] != cur["id"] and _newer(best, cur):
                updates.append((pid, info, best))
        out = {"instance": inst.name, "applied": False, "updates": [
            {"title": info["project"].get("title"), "from": info["version"].get("version_number"),
             "to": best.get("version_number")} for pid, info, best in updates]}
        if not apply or not updates:
            return out
        updating = {pid for pid, _, _ in updates}
        walk = await resolver.walk(
            mc, loader_key, [(pid, "update", best["id"]) for pid, _, best in updates],
            installed=set(installed) - updating,
            installed_names={n for pid, i in installed.items() if pid not in updating for n in identity(i["project"])},
            allow_unstable=allow_unstable)
        if walk.unresolved:
            raise LauncherError("Not updating; unresolved: " + " | ".join(walk.unresolved))
        picks = list(walk.picks.values())
        manual = {p.project_id for p in picks if p.manual}
        files = _downloads(picks)
        # Fetch everything first so a failed download can't leave the instance half-updated.
        await asyncio.gather(*(dl.fetch(f["url"], f["sha1"], f.get("sha512")) for f in files))
        moved = backup(inst, [info["file"] for pid, info, _ in updates if pid not in manual])
        await place_files(dl, inst.game_dir, files)
    except (LauncherError, ResolveError, HttpError) as e:
        _fail(e)
    out.update(applied=True, backup=str(moved) if moved else None, manual_downloads=manual_list(picks),
               new_dependencies=[p.title for p in picks if p.reason != "update"])
    return out


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False))
def set_mod_enabled(instance: str, file: str, enabled: bool) -> dict:
    """Enable or disable a mod jar the way MultiMC does (renaming it to/from <file>.disabled)."""
    launcher = _launcher()
    try:
        inst = launcher.get_instance(instance)
        launcher.require_not_running(inst)
        mods = inst.game_dir / "mods"
        name = safe_name(file)
        base = name[:-len(".disabled")] if name.endswith(".disabled") else name
        on, off = mods / base, mods / f"{base}.disabled"
        src, dest = (off, on) if enabled else (on, off)
        if dest.exists() and not src.exists():
            return {"file": dest.name, "enabled": enabled, "changed": False}
        if not src.exists():
            raise LauncherError(f"No mod file '{file}' in {inst.name}.")
        src.rename(dest)
    except LauncherError as e:
        _fail(e)
    return {"file": dest.name, "enabled": enabled, "changed": True}


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False))
def rename_instance(instance: str, new_name: str) -> dict:
    """Rename an instance and its folder to match (MultiMC's own rename leaves the folder alone, because
    the folder is the instance's ID). Updates groups and the selected instance. MultiMC must be closed."""
    launcher = _launcher()
    try:
        return launcher.rename(launcher.get_instance(instance), new_name)
    except LauncherError as e:
        _fail(e)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False))
def match_folders_to_names(apply: bool = False) -> dict:
    """List instances whose folder name doesn't match their display name (what happens after renaming in
    MultiMC, or accepting the default version-number name). With apply=true, rename those folders to
    match. MultiMC must be closed to apply."""
    launcher = _launcher()
    mismatched = [i for i in launcher.list_instances() if launcher.sanitize(i.name) != i.folder]
    out = {"mismatched": [{"name": i.name, "folder": i.folder} for i in mismatched], "renamed": []}
    if apply:
        for inst in mismatched:
            try:
                out["renamed"].append(launcher.rename(inst, inst.name))
            except LauncherError as e:
                out.setdefault("errors", []).append(f"{inst.name}: {e}")
    return out


async def _find_modpack(sources: Sources, ref: str) -> dict:
    project = await sources.project(ref)
    if project and project.get("project_type") == "modpack":
        return project
    q = _norm(ref)
    for source in ["modrinth"] + (["curseforge"] if sources.cf else []):
        hits = await sources.search(ref, source=source, project_types=("modpack",), limit=10)
        match = next((h for h in hits if names_match(ref, h)), None) or next(
            (h for h in hits if q and _norm(h["title"]).startswith(q)), None)
        if match:
            return (await sources.projects([match["project_id"]]))[match["project_id"]]
    raise LauncherError(f"No modpack on {sources.names} is named '{ref}'. Try search_mods(project_type='modpack').")


@mcp.tool(annotations=CHANGES)
async def install_modpack(modpack: str, name: str | None = None, version: str | None = None,
                          mc_version: str | None = None, group: str | None = None,
                          memory_mb: int | None = None) -> dict:
    """Install a whole modpack (Modrinth or CurseForge) as a new instance. modpack: name, slug, URL, or a
    local .mrpack / CurseForge .zip path. version picks a pack version; mc_version picks the newest pack
    version for that Minecraft."""
    launcher = _launcher()
    _, sources, meta, _, dl = _deps()
    warnings: list[str] = []
    manual: list[dict] = []
    try:
        local = Path(modpack).expanduser()
        title, pack_version = None, None
        if local.suffix in (".mrpack", ".zip") and local.is_file():
            pack_path, pack_version, kind = local, local.stem, "mrpack" if local.suffix == ".mrpack" else "curseforge"
        else:
            project = await _find_modpack(sources, modpack)
            title = project["title"]
            if is_cf(project["id"]):
                kind = "curseforge"
                files = [f for f in await sources.cf.project_files(project["id"], mc=mc_version) if not f.get("server_pack")]
                if version:
                    files = [f for f in files if version in (f["version_number"], f["files"][0]["filename"], str(f["_file_id"]))]
                chosen = next((f for f in files if f["version_type"] == "release"), files[0] if files else None)
            else:
                kind = "mrpack"
                versions = await sources.mr.versions(project["id"])
                if version:
                    versions = [v for v in versions if version in (v.get("version_number"), v["id"])]
                if mc_version:
                    versions = [v for v in versions if mc_version in v.get("game_versions", [])]
                chosen = next((v for v in versions if v.get("version_type") == "release"), versions[0] if versions else None)
            if not chosen:
                raise LauncherError(f"{title} has no version matching that request.")
            f = primary_file(chosen)
            if not f.get("url"):
                raise LauncherError(f"{title}'s author blocks third-party downloads. Download it from "
                                    f"{cf_page_url(project, chosen.get('_file_id'))} and pass the .zip path.")
            pack_path = await dl.fetch(f["url"], f["hashes"]["sha1"], f["hashes"].get("sha512"))
            pack_version = chosen.get("version_number")

        if kind == "mrpack":
            index = read_mrpack(pack_path)
            components = mrpack_components(index)
            files, skipped = mrpack_files(index)
            pack_name = index.get("name") or title
            overrides = ("overrides", "client-overrides")
        else:
            if not sources.cf:
                raise LauncherError("CurseForge modpacks need CURSEFORGE_API_KEY set for the MCP server.")
            manifest = read_cf_manifest(pack_path)
            components = cf_components(manifest)
            pack_name = manifest.get("name") or title
            wanted = [f for f in manifest.get("files", []) if f.get("required", True)]
            full = await sources.cf.files([f["fileID"] for f in wanted])
            projects = await sources.projects([f"cf:{f['projectID']}" for f in wanted])
            files, skipped = [], []
            for entry in wanted:
                v = full.get(entry["fileID"])
                project = projects.get(f"cf:{entry['projectID']}", {"project_type": "mod", "slug": str(entry["projectID"])})
                if not v:
                    skipped.append(f"CurseForge file {entry['fileID']} (project {entry['projectID']}) no longer exists")
                    continue
                fl = v["files"][0]
                folder = FOLDERS.get(project.get("project_type"), "mods")
                if not fl["url"]:
                    manual.append({"title": project.get("title"), "file": fl["filename"], "put_in": folder,
                                   "download_from": cf_page_url(project, entry["fileID"])})
                    continue
                files.append({"url": fl["url"], "sha1": fl["hashes"]["sha1"], "relpath": f"{folder}/{safe_name(fl['filename'])}"})
            overrides = (manifest.get("overrides") or "overrides",)

        mc = components[0]["version"]
        for comp in components[1:]:
            known = {v["version"] for v in await meta.index(comp["uid"])}
            if comp["version"] not in known:
                warnings.append(f"MultiMC's metadata has no {comp['uid']} {comp['version']}; the instance may not launch.")
        java_path, java_warning = pick_java(await meta.java_major(mc), launcher.cfg.get("JavaPath") or "java")
        if java_warning:
            warnings.append(java_warning)
        mod_count = sum(1 for f in files if f["relpath"].startswith("mods/"))
        if memory_mb is None and mod_count >= 80:
            memory_mb = 6144
        inst_name = name or f"{mc} - {pack_name}"
        cfg = instance_cfg(inst_name, f"Modpack {pack_name} {pack_version or ''}, installed by the MultiMC MCP server.",
                           memory_mb, java_path)
        extracted = 0

        async def populate(game_dir: Path) -> None:
            nonlocal extracted
            await place_files(dl, game_dir, files)
            extracted = extract_overrides(pack_path, game_dir, overrides)

        result = await build_instance(launcher, inst_name, components, cfg, populate, group)
    except (KeyError, ValueError) as e:
        raise ToolError(f"Bad modpack file: {e}") from e
    except (LauncherError, HttpError) as e:
        _fail(e)
    result.update({
        "name": inst_name, "pack": f"{pack_name} {pack_version or ''}".strip(), "minecraft": mc,
        "components": [f"{c['uid']} {c['version']}" for c in components],
        "files_downloaded": len(files), "override_files": extracted, "skipped": skipped,
        "manual_downloads": manual, "memory_mb": memory_mb or "default", "warnings": warnings,
    })
    return result


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False))
def launch_instance(instance: str, server: str | None = None, world: str | None = None,
                    account: str | None = None) -> dict:
    """Start an instance in MultiMC (opens the game on the user's screen). server: host[:port] to join;
    world: singleplayer world folder (Minecraft 23w14a+); account: MultiMC profile name."""
    launcher = _launcher()
    try:
        inst = launcher.get_instance(instance)
        if inst.folder in launcher.running_instance_folders():
            raise LauncherError(f"'{inst.name}' is already running.")
        cmd = launcher.launch(inst, server, world, account)
    except LauncherError as e:
        _fail(e)
    return {"launched": inst.name, "command": f"MultiMC {cmd}",
            "note": "MultiMC downloads Minecraft, the loader and libraries on first launch, so it can take a minute."}


def main() -> None:
    mcp.run()
