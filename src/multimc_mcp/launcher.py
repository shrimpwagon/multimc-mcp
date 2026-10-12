"""The installed MultiMC: data dir, instances, groups, running state, launching.

Everything here follows the on-disk formats in MultiMC/Launcher
(InstanceList.cpp, MinecraftInstance.cpp, PackProfile.cpp, FileSystem.cpp).
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import inifile

# FileSystem.cpp: badFilenameChars, replaced with '-' in instance folder names.
BAD_FILENAME_CHARS = set('"\\/?<>:;*|!+\r\n')

# Folder (relative to an instance) where this server stages new instances and
# keeps backups. It has no instance.cfg, so MultiMC ignores it.
STAGING_DIR = ".mcp-staging"
BACKUP_DIR = ".mcp-backup"

LOADER_UIDS = {
    "net.neoforged": "neoforge",
    "net.minecraftforge": "forge",
    "net.fabricmc.fabric-loader": "fabric",
    "org.quiltmc.quilt-loader": "quilt",
    "com.mumfrey.liteloader": "liteloader",
}


class LauncherError(Exception):
    pass


def find_data_dir() -> Path:
    """MULTIMC_DIR, else the .deb wrapper's ~/.local/share/multimc, else a portable copy in ~/MultiMC."""
    env = os.environ.get("MULTIMC_DIR")
    if env:
        return Path(env).expanduser()
    xdg = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    for candidate in (xdg / "multimc", Path.home() / "MultiMC"):
        if (candidate / "multimc.cfg").exists() or (candidate / "MultiMC").exists():
            return candidate
    raise LauncherError(
        "Can't find MultiMC's data folder (looked in ~/.local/share/multimc and ~/MultiMC). "
        "Set MULTIMC_DIR to the folder that holds multimc.cfg."
    )


@dataclass
class Instance:
    folder: str
    path: Path
    cfg: dict[str, str]
    components: list[dict]
    group: str | None = None
    running: bool = False

    @property
    def name(self) -> str:
        return self.cfg.get("name") or self.folder

    def component_version(self, uid: str) -> str | None:
        for c in self.components:
            if c.get("uid") == uid:
                return c.get("version")
        return None

    @property
    def mc_version(self) -> str | None:
        return self.component_version("net.minecraft") or self.cfg.get("IntendedVersion") or None

    @property
    def loader(self) -> tuple[str, str] | None:
        """(loader key, version), e.g. ("neoforge", "21.1.243"); None for vanilla."""
        for c in self.components:
            key = LOADER_UIDS.get(c.get("uid", ""))
            if key:
                return key, c.get("version", "")
        return None

    @property
    def game_dir(self) -> Path:
        # MinecraftInstance::gameRoot(): "minecraft" only if ".minecraft" doesn't exist.
        mc, dot = self.path / "minecraft", self.path / ".minecraft"
        return mc if mc.exists() and not dot.exists() else dot

    def summary(self) -> dict:
        loader = self.loader
        mods_dir = self.game_dir / "mods"
        mods = [p for p in mods_dir.iterdir() if p.is_file()] if mods_dir.is_dir() else []
        return {
            "name": self.name,
            "folder": self.folder,
            "minecraft": self.mc_version,
            "loader": f"{loader[0]} {loader[1]}" if loader else "vanilla",
            "mods": sum(1 for p in mods if p.name.endswith(".jar")),
            "disabled_mods": sum(1 for p in mods if p.name.endswith(".disabled")),
            "group": self.group,
            "running": self.running,
        }


