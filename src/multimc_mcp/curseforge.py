"""CurseForge Core API client (https://docs.curseforge.com/rest-api/), key in CURSEFORGE_API_KEY.

Projects, files and dependencies are converted to the same shapes the Modrinth client
returns, with IDs prefixed "cf:" (projects) and "cf:file:" (files), so the resolver
handles both sources the same way.
"""

from __future__ import annotations

import os
import re

from .http import Http, HttpError

API = "https://api.curseforge.com/v1"
MINECRAFT = 432
PREFIX = "cf:"
FILE_PREFIX = "cf:file:"

CLASS_TYPES = {6: "mod", 12: "resourcepack", 6552: "shader", 4471: "modpack"}
TYPE_CLASSES = {v: k for k, v in CLASS_TYPES.items()}
LOADER_IDS = {1: "forge", 4: "fabric", 5: "quilt", 6: "neoforge"}
LOADER_TYPES = {v: k for k, v in LOADER_IDS.items()}
RELEASE_TYPES = {1: "release", 2: "beta", 3: "alpha"}
# relationType: 1 embedded library, 2 optional, 3 required, 4 tool, 5 incompatible, 6 include
RELATIONS = {1: "embedded", 2: "optional", 3: "required", 4: "optional", 5: "incompatible", 6: "embedded"}
SECTIONS = {"mod": "mc-mods", "resourcepack": "texture-packs", "shader": "shaders", "modpack": "modpacks"}

_URL_RE = re.compile(r"curseforge\.com/minecraft/(mc-mods|modpacks|texture-packs|shaders)/([^/?#]+)")
_MC_RE = re.compile(r"^\d+(\.\d+)+$")
_PRE_1_14 = re.compile(r"^1\.([0-9]|1[0-3])(\.|$)")


def api_key() -> str | None:
    return os.environ.get("CURSEFORGE_API_KEY") or None


def is_cf(ref: str) -> bool:
    return ref.startswith(PREFIX)


def mod_id(ref: str) -> int:
    return int(ref[len(PREFIX):])


def file_id(ref: str) -> int:
    return int(ref[len(FILE_PREFIX):])


def page_url(project: dict, file_id_: int | None = None) -> str:
    base = f"https://www.curseforge.com/minecraft/{SECTIONS.get(project.get('project_type', 'mod'), 'mc-mods')}/{project.get('slug')}"
    return f"{base}/files/{file_id_}" if file_id_ else base


def to_project(m: dict) -> dict:
    return {
        "id": f"{PREFIX}{m['id']}",
        "slug": m.get("slug", str(m["id"])),
        "title": m.get("name", ""),
        "project_type": CLASS_TYPES.get(m.get("classId"), "other"),
        "description": m.get("summary", ""),
        "downloads": m.get("downloadCount", 0),
        "source": "curseforge",
        "allow_distribution": m.get("allowModDistribution") is not False,
        "_indexes": m.get("latestFilesIndexes", []),
    }


def _loaders(game_versions: list[str]) -> list[str]:
    loaders = [g.lower() for g in game_versions if g.lower() in LOADER_TYPES]
    if not loaders and any(_PRE_1_14.match(g) for g in game_versions):
        loaders = ["forge"]  # old Forge files are often untagged; Fabric didn't exist before 1.14
    return loaders


def to_version(f: dict) -> dict:
    sha1 = next((h["value"] for h in f.get("hashes", []) if h.get("algo") == 1), "")
    gvs = [g for g in f.get("gameVersions", []) if _MC_RE.match(g)]
    return {
        "id": f"{FILE_PREFIX}{f['id']}",
        "project_id": f"{PREFIX}{f['modId']}",
        "version_number": f.get("displayName") or f.get("fileName", ""),
        "version_type": RELEASE_TYPES.get(f.get("releaseType"), "release"),
        "game_versions": gvs,
        "loaders": _loaders(f.get("gameVersions", [])),
        "date_published": f.get("fileDate", ""),
        "files": [{
            "url": f.get("downloadUrl") or "",
            "filename": f.get("fileName", ""),
            "primary": True,
            "size": f.get("fileLength", 0),
            "hashes": {"sha1": sha1},
        }],
        "dependencies": [
            {"project_id": f"{PREFIX}{d['modId']}", "version_id": None,
             "dependency_type": RELATIONS.get(d.get("relationType"), "optional")}
            for d in f.get("dependencies", [])
        ],
        "server_pack": f.get("isServerPack", False),
        "_file_id": f["id"],
    }


def index_versions(project: dict) -> list[dict]:
    """Partial versions from a project's latestFilesIndexes (newest file per game version, loader
    and release type). Enough to pick a file; `complete` fetches the rest."""
    by_file: dict[int, dict] = {}
    for e in project.get("_indexes", []):
        gv = e.get("gameVersion", "")
        if not _MC_RE.match(gv):
            continue
        v = by_file.setdefault(e["fileId"], {
            "id": f"{FILE_PREFIX}{e['fileId']}", "project_id": project["id"],
            "version_number": e.get("filename", ""), "version_type": RELEASE_TYPES.get(e.get("releaseType"), "release"),
            "game_versions": [], "loaders": [], "date_published": "", "files": [], "dependencies": [],
            "partial": True, "_file_id": e["fileId"],
        })
        if gv not in v["game_versions"]:
            v["game_versions"].append(gv)
        loader = LOADER_IDS.get(e.get("modLoader"))
        if loader is None and _PRE_1_14.match(gv):
            loader = "forge"
        if loader and loader not in v["loaders"]:
            v["loaders"].append(loader)
    # File IDs only go up, so they order files by upload time.
    return sorted(by_file.values(), key=lambda v: v["_file_id"], reverse=True)


