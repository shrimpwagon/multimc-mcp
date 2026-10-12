"""The installed MultiMC: data dir, instances, groups, running state, launching.

Everything here follows the on-disk formats in MultiMC/Launcher
(InstanceList.cpp, MinecraftInstance.cpp, PackProfile.cpp, FileSystem.cpp).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from . import inifile

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

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


def _candidates() -> list[Path]:
    """Where MultiMC usually keeps its data (multimc.cfg, instances/) on each OS."""
    home = Path.home()
    if IS_WINDOWS:
        # MultiMC on Windows is portable: data lives next to MultiMC.exe, wherever it was extracted.
        env = os.environ
        roots = [home, home / "Desktop", home / "Downloads", home / "Documents", home / "Games",
                 Path(env.get("LOCALAPPDATA", home)), Path(env.get("APPDATA", home)),
                 Path(env.get("SystemDrive", "C:") + "/"), Path(env.get("ProgramFiles", "C:/Program Files"))]
        return [r / "MultiMC" for r in roots]
    if IS_MAC:
        # Application.cpp: data lives in MultiMC.app/Data (older installs: next to the binary).
        apps = [Path("/Applications/MultiMC.app"), home / "Applications/MultiMC.app"]
        return [a / "Data" for a in apps] + [a / "Contents/MacOS" for a in apps] + [home / "MultiMC"]
    xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
    return [xdg / "multimc", home / "MultiMC"]


def _data_dir_of(exe: Path) -> Path:
    """MultiMC's data folder, given the path of its running binary."""
    if IS_MAC and exe.parent.name == "MacOS":
        data = exe.parents[2] / "Data"  # MultiMC.app/Contents/MacOS/MultiMC -> MultiMC.app/Data
        return data if data.is_dir() else exe.parent
    if exe.parent.name == "bin" and (exe.parent.parent / "multimc.cfg").exists():
        return exe.parent.parent  # Linux tarball: <data>/bin/MultiMC
    return exe.parent


def _multimc_processes():
    for proc in psutil.process_iter(["name", "exe"]):
        name = (proc.info.get("name") or "").lower()
        if name in ("multimc", "multimc.exe") and proc.info.get("exe"):
            yield proc, Path(proc.info["exe"])


def find_data_dir() -> Path:
    """MULTIMC_DIR, else the usual place for this OS, else ask a running MultiMC where it lives."""
    env = os.environ.get("MULTIMC_DIR")
    if env:
        return Path(env).expanduser()
    for candidate in _candidates():
        if (candidate / "multimc.cfg").exists():
            return candidate
    for _, exe in _multimc_processes():
        data = _data_dir_of(exe)
        if (data / "multimc.cfg").exists():
            return data
    if IS_WINDOWS:
        where = "the folder you extracted MultiMC to (it holds MultiMC.exe and multimc.cfg)"
    elif IS_MAC:
        where = "MultiMC.app/Data"
    else:
        where = "the folder that holds multimc.cfg"
    raise LauncherError(f"Can't find MultiMC's data folder. Open MultiMC once, or set MULTIMC_DIR to {where}.")


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
        if IS_WINDOWS:
            return self.data_dir / "MultiMC.exe"
        if IS_MAC and self.data_dir.name == "Data" and self.data_dir.parent.suffix == ".app":
            return self.data_dir.parent / "Contents/MacOS/MultiMC"
        return self.data_dir / "MultiMC"  # Linux (a launcher script) and old Mac layouts

    # ---- processes ---------------------------------------------------------

    def is_running(self) -> bool:
        """True if the MultiMC GUI that uses this data dir is running."""
        mine = self.data_dir.resolve()
        return any(_data_dir_of(exe).resolve() == mine for _, exe in _multimc_processes())

    def running_instance_folders(self) -> set[str]:
        """Instance folders that have a Java process working inside them (a running game)."""
        root = self.instances_dir.resolve()
        found: set[str] = set()
        for proc in psutil.process_iter(["name"]):
            if "java" not in (proc.info.get("name") or "").lower():  # java, javaw.exe
                continue
            try:
                cwd = Path(proc.cwd()).resolve()
            except (psutil.Error, OSError):
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
        if new != old:
            # Settings that point inside the instance (e.g. a Java runtime kept there) move with it.
            old_root, new_root = str(self.instances_dir / old), str(self.instances_dir / new)
            for key, value in cfg.items():
                if old_root in value:
                    cfg[key] = value.replace(old_root, new_root)
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
        result = {"name": new_name, "old_folder": old, "folder": new, "folder_renamed": new != old}
        stale = self.shortcuts_launching(old) if new != old else []
        if stale:
            result["shortcuts_to_update"] = stale
            result["note"] = f"These launch the old ID with -l; change '{old}' to '{new}' in them."
        return result

    def shortcuts_launching(self, folder: str) -> list[str]:
        """Desktop entries / scripts outside MultiMC that launch this instance ID (`-l <id>`)."""
        pattern = re.compile(r"""(?:-l|--launch)[ =]+(["']?)""" + re.escape(folder) + r"""\1(?:\s|$)""")
        home = Path.home()
        dirs = [home / "Desktop", home / ".local/share/applications", home / ".config/autostart",
                home / "scripts", home / "bin", home / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs"]
        hits = []
        for d in dirs:
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if f.is_file() and f.stat().st_size < 1_000_000:
                    try:
                        if pattern.search(f.read_text(errors="ignore")):
                            hits.append(str(f))
                    except OSError:
                        continue
        return hits

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
        detach: dict = {}
        if IS_WINDOWS:
            detach["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            detach["start_new_session"] = True
            if not IS_MAC:
                env.setdefault("DISPLAY", ":0")
        # If MultiMC is already open this hands the launch to it over its local socket and exits.
        subprocess.Popen(args, cwd=self.data_dir, env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detach)
        return " ".join(args[1:])
