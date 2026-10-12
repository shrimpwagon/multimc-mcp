"""One interface over Modrinth and (when a key is set) CurseForge. Modrinth is tried first;
CurseForge IDs carry a "cf:" prefix and are routed to the CurseForge client."""

from __future__ import annotations

import asyncio
from pathlib import Path

from .curseforge import CurseForge, fingerprint, is_cf
from .http import HttpError
from .install import sha1_of
from .modrinth import Modrinth


class Sources:
    def __init__(self, modrinth: Modrinth, curseforge: CurseForge | None):
        self.mr = modrinth
        self.cf = curseforge
        self._projects: dict[str, dict] = {}
        self.cf_search_blocked = False  # some CurseForge keys get 403 on /mods/search only

    @property
    def names(self) -> str:
        return "Modrinth or CurseForge" if self.cf else "Modrinth"

    @property
    def cf_hint(self) -> str:
        if not self.cf:
            return " CurseForge isn't enabled (no CURSEFORGE_API_KEY)."
        if self.cf_search_blocked:
            return (" CurseForge search is blocked for this API key, so CurseForge mods need their numeric "
                    "Project ID (on the mod's CurseForge page), passed as 'cf:<id>'.")
        return ""

    async def project(self, ref: str) -> dict | None:
        if is_cf(ref) or "curseforge.com" in ref:
            return await self._cf_project(ref)
        p = await self.mr.project(ref)
        if p is None and ref.isdigit():
            p = await self._cf_project(ref)
        return p

    async def _cf_project(self, ref: str) -> dict | None:
        if not self.cf:
            return None
        try:
            p = await self.cf.project(ref)
        except HttpError as e:
            if e.status != 403:
                raise
            self.cf_search_blocked = True  # slug lookups go through /mods/search
            return None
        if p:
            self._projects[p["id"]] = p
        return p

    async def search(self, query: str, *, source: str, mc: str | None = None, loader: str | None = None,
                     project_types: tuple[str, ...] = ("mod",), limit: int = 10) -> list[dict]:
        """Hits normalised to {project_id, slug, title, project_type, source, ...}."""
        if source == "curseforge":
            if not self.cf:
                return []
            try:
                hits = await self.cf.search(query, mc=mc, loader=loader, project_types=project_types, limit=limit)
            except HttpError as e:
                if e.status == 403:
                    self.cf_search_blocked = True
                    return []
                raise
            for h in hits:
                self._projects[h["id"]] = h
            return [{**h, "project_id": h["id"]} for h in hits]
        from .meta import LOADERS
        hits = await self.mr.search(query, mc=mc, loaders=LOADERS[loader].modrinth if loader else (),
                                    project_types=project_types, limit=limit)
        return [{**h, "source": "modrinth"} for h in hits]

    async def projects(self, ids: list[str]) -> dict[str, dict]:
        cf_ids = [i for i in ids if is_cf(i)]
        mr_ids = [i for i in ids if not is_cf(i)]
        out: dict[str, dict] = {}
        if mr_ids:
            out.update(await self.mr.projects(mr_ids))
        if cf_ids and self.cf:
            missing = [i for i in cf_ids if i not in self._projects]
            if missing:
                self._projects.update(await self.cf.projects(missing))
            out.update({i: self._projects[i] for i in cf_ids if i in self._projects})
        return out

    async def versions(self, project_id: str) -> list[dict]:
        if is_cf(project_id):
            if not self.cf:
                return []
            project = (await self.projects([project_id])).get(project_id)
            return await self.cf.versions(project) if project else []
        return await self.mr.versions(project_id)

    async def versions_for(self, ids: list[str]) -> dict[str, list[dict]]:
        results = await asyncio.gather(*(self.versions(i) for i in ids))
        return dict(zip(ids, results))

    async def version(self, version_id: str) -> dict | None:
        if is_cf(version_id):
            return await self.cf.version(version_id) if self.cf else None
        return await self.mr.version(version_id)

    async def complete(self, version: dict) -> dict:
        """CurseForge index entries are partial; fetch the full file (URL, hash, dependencies)."""
        if version.get("partial") and self.cf:
            full = await self.cf.version(version["id"])
            if full:
                return full
        return version

    async def identify(self, files: list[Path]) -> dict[Path, dict]:
        """Which project version each file is: Modrinth by sha1, then CurseForge by fingerprint."""
        if not files:
            return {}
        by_sha1 = {sha1_of(p): p for p in files}
        found: dict[Path, dict] = {}
        try:
            for h, v in (await self.mr.versions_by_sha1(list(by_sha1))).items():
                found[by_sha1[h]] = v
        except HttpError:
            pass
        rest = [p for p in files if p not in found]
        if rest and self.cf:
            prints = {fingerprint(p.read_bytes()): p for p in rest}
            try:
                for fp, v in (await self.cf.by_fingerprint(list(prints))).items():
                    found[prints[fp]] = v
            except HttpError:
                pass
        return found
