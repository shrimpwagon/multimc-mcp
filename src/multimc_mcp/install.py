"""Downloading, verifying and writing files into MultiMC instances."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Awaitable, Callable

from . import inifile
from .http import Http
from .launcher import BACKUP_DIR, STAGING_DIR, Instance, Launcher, LauncherError

PARALLEL_DOWNLOADS = 6


def cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "multimc-mcp"


def sha1_of(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(filename: str) -> str:
    name = PurePosixPath(filename).name
    if not name or name.startswith(".") or name != filename or "\\" in filename:
        raise LauncherError(f"Refusing odd file name '{filename}'.")
    return name


def safe_relpath(path: str) -> PurePosixPath:
    """A relative path that can't escape the folder it's joined to."""
    p = PurePosixPath(path.replace("\\", "/"))
    if p.is_absolute() or not p.parts or any(part in ("..", "") for part in p.parts) or ":" in p.parts[0]:
        raise LauncherError(f"Refusing unsafe path '{path}'.")
    return p


class Downloader:
    """Downloads into a cache keyed by sha1; nothing reaches an instance unverified."""

    def __init__(self, http: Http, root: Path | None = None):
        self.http = http
        self.root = (root or cache_dir()) / "files"
        self.sem = asyncio.Semaphore(PARALLEL_DOWNLOADS)

    async def fetch(self, url: str, sha1: str, sha512: str | None = None) -> Path:
        if not url.startswith("https://"):
            raise LauncherError(f"Refusing non-HTTPS download {url}")
        sha1 = sha1.lower()
        dest = self.root / sha1[:2] / sha1
        if dest.exists() and sha1_of(dest) == sha1:
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{sha1}.{uuid.uuid4().hex[:6]}.part")
        async with self.sem:
            h1, h512 = hashlib.sha1(), hashlib.sha512()
            try:
                async with self.http.client.stream("GET", url) as r:
                    if r.status_code != 200:
                        raise LauncherError(f"Download failed ({r.status_code}): {url}")
                    with tmp.open("wb") as f:
                        async for chunk in r.aiter_bytes(1 << 16):
                            f.write(chunk)
                            h1.update(chunk)
                            h512.update(chunk)
                if h1.hexdigest() != sha1 or (sha512 and h512.hexdigest() != sha512.lower()):
                    raise LauncherError(f"Hash mismatch for {url}; not installing it.")
                os.replace(tmp, dest)
            finally:
                tmp.unlink(missing_ok=True)
        return dest


async def place_files(dl: Downloader, game_dir: Path, files: list[dict]) -> list[str]:
    """files: {url, sha1, sha512?, relpath}. Returns the relpaths written."""

    async def one(f: dict) -> str:
        src = await dl.fetch(f["url"], f["sha1"], f.get("sha512"))
        dest = game_dir / safe_relpath(f["relpath"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not (dest.exists() and sha1_of(dest) == f["sha1"].lower()):
            shutil.copyfile(src, dest)
        return f["relpath"]

    return list(await asyncio.gather(*(one(f) for f in files)))


def instance_cfg(name: str, notes: str, memory_mb: int | None, java_path: str | None) -> dict[str, str]:
    cfg = {"InstanceType": "OneSix", "name": name, "iconKey": "default", "notes": notes}
    if memory_mb:
        cfg.update(OverrideMemory="true", MaxMemAlloc=str(memory_mb), MinMemAlloc=str(min(2048, memory_mb)))
    if java_path:
        cfg.update(OverrideJavaLocation="true", JavaPath=java_path)
    return cfg


async def build_instance(launcher: Launcher, name: str, components: list[dict], cfg: dict[str, str],
                         populate: Callable[[Path], Awaitable[None]], group: str | None) -> dict:
    """Build the instance in a staging folder MultiMC ignores, then rename it into place
    in one step so a running MultiMC never sees it half-written."""
    instances = launcher.instances_dir
    instances.mkdir(parents=True, exist_ok=True)
    staging = instances / STAGING_DIR / uuid.uuid4().hex[:12]
    game_dir = staging / ".minecraft"
    for sub in ("mods", "resourcepacks", "shaderpacks", "saves"):
        (game_dir / sub).mkdir(parents=True, exist_ok=True)
    try:
        await populate(game_dir)
        inifile.atomic_write_text(staging / "mmc-pack.json",
                                  json.dumps({"components": components, "formatVersion": 1}, indent=4) + "\n")
        inifile.write(staging / "instance.cfg", cfg)
        folder = launcher.free_folder_name(name)
        os.rename(staging, instances / folder)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        try:
            (instances / STAGING_DIR).rmdir()
        except OSError:
            pass

    result = {"folder": folder, "path": str(instances / folder)}
    if group:
        try:
            launcher.set_group(folder, group)
            result["group"] = group
        except LauncherError as e:
            result["group_note"] = str(e)
    return result


def backup(inst: Instance, files: list[Path]) -> Path | None:
    """Move files out of the instance into <instance>/.mcp-backup/<timestamp>/, keeping their relative paths."""
    if not files:
        return None
    dest_root = inst.path / BACKUP_DIR / time.strftime("%Y%m%d-%H%M%S")
    for f in files:
        rel = f.relative_to(inst.path)
        (dest_root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), dest_root / rel)
    return dest_root


# ---- Modrinth modpacks (.mrpack) ---------------------------------------------

MRPACK_LOADERS = {
    "forge": "net.minecraftforge",
    "neoforge": "net.neoforged",
    "fabric-loader": "net.fabricmc.fabric-loader",
    "quilt-loader": "org.quiltmc.quilt-loader",
}


def read_mrpack(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        index = json.loads(z.read("modrinth.index.json"))
    if index.get("formatVersion") != 1 or index.get("game") != "minecraft":
        raise LauncherError("Not a Minecraft .mrpack (format 1).")
    deps = index.get("dependencies", {})
    unknown = set(deps) - {"minecraft"} - set(MRPACK_LOADERS)
    if "minecraft" not in deps or unknown:
        raise LauncherError(f"Unsupported .mrpack dependencies: {sorted(unknown) or 'no minecraft version'}")
    return index


def mrpack_components(index: dict) -> list[dict]:
    deps = index["dependencies"]
    comps = [{"uid": "net.minecraft", "version": deps["minecraft"], "important": True}]
    for key, uid in MRPACK_LOADERS.items():
        if key in deps:
            comps.append({"uid": uid, "version": deps[key]})
    return comps


def mrpack_files(index: dict) -> tuple[list[dict], list[str]]:
    """Client-side files to download, and notes about skipped ones."""
    files, skipped = [], []
    for f in index.get("files", []):
        env = f.get("env") or {}
        if env.get("client") == "unsupported":
            continue
        rel = str(safe_relpath(f["path"]))
        urls = [u for u in f.get("downloads", []) if u.startswith("https://")]
        sha1 = (f.get("hashes") or {}).get("sha1")
        if not urls or not sha1:
            skipped.append(rel)
            continue
        if env.get("client") == "optional":
            skipped.append(f"{rel} (optional, left out)")
            continue
        files.append({"url": urls[0], "sha1": sha1, "sha512": f["hashes"].get("sha512"), "relpath": rel,
                      "size": f.get("fileSize", 0)})
    return files, skipped


def extract_overrides(path: Path, game_dir: Path, folders: tuple[str, ...] = ("overrides", "client-overrides")) -> int:
    count = 0
    with zipfile.ZipFile(path) as z:
        for prefix in (f"{f.strip('/')}/" for f in folders):
            for info in z.infolist():
                if not info.filename.startswith(prefix) or info.is_dir():
                    continue
                rel = safe_relpath(info.filename[len(prefix):])
                dest = game_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out)
                count += 1
    return count


# ---- CurseForge modpacks (manifest.json) ---------------------------------------

CF_LOADERS = {
    "forge": "net.minecraftforge",
    "neoforge": "net.neoforged",
    "fabric": "net.fabricmc.fabric-loader",
    "quilt": "org.quiltmc.quilt-loader",
}


def read_cf_manifest(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read("manifest.json"))
    if manifest.get("manifestType") != "minecraftModpack" or "minecraft" not in manifest:
        raise LauncherError("Not a CurseForge Minecraft modpack (manifest.json).")
    return manifest


def cf_components(manifest: dict) -> list[dict]:
    mc = manifest["minecraft"]
    comps = [{"uid": "net.minecraft", "version": mc["version"], "important": True}]
    loaders = mc.get("modLoaders") or []
    primary = next((l for l in loaders if l.get("primary")), loaders[0] if loaders else None)
    if primary:
        kind, _, version = primary["id"].partition("-")
        if kind not in CF_LOADERS:
            raise LauncherError(f"Unsupported mod loader in modpack: {primary['id']}")
        comps.append({"uid": CF_LOADERS[kind], "version": version})
    return comps
