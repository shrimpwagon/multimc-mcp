"""Version metadata: MultiMC's meta server (what MultiMC can actually launch),
Mojang's version files (which Java each Minecraft version needs), and the JDKs
installed on this machine."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .http import Http

META = "https://meta.multimc.org/v1"
MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"


@dataclass(frozen=True)
class Loader:
    key: str
    name: str
    uid: str
    modrinth: tuple[str, ...]  # Modrinth loader names whose mods this loader can run


LOADERS: dict[str, Loader] = {
    "neoforge": Loader("neoforge", "NeoForge", "net.neoforged", ("neoforge",)),
    "forge": Loader("forge", "Forge", "net.minecraftforge", ("forge",)),
    "fabric": Loader("fabric", "Fabric", "net.fabricmc.fabric-loader", ("fabric",)),
    # Quilt runs most Fabric mods.
    "quilt": Loader("quilt", "Quilt", "org.quiltmc.quilt-loader", ("quilt", "fabric")),
}


class Meta:
    def __init__(self, http: Http):
        self.http = http

    async def index(self, uid: str) -> list[dict]:
        data = await self.http.get_json(f"{META}/{uid}/index.json", ttl=1800)
        return sorted(data.get("versions", []), key=lambda v: v.get("releaseTime", ""), reverse=True)

    async def minecraft_versions(self) -> dict[str, dict]:
        """version -> {type, releaseTime}, newest first."""
        return {v["version"]: v for v in await self.index("net.minecraft")}

    async def newest_release(self) -> str:
        for v, info in (await self.minecraft_versions()).items():
            if info.get("type") == "release":
                return v
        raise RuntimeError("No Minecraft release in MultiMC's metadata")

    async def loader_version(self, loader: str, mc: str) -> str | None:
        """The loader build to use for this Minecraft version, or None if MultiMC can't run it."""
        if loader in ("fabric", "quilt"):
            # Both need intermediary mappings for the exact MC version (26.x has empty stub ones).
            if not any(v["version"] == mc for v in await self.index("net.fabricmc.intermediary")):
                return None
            versions = await self.index(LOADERS[loader].uid)
            for pick in (
                lambda v: v.get("recommended") and v.get("type", "release") == "release",
                lambda v: v.get("type", "release") == "release",
                lambda v: True,
            ):
                for v in versions:
                    if pick(v):
                        return v["version"]
            return None

        versions = [
            v for v in await self.index(LOADERS[loader].uid)
            if any(r.get("uid") == "net.minecraft" and r.get("equals") == mc for r in v.get("requires", []))
        ]
        for pick in (
            lambda v: v.get("recommended"),
            lambda v: "beta" not in v["version"] and "alpha" not in v["version"],
            lambda v: True,
        ):
            for v in versions:
                if pick(v):
                    return v["version"]
        return None

    async def java_major(self, mc: str) -> int | None:
        manifest = await self.http.get_json(MOJANG_MANIFEST, ttl=3600)
        url = next((v["url"] for v in manifest.get("versions", []) if v["id"] == mc), None)
        if not url:
            return None
        data = await self.http.get_json(url, ttl=86400)
        return (data.get("javaVersion") or {}).get("majorVersion") or 8


# ---- installed JDKs ----------------------------------------------------------

_RELEASE_RE = re.compile(r'^JAVA_VERSION="([^"]+)"', re.M)


def _major(version: str) -> int | None:
    m = re.match(r"(\d+)(?:\.(\d+))?", version)
    if not m:
        return None
    first = int(m.group(1))
    return int(m.group(2) or 0) if first == 1 else first  # "1.8.0_402" -> 8


def _java_version(java: Path) -> int | None:
    home = java.resolve().parent.parent
    for release in (home / "release", home.parent / "release"):
        if release.is_file():
            m = _RELEASE_RE.search(release.read_text(errors="replace"))
            if m:
                return _major(m.group(1))
    try:
        out = subprocess.run([str(java), "-version"], capture_output=True, text=True, timeout=15).stderr
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r'version "([^"]+)"', out)
    return _major(m.group(1)) if m else None


def installed_javas() -> dict[int, str]:
    """major version -> path of a java binary, preferring system JDKs."""
    candidates: list[Path] = []
    for root in (Path("/usr/lib/jvm"), Path("/usr/java"), Path.home() / ".jdks", Path.home() / ".sdkman/candidates/java"):
        if root.is_dir():
            for d in sorted(root.iterdir()):
                for rel in ("bin/java", "jre/bin/java"):
                    if (d / rel).is_file() and os.access(d / rel, os.X_OK):
                        candidates.append(d / rel)
                        break
    on_path = shutil.which("java")
    if on_path:
        candidates.insert(0, Path(on_path))
    found: dict[int, str] = {}
    seen: set[Path] = set()
    for java in candidates:
        real = java.resolve()
        if real in seen:
            continue
        seen.add(real)
        major = _java_version(java)
        if major and major not in found:
            found[major] = str(real)
    return found


def pick_java(required: int | None, default_java: str = "java") -> tuple[str | None, str | None]:
    """(java path to pin on the instance or None to use MultiMC's default, warning or None)."""
    if not required:
        return None, None
    javas = installed_javas()
    default_path = shutil.which(default_java) or default_java
    default_major = _java_version(Path(default_path)) if Path(default_path).exists() else None
    if default_major == required:
        return None, None
    if required in javas:
        return javas[required], None
    # Minecraft 1.17+ generally runs on newer Java than it asks for; 1.16 and older want exactly 8.
    if required >= 17:
        newer = sorted(m for m in javas if m > required)
        if default_major and default_major > required:
            return None, f"Java {required} isn't installed; using the default Java {default_major}, which usually works."
        if newer:
            return javas[newer[0]], f"Java {required} isn't installed; using Java {newer[0]}, which usually works."
    return None, (
        f"This Minecraft version needs Java {required}, which isn't installed "
        f"(found: {', '.join(map(str, sorted(javas))) or 'none'}). Install it, e.g. "
        f"`sudo apt install openjdk-{required}-jre`, then set it in the instance's settings."
    )