# ---- CurseForge fingerprints (MurmurHash2, seed 1, over the file minus whitespace bytes) ----

def fingerprint(data: bytes) -> int:
    data = bytes(b for b in data if b not in (9, 10, 13, 32))
    m, r, length = 0x5BD1E995, 24, len(data)
    h = (1 ^ length) & 0xFFFFFFFF
    i = 0
    while length - i >= 4:
        k = int.from_bytes(data[i:i + 4], "little")
        k = (k * m) & 0xFFFFFFFF
        k ^= k >> r
        k = (k * m) & 0xFFFFFFFF
        h = (h * m) & 0xFFFFFFFF
        h ^= k
        i += 4
    rest = length - i
    if rest == 3:
        h ^= data[i + 2] << 16
    if rest >= 2:
        h ^= data[i + 1] << 8
    if rest >= 1:
        h ^= data[i]
        h = (h * m) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * m) & 0xFFFFFFFF
    h ^= h >> 15
    return h


class CurseForge:
    def __init__(self, http: Http, key: str):
        self.http = http
        self.headers = {"x-api-key": key, "Accept": "application/json"}

    async def _get(self, path: str, params: dict | None = None, ttl: float = 600):
        return (await self.http.get_json(f"{API}{path}", params=params, ttl=ttl, headers=self.headers))["data"]

    async def _post(self, path: str, body: dict):
        return (await self.http.post_json(f"{API}{path}", body, headers=self.headers))["data"]

    async def search(self, query: str, *, mc: str | None = None, loader: str | None = None,
                     project_types: tuple[str, ...] = ("mod",), limit: int = 10, slug: str | None = None) -> list[dict]:
        out: list[dict] = []
        for ptype in project_types:
            if ptype not in TYPE_CLASSES:
                continue
            params = {"gameId": MINECRAFT, "classId": TYPE_CLASSES[ptype], "sortField": 2, "sortOrder": "desc",
                      "pageSize": limit}
            if slug:
                params["slug"] = slug
            else:
                params["searchFilter"] = query
            if mc:
                params["gameVersion"] = mc
            if loader in LOADER_TYPES and ptype == "mod":
                params["modLoaderType"] = LOADER_TYPES[loader]
            out += [to_project(m) for m in await self._get("/mods/search", params)]
        return out

    async def project(self, ref: str) -> dict | None:
        ref = ref.strip()
        m = _URL_RE.search(ref)
        if is_cf(ref) or ref.isdigit():
            try:
                return to_project(await self._get(f"/mods/{mod_id(ref) if is_cf(ref) else int(ref)}"))
            except HttpError as e:
                if e.status == 404:
                    return None
                raise
        slug = m.group(2) if m else ref
        if not re.fullmatch(r"[a-z0-9-]+", slug):
            return None
        types = (next(t for t, s in SECTIONS.items() if s == m.group(1)),) if m else ("mod", "modpack", "resourcepack", "shader")
        hits = await self.search("", slug=slug, project_types=types, limit=1)
        return hits[0] if hits else None

    async def projects(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        mods = await self._post("/mods", {"modIds": [mod_id(i) for i in ids]})
        return {f"{PREFIX}{m['id']}": to_project(m) for m in mods}

    async def versions(self, project: dict) -> list[dict]:
        return index_versions(project)

    async def files(self, ids: list[int]) -> dict[int, dict]:
        """fileId -> full version dict, in batches."""
        out: dict[int, dict] = {}
        for i in range(0, len(ids), 500):
            for f in await self._post("/mods/files", {"fileIds": ids[i:i + 500]}):
                out[f["id"]] = to_version(f)
        return out

    async def version(self, version_id: str) -> dict | None:
        return (await self.files([file_id(version_id)])).get(file_id(version_id))

    async def project_files(self, project_id: str, mc: str | None = None, loader: str | None = None) -> list[dict]:
        params = {"pageSize": 50}
        if mc:
            params["gameVersion"] = mc
        if loader in LOADER_TYPES:
            params["modLoaderType"] = LOADER_TYPES[loader]
        files = await self._get(f"/mods/{mod_id(project_id)}/files", params)
        return [to_version(f) for f in files]

    async def by_fingerprint(self, prints: list[int]) -> dict[int, dict]:
        """fingerprint -> full version for files CurseForge recognises."""
        if not prints:
            return {}
        data = await self._post(f"/fingerprints/{MINECRAFT}", {"fingerprints": prints})
        return {m["file"]["fileFingerprint"]: to_version(m["file"]) for m in data.get("exactMatches", [])}
