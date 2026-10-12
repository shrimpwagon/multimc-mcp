"""Modrinth API v2 client (https://docs.modrinth.com/api/). No key needed."""

from __future__ import annotations

import asyncio
import json
import re

from .http import Http, HttpError

API = "https://api.modrinth.com/v2"

# project_type -> folder inside .minecraft
FOLDERS = {"mod": "mods", "resourcepack": "resourcepacks", "shader": "shaderpacks"}

_URL_RE = re.compile(r"modrinth\.com/(?:mod|plugin|resourcepack|shader|modpack|datapack|project)/([^/?#]+)")


def slug_from(ref: str) -> str:
    m = _URL_RE.search(ref)
    return m.group(1) if m else ref.strip()


class Modrinth:
    def __init__(self, http: Http):
        self.http = http

    async def search(self, query: str, *, mc: str | None = None, loaders: tuple[str, ...] = (),
                     project_types: tuple[str, ...] = ("mod",), limit: int = 10) -> list[dict]:
        facets = [[f"project_type:{t}" for t in project_types]]
        if mc:
            facets.append([f"versions:{mc}"])
        if loaders:
            facets.append([f"categories:{l}" for l in loaders])
        data = await self.http.get_json(f"{API}/search", params={
            "query": query, "facets": json.dumps(facets), "limit": limit, "index": "relevance",
        })
        return data.get("hits", [])

    async def project(self, ref: str) -> dict | None:
        try:
            return await self.http.get_json(f"{API}/project/{slug_from(ref)}", ttl=600)
        except HttpError as e:
            if e.status == 404:
                return None
            raise

    async def projects(self, ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            for p in await self.http.get_json(f"{API}/projects", params={"ids": json.dumps(chunk)}, ttl=600):
                out[p["id"]] = p
        return out

    async def versions(self, project_id: str) -> list[dict]:
        """Every version of a project, newest first."""
        data = await self.http.get_json(f"{API}/project/{project_id}/version",
                                        params={"include_changelog": "false"}, ttl=600)
        return sorted(data, key=lambda v: v.get("date_published", ""), reverse=True)

    async def version(self, version_id: str) -> dict | None:
        try:
            return await self.http.get_json(f"{API}/version/{version_id}", ttl=600)
        except HttpError as e:
            if e.status == 404:
                return None
            raise

    async def versions_by_sha1(self, sha1s: list[str]) -> dict[str, dict]:
        """sha1 -> the Modrinth version that file belongs to (files not on Modrinth are absent)."""
        if not sha1s:
            return {}
        return await self.http.post_json(f"{API}/version_files", {"hashes": sha1s, "algorithm": "sha1"})

    async def latest_by_sha1(self, sha1s: list[str], loaders: list[str], game_versions: list[str]) -> dict[str, dict]:
        """sha1 -> newest version of that file's project for these loaders + game versions."""
        if not sha1s:
            return {}
        return await self.http.post_json(f"{API}/version_files/update", {
            "hashes": sha1s, "algorithm": "sha1", "loaders": loaders, "game_versions": game_versions,
        })

    async def versions_for(self, project_ids: list[str]) -> dict[str, list[dict]]:
        results = await asyncio.gather(*(self.versions(pid) for pid in project_ids))
        return dict(zip(project_ids, results))


def primary_file(version: dict) -> dict:
    files = version.get("files") or []
    return next((f for f in files if f.get("primary")), files[0] if files else {})