@dataclass
class Launcher:
    data_dir: Path = field(default_factory=find_data_dir)

    @property
    def cfg(self) -> dict[str, str]:
        return inifile.read(self.data_dir / "multimc.cfg")

    @property
    def instances_dir(self) -> Path:
        d = Path(self.cfg.get("InstanceDir") or "instances").expanduser()
        return d if d.is_absolute() else self.data_dir / d

    @property
    def binary(self) -> Path:
        return self.data_dir / "MultiMC"

    # ---- processes ---------------------------------------------------------

    def _procs(self):
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                yield entry

    def is_running(self) -> bool:
        """True if the MultiMC GUI from this data dir is running."""
        targets = {str(self.binary.resolve()), str((self.data_dir / "bin" / "MultiMC").resolve())}
        for proc in self._procs():
            try:
                exe = os.readlink(proc / "exe")
            except OSError:
                continue
            if exe in targets:
                return True
        return False

    def running_instance_folders(self) -> set[str]:
        """Instance folders that have a Java process working inside them (a running game)."""
        root = self.instances_dir.resolve()
        found: set[str] = set()
        for proc in self._procs():
            try:
                comm = (proc / "comm").read_text().strip()
                if "java" not in comm:
                    continue
                cwd = Path(os.readlink(proc / "cwd"))
            except OSError:
                continue
            try:
                rel = cwd.relative_to(root)
            except ValueError:
                continue
            if rel.parts:
                found.add(rel.parts[0])
        return found

    # ---- instances ---------------------------------------------------------

    def groups(self) -> dict[str, str]:
        """instance folder -> group name, from instgroups.json."""
        path = self.instances_dir / "instgroups.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        out = {}
        for group, info in (data.get("groups") or {}).items():
            for folder in info.get("instances", []):
                out[folder] = group
        return out

    def _load(self, folder: str, groups: dict[str, str], running: set[str]) -> Instance | None:
        path = self.instances_dir / folder
        cfg_path = path / "instance.cfg"
        if not cfg_path.is_file():
            return None
        try:
            pack = json.loads((path / "mmc-pack.json").read_text(encoding="utf-8"))
            components = pack.get("components", [])
        except (FileNotFoundError, json.JSONDecodeError):
            components = []
        return Instance(
            folder=folder,
            path=path,
            cfg=inifile.read(cfg_path),
            components=components,
            group=groups.get(folder),
            running=folder in running,
        )

    def list_instances(self) -> list[Instance]:
        root = self.instances_dir
        if not root.is_dir():
            return []
        groups = self.groups()
        running = self.running_instance_folders()
        out = []
        for d in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if d.is_dir() and not d.name.startswith((STAGING_DIR, "_LAUNCHER_TEMP")):
                inst = self._load(d.name, groups, running)
                if inst:
                    out.append(inst)
        return out

    def get_instance(self, ref: str) -> Instance:
        """Find an instance by folder name (its MultiMC ID) or display name, case-insensitively."""
        instances = self.list_instances()
        for inst in instances:
            if inst.folder == ref:
                return inst
        low = ref.lower()
        matches = [i for i in instances if i.folder.lower() == low or i.name.lower() == low]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            matches = [i for i in instances if low in i.name.lower()]
            if len(matches) == 1:
                return matches[0]
        names = ", ".join(f"'{i.name}'" for i in (matches or instances))
        raise LauncherError(
            f"{'Several' if matches else 'No'} instances match '{ref}'. "
            f"{'Candidates' if matches else 'Instances'}: {names}"
        )

    def require_not_running(self, inst: Instance) -> None:
        if inst.folder in self.running_instance_folders():
            raise LauncherError(f"'{inst.name}' is running. Close the game before changing its files.")

    def free_folder_name(self, name: str) -> str:
        """FS::DirNameFromString: replace bad chars with '-', append 1, 2, ... until unused."""
        base = "".join("-" if c in BAD_FILENAME_CHARS else c for c in name).strip() or "instance"
        if base in (".", "..") or base.startswith((STAGING_DIR, BACKUP_DIR, "_LAUNCHER_TEMP")):
            base = f"instance-{base.strip('.')}"
        folder, n = base, 0
        while (self.instances_dir / folder).exists():
            n += 1
            folder = f"{base}{n}"
        return folder

    def set_group(self, folder: str, group: str) -> None:
        """Add an instance to a group. Only safe while MultiMC is closed: it keeps the
        group list in memory and overwrites instgroups.json on its next change."""
        if self.is_running():
            raise LauncherError("MultiMC is open, so it would overwrite the group list. Set the group in MultiMC instead.")
        path = self.instances_dir / "instgroups.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        data.setdefault("formatVersion", "1")
        groups = data.setdefault("groups", {})
        for info in groups.values():
            if folder in info.get("instances", []):
                info["instances"].remove(folder)
        entry = groups.setdefault(group, {"hidden": False, "instances": []})
        entry.setdefault("instances", []).append(folder)
        inifile.atomic_write_text(path, json.dumps(data, indent=4) + "\n")

    def sanitize(self, name: str) -> str:
        return "".join("-" if c in BAD_FILENAME_CHARS else c for c in name).strip()

    def rename(self, inst: Instance, new_name: str) -> dict:
        """Rename an instance AND its folder (MultiMC's own rename only changes the display name,
        because the folder is the instance ID). Updates instgroups.json and SelectedInstance."""
        if self.is_running():
            raise LauncherError("Close MultiMC first: it keeps instance IDs in memory and would undo the rename.")
        self.require_not_running(inst)
        old = inst.folder
        new = old if self.sanitize(new_name) == old else self.free_folder_name(new_name)
        if new != old:
            os.rename(inst.path, self.instances_dir / new)
        cfg_path = self.instances_dir / new / "instance.cfg"
        cfg = inifile.read(cfg_path)
        cfg["name"] = new_name
        inifile.write(cfg_path, cfg)
        if new != old:
            groups_path = self.instances_dir / "instgroups.json"
            try:
                data = json.loads(groups_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                data = None
            if data:
                for info in (data.get("groups") or {}).values():
                    info["instances"] = [new if f == old else f for f in info.get("instances", [])]
                inifile.atomic_write_text(groups_path, json.dumps(data, indent=4) + "\n")
            main_cfg = self.data_dir / "multimc.cfg"
            settings = inifile.read(main_cfg)
            if settings.get("SelectedInstance") == old:
                settings["SelectedInstance"] = new
                inifile.write(main_cfg, settings)
        return {"name": new_name, "old_folder": old, "folder": new, "folder_renamed": new != old}

    # ---- launching ---------------------------------------------------------

    def launch(self, inst: Instance, server: str | None = None, world: str | None = None,
               account: str | None = None) -> str:
        if not self.binary.exists():
            raise LauncherError(f"MultiMC binary not found at {self.binary}")
        args = [str(self.binary), "-l", inst.folder]
        if server:
            args += ["-s", server]
        if world:
            args += ["-w", world]
        if account:
            args += ["-a", account]
        env = dict(os.environ)
        env.setdefault("DISPLAY", ":0")
        # If MultiMC is already open this hands the launch to it over its local socket and exits.
        subprocess.Popen(args, cwd=self.data_dir, env=env, start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return " ".join(args[1:])
