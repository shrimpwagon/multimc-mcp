"""Turn "these mods" into a concrete, launchable setup.

1. Match each request to a project: slug, ID or URL as-is, otherwise a search hit whose
   name really is the request (Modrinth first, then CurseForge).
2. Find the (Minecraft version, loader) pairs every requested project has a build for,
   keep the ones MultiMC's metadata can launch, newest Minecraft first.
3. For a pair, pick each project's newest release (then beta) and walk `required`
   dependencies recursively. The first pair where everything resolves wins.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import asdict, dataclass, field

from .curseforge import page_url as cf_page_url
from .meta import LOADERS, Meta, installed_javas
from .modrinth import FOLDERS, primary_file
from .sources import Sources

MAX_PAIRS_TRIED = 6


class ResolveError(Exception):
    pass


@dataclass
class Pick:
    project_id: str
    slug: str
    title: str
    project_type: str
    source: str
    version_id: str
    version_number: str
    version_type: str
    filename: str
    url: str
    sha1: str
    sha512: str | None
    size: int
    folder: str
    reason: str
    page: str
    date_published: str = ""

    @property
    def manual(self) -> bool:
        """CurseForge files whose authors block third-party downloads."""
        return not self.url


@dataclass
class Plan:
    minecraft: str
    loader: str | None
    loader_version: str | None
    files: list[Pick] = field(default_factory=list)
    requested: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    optional_suggestions: list[dict] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    java_major: int | None = None
    java_path: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def components(self) -> list[dict]:
        comps = [{"uid": "net.minecraft", "version": self.minecraft, "important": True}]
        if self.loader:
            comps.append({"uid": LOADERS[self.loader].uid, "version": self.loader_version})
        return comps

    def to_dict(self) -> dict:
        d = asdict(self)
        d["loader"] = f"{LOADERS[self.loader].name} {self.loader_version}" if self.loader else "vanilla"
        d["files"] = [
            {k: p[k] for k in ("title", "source", "version_number", "version_type", "filename", "folder", "reason")}
            for p in d["files"] if p["url"]
        ]
        d["manual_downloads"] = manual_list(self.files)
        d["total_download_mb"] = round(sum(p.size for p in self.files) / 1e6, 1)
        d["ready"] = not self.unresolved
        d.pop("java_path")
        return d


def manual_list(picks: list[Pick]) -> list[dict]:
    return [{"title": p.title, "file": p.filename, "put_in": p.folder, "download_from": p.page, "reason": p.reason}
            for p in picks if p.manual]


def _norm(text: str) -> str:
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text.lower()).replace("'s", "s")
    return re.sub(r"[^a-z0-9]+", "", text)


def names_match(query: str, hit: dict) -> bool:
    """True when a search hit is plausibly the thing the user named, not just something related."""
    q = _norm(query)
    if not q:
        return False
    title = hit.get("title", "")
    acronym = re.search(r"\(([^)]+)\)", title)  # "Just Enough Items (JEI)"
    names = {_norm(title), _norm(hit.get("slug", "")), re.sub(r"[^a-z0-9]", "", title.lower())}
    if acronym:
        names.add(_norm(acronym.group(1)))
    return q in names


def identity(project: dict) -> set[str]:
    """Names that mean "the same mod" across Modrinth and CurseForge."""
    return {n for n in (_norm(project.get("slug", "")), _norm(project.get("title", ""))) if n}


def _accepts(version: dict, mc: str, loader: str | None, project_type: str) -> bool:
    if mc not in version.get("game_versions", []):
        return False
    if project_type != "mod" or loader is None:
        return True
    return bool(set(version.get("loaders", [])) & set(LOADERS[loader].modrinth))


def best_version(versions: list[dict], mc: str, loader: str | None, project_type: str,
                 allow_unstable: bool = False) -> dict | None:
    """Newest release for the pair, else newest beta, else (if allowed) newest alpha."""
    fits = [v for v in versions if _accepts(v, mc, loader, project_type) and not v.get("server_pack")]
    tiers = ["release", "beta"] + (["alpha"] if allow_unstable else [])
    for tier in tiers:
        for v in fits:  # versions are newest first
            if v.get("version_type") == tier:
                return v
    return None


def supported_pairs(versions: list[dict], project_type: str, allow_unstable: bool) -> set[tuple[str, str | None]]:
    allowed = {"release", "beta"} | ({"alpha"} if allow_unstable else set())
    pairs: set[tuple[str, str | None]] = set()
    for v in versions:
        if v.get("version_type") not in allowed:
            continue
        for mc in v.get("game_versions", []):
            if project_type != "mod":
                pairs.add((mc, None))
                continue
            for key, loader in LOADERS.items():
                if set(v.get("loaders", [])) & set(loader.modrinth):
                    pairs.add((mc, key))
    return pairs


def make_pick(project: dict, version: dict, reason: str) -> Pick:
    f = primary_file(version)
    source = project.get("source", "modrinth")
    ptype = project.get("project_type", "mod")
    page = (cf_page_url(project, version.get("_file_id")) if source == "curseforge"
            else f"https://modrinth.com/{ptype}/{project.get('slug')}")
    return Pick(
        project_id=project["id"],
        slug=project.get("slug", project["id"]),
        title=project.get("title", project.get("slug", "")),
        project_type=ptype,
        source=source,
        version_id=version["id"],
        version_number=version.get("version_number", ""),
        version_type=version.get("version_type", ""),
        filename=f.get("filename", ""),
        url=f.get("url", ""),
        sha1=(f.get("hashes") or {}).get("sha1", ""),
        sha512=(f.get("hashes") or {}).get("sha512"),
        size=f.get("size", 0),
        folder=FOLDERS.get(ptype, "mods"),
        reason=reason,
        page=page,
        date_published=version.get("date_published", ""),
    )


@dataclass
class Walk:
    picks: dict[str, Pick] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    optional: dict[str, str] = field(default_factory=dict)  # project id -> wanted by
    incompatible: list[tuple[str, str]] = field(default_factory=list)  # (project id, declared by)


class Resolver:
    def __init__(self, sources: Sources, meta: Meta):
        self.src = sources
        self.meta = meta

    # ---- matching requests to projects -----------------------------------

    async def find_project(self, query: str, mc: str | None = None, loader: str | None = None) -> tuple[dict | None, str | None]:
        """A slug/ID/URL is used as-is. Free text only counts when a search hit's name really is
        that text, so "dragon survival" never silently becomes "Dragon Survival Compatibility".
        Mods beat modpacks, and Modrinth beats CurseForge."""
        project = await self.src.project(query)
        if project:
            return project, None
        seen: list[dict] = []
        sources = ["modrinth"] + (["curseforge"] if self.src.cf else [])
        for types in (("mod", "resourcepack", "shader"), ("modpack",)):
            for source in sources:
                hits = await self.src.search(query, source=source, project_types=types, limit=10)
                seen += hits
                match = next((h for h in hits if names_match(query, h)), None)
                if match:
                    found = await self.src.projects([match["project_id"]])
                    if match["project_id"] in found:
                        return found[match["project_id"]], None
        closest = ", ".join(f"{h['title']} ({h['slug']}, {h['source']})" for h in seen[:4])
        return None, (
            f"Nothing on {self.src.names} is named '{query}'."
            + (f" Closest: {closest}. Pass the slug (or a URL) to use one." if closest else "")
            + self.src.cf_hint
        )

    # ---- dependency walk for one pair --------------------------------------

    async def walk(self, mc: str, loader: str | None, roots: list[tuple[str, str, str | None]],
                   installed: set[str] = frozenset(), installed_names: set[str] = frozenset(),
                   allow_unstable: bool = False) -> Walk:
        """roots: (project id, reason, pinned version id or None). installed / installed_names
        describe what's already in the instance so it isn't added twice."""
        w = Walk()
        names: dict[str, str] = {n: "installed" for n in installed_names}
        projects: dict[str, dict] = {}
        versions: dict[str, list[dict]] = {}
        level = list(roots)
        while level:
            level = [item for item in level if item[0] not in w.picks and item[0] not in installed]
            need = list({pid for pid, _, _ in level if pid not in projects})
            if need:
                projects.update(await self.src.projects(need))
            need = list({pid for pid, _, _ in level if pid not in versions and pid in projects})
            if need:
                versions.update(await self.src.versions_for(need))
            next_level: list[tuple[str, str, str | None]] = []
            for pid, reason, pinned in level:
                if pid in w.picks:
                    continue
                project = projects.get(pid)
                if not project:
                    w.unresolved.append(f"Project {pid} ({reason}) can't be found anymore.")
                    continue
                if reason != "requested" and identity(project) & set(names):
                    continue  # same library already chosen from the other source
                ptype = project.get("project_type", "mod")
                title = project.get("title", pid)
                if ptype not in FOLDERS:
                    w.warnings.append(f"Skipped {title}: it's a {ptype}, not something that goes in an instance folder.")
                    continue
                version = None
                if pinned:
                    pv = next((v for v in versions.get(pid, []) if v["id"] == pinned), None) or await self.src.version(pinned)
                    if pv and _accepts(pv, mc, loader, ptype):
                        version = pv
                if not version:
                    version = best_version(versions.get(pid, []), mc, loader, ptype, allow_unstable)
                if not version:
                    what = f"{LOADERS[loader].name} " if loader and ptype == "mod" else ""
                    w.unresolved.append(f"{title} ({project.get('slug')}) has no {what}build for Minecraft {mc} ({reason}).")
                    continue
                version = await self.src.complete(version)
                pick = make_pick(project, version, reason)
                if not pick.sha1 and not pick.manual:
                    w.unresolved.append(f"{title} {pick.version_number} has no downloadable file.")
                    continue
                if pick.version_type != "release":
                    w.warnings.append(f"{title}: no release build for this setup, using {pick.version_type} {pick.version_number}.")
                w.picks[pid] = pick
                for n in identity(project):
                    names.setdefault(n, pid)
                for dep in version.get("dependencies", []):
                    kind, dpid, dvid = dep.get("dependency_type"), dep.get("project_id"), dep.get("version_id")
                    if kind == "required":
                        if not dpid and dvid:
                            dv = await self.src.version(dvid)
                            dpid = dv.get("project_id") if dv else None
                        if dpid:
                            next_level.append((dpid, f"required by {title}", dvid))
                        else:
                            w.unresolved.append(f"{title} needs '{dep.get('file_name') or 'a file'}', which isn't on {self.src.names}.")
                    elif kind == "optional" and dpid:
                        w.optional.setdefault(dpid, title)
                    elif kind == "incompatible" and dpid:
                        w.incompatible.append((dpid, title))
            level = next_level

        chosen = set(w.picks) | set(installed)
        for dpid, by in w.incompatible:
            if dpid in chosen:
                other = w.picks[dpid].title if dpid in w.picks else f"installed project {dpid}"
                w.unresolved.append(f"{by} says it's incompatible with {other}.")
        return w

    # ---- full planning -----------------------------------------------------

    @staticmethod
    def _rank(mc_versions: dict[str, dict], mc: str, loader: str | None) -> tuple:
        modern_from = mc_versions.get("1.20.2", {}).get("releaseTime", "")
        released = mc_versions.get(mc, {}).get("releaseTime", "")
        prefs = ["neoforge", "fabric", "forge", "quilt"] if released >= modern_from else ["forge", "fabric", "quilt", "neoforge"]
        return (released, -(prefs.index(loader) if loader in prefs else len(prefs)))

    async def plan(self, mods: list[str], mc_version: str | None = None, loader: str | None = None,
                   allow_unstable: bool = False) -> Plan:
        vanilla = loader in ("vanilla", "none", "")
        if vanilla:
            loader = None
        if loader and loader not in LOADERS:
            raise ResolveError(f"Unknown loader '{loader}'. Use one of: {', '.join(LOADERS)}, or vanilla.")

        mc_versions = await self.meta.minecraft_versions()
        if mc_version and mc_version not in mc_versions:
            raise ResolveError(f"MultiMC doesn't know Minecraft '{mc_version}'.")

        requested, roots, unmatched = [], [], []
        projects: dict[str, dict] = {}
        for query in mods:
            project, note = await self.find_project(query, mc_version, loader)
            if not project:
                requested.append({"query": query, "matched": None, "note": note})
                unmatched.append(note)
                continue
            label = f"{project['title']} ({project['slug']}, {project.get('source', 'modrinth')})"
            if project.get("project_type") == "modpack":
                note = f"{project['title']} is a modpack. Use install_modpack('{project['slug']}') instead."
                requested.append({"query": query, "matched": label, "note": note})
                unmatched.append(note)
                continue
            requested.append({"query": query, "matched": label, "note": None})
            if project["id"] not in projects:
                projects[project["id"]] = project
                roots.append((project["id"], "requested", None))

        if vanilla and any(p.get("project_type") == "mod" for p in projects.values()):
            raise ResolveError("Mods need a loader; drop loader='vanilla' or pick one.")

        # Pairs every requested project supports.
        all_versions = await self.src.versions_for(list(projects))
        candidate: set[tuple[str, str | None]] | None = None
        mc_only: set[str] | None = None
        for pid, project in projects.items():
            pairs = supported_pairs(all_versions[pid], project.get("project_type", "mod"), allow_unstable)
            if project.get("project_type") == "mod":
                candidate = pairs if candidate is None else candidate & pairs
            else:
                mcs = {mc for mc, _ in pairs}
                mc_only = mcs if mc_only is None else mc_only & mcs
        if candidate is None:
            base = mc_only if mc_only is not None else {mc_version or await self.meta.newest_release()}
            candidate = {(mc, loader) for mc in base}
        elif mc_only is not None:
            candidate = {(mc, l) for mc, l in candidate if mc in mc_only}

        ordered = sorted(
            ((mc, l) for mc, l in candidate
             if (not mc_version or mc == mc_version) and (not loader or l == loader)
             and mc in mc_versions and (mc_versions[mc].get("type") == "release" or mc == mc_version)),
            key=lambda p: self._rank(mc_versions, *p), reverse=True,
        )
        viable: list[tuple[str, str | None, str | None]] = []
        for mc, l in ordered:
            lv = await self.meta.loader_version(l, mc) if l else None
            if l and not lv:
                continue
            viable.append((mc, l, lv))
            if len(viable) >= 40:
                break

        # Prefer setups where every requested mod has a stable release and the needed Java is
        # installed, then the newest Minecraft, then the loader preference.
        javas = installed_javas()
        majors = dict(zip({mc for mc, _, _ in viable}, await asyncio.gather(
            *(self.meta.java_major(mc) for mc in {mc for mc, _, _ in viable}))))

        def stable(mc: str, l: str | None) -> bool:
            for pid, project in projects.items():
                v = best_version(all_versions[pid], mc, l, project.get("project_type", "mod"))
                if not v or v.get("version_type") != "release":
                    return False
            return True

        def java_ok(mc: str) -> bool:
            need = majors.get(mc)
            return not need or need in javas or (need >= 17 and any(m > need for m in javas))

        viable.sort(key=lambda t: (stable(t[0], t[1]), java_ok(t[0]), self._rank(mc_versions, t[0], t[1])), reverse=True)

        if not viable:
            wanted = " + ".join(x for x in (mc_version, loader) if x) or "any Minecraft version"
            names = ", ".join(p["title"] for p in projects.values()) or "this setup"
            raise ResolveError(
                f"No Minecraft version + loader that MultiMC can launch has builds of all of: {names} "
                f"(constraint: {wanted}). Try fewer mods, a different version, or allow_unstable=True."
                + (f" Also: {' '.join(unmatched)}" if unmatched else "")
            )

        best: tuple[Walk, tuple] | None = None
        for mc, l, lv in viable[:MAX_PAIRS_TRIED]:
            w = await self.walk(mc, l, roots, allow_unstable=allow_unstable)
            if best is None or len(w.unresolved) < len(best[0].unresolved):
                best = (w, (mc, l, lv))
            if not w.unresolved:
                break
        assert best is not None
        w, (mc, l, lv) = best

        plan = Plan(minecraft=mc, loader=l, loader_version=lv, requested=requested)
        plan.files = sorted(w.picks.values(), key=lambda p: (p.reason != "requested", p.title.lower()))
        plan.warnings = w.warnings
        plan.unresolved = w.unresolved + unmatched
        if w.optional:
            opt_ids = [pid for pid in w.optional if pid not in w.picks]
            info = await self.src.projects(opt_ids) if opt_ids else {}
            chosen_names = {n for p in w.picks.values() for n in (_norm(p.slug), _norm(p.title))}
            plan.optional_suggestions = [
                {"title": info[pid]["title"], "slug": info[pid]["slug"], "suggested_by": w.optional[pid]}
                for pid in opt_ids if pid in info and not identity(info[pid]) & chosen_names
            ]
        plan.alternatives = [
            f"Minecraft {m}" + (f" + {LOADERS[x].name} {v}" if x else "")
            for m, x, v in viable if (m, x) != (mc, l)
        ][:6]
        plan.java_major = await self.meta.java_major(mc)
        return plan
