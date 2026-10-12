"""Offline tests: file formats, naming, renames, and the resolver against fake sources."""

from __future__ import annotations

import asyncio
import json
import sys
import zipfile
from pathlib import Path

import pytest

from multimc_mcp import inifile
from multimc_mcp.curseforge import fingerprint, index_versions, to_version
from multimc_mcp.install import LauncherError, mrpack_files, read_cf_manifest, cf_components, safe_name, safe_relpath
from multimc_mcp.launcher import Launcher
from multimc_mcp.resolver import Resolver, best_version, names_match

# ---- inifile -------------------------------------------------------------------


def test_ini_roundtrip_escapes():
    data = {"name": "A #1\\B", "notes": "line1\nline2\ttab", "x": "plain"}
    assert inifile.parse(inifile.dumps(data)) == data


def test_ini_comments_and_spacing():
    text = "# comment\nname = Foo \\# bar # trailing\nbad line\n=empty key\n"
    assert inifile.parse(text) == {"name": "Foo # bar", "": "empty key"}


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX file modes")
def test_atomic_write_keeps_mode(tmp_path: Path):
    p = tmp_path / "f.cfg"
    p.write_text("a=1\n")
    p.chmod(0o640)
    inifile.write(p, {"a": "2"})
    assert p.stat().st_mode & 0o777 == 0o640
    assert inifile.read(p) == {"a": "2"}


# ---- launcher ------------------------------------------------------------------


def make_launcher(tmp_path: Path) -> Launcher:
    (tmp_path / "instances").mkdir()
    (tmp_path / "multimc.cfg").write_text("InstanceDir=instances\nSelectedInstance=1.21.4\n")
    return Launcher(data_dir=tmp_path)


def make_instance(launcher: Launcher, folder: str, name: str, mc: str = "1.21.4") -> None:
    d = launcher.instances_dir / folder
    (d / ".minecraft" / "mods").mkdir(parents=True)
    inifile.write(d / "instance.cfg", {"InstanceType": "OneSix", "name": name})
    (d / "mmc-pack.json").write_text(json.dumps({"formatVersion": 1, "components": [
        {"uid": "net.minecraft", "version": mc}, {"uid": "net.neoforged", "version": "21.4.1"}]}))


def test_folder_names_follow_multimc(tmp_path: Path):
    launcher = make_launcher(tmp_path)
    assert launcher.free_folder_name('Cobblemon: test/1 "x"!') == "Cobblemon- test-1 -x--"
    make_instance(launcher, "Foo", "Foo")
    assert launcher.free_folder_name("Foo") == "Foo1"
    assert launcher.free_folder_name("..") != ".."
    assert not launcher.free_folder_name(".mcp-staging").startswith(".mcp-staging")


def test_list_and_lookup(tmp_path: Path):
    launcher = make_launcher(tmp_path)
    make_instance(launcher, "1.21.4", "1.21.4 - Yungs")
    (launcher.instances_dir / ".mcp-staging" / "abc").mkdir(parents=True)
    insts = launcher.list_instances()
    assert [i.folder for i in insts] == ["1.21.4"]
    assert insts[0].loader == ("neoforge", "21.4.1")
    assert launcher.get_instance("1.21.4 - yungs").folder == "1.21.4"
    assert launcher.get_instance("yungs").folder == "1.21.4"
    with pytest.raises(LauncherError):
        launcher.get_instance("nope")


def test_rename_moves_folder_and_references(tmp_path: Path):
    launcher = make_launcher(tmp_path)
    make_instance(launcher, "1.21.4", "1.21.4 - Yungs")
    launcher.set_group("1.21.4", "Misc")
    result = launcher.rename(launcher.get_instance("1.21.4"), "1.21.4 - Yungs")
    assert result["folder"] == "1.21.4 - Yungs" and result["folder_renamed"]
    assert (launcher.instances_dir / "1.21.4 - Yungs" / "instance.cfg").exists()
    assert launcher.groups() == {"1.21.4 - Yungs": "Misc"}
    assert inifile.read(tmp_path / "multimc.cfg")["SelectedInstance"] == "1.21.4 - Yungs"
    # Renaming to the same name is a no-op for the folder.
    again = launcher.rename(launcher.get_instance("1.21.4 - Yungs"), "1.21.4 - Yungs")
    assert not again["folder_renamed"]


def test_set_group_moves_between_groups(tmp_path: Path):
    launcher = make_launcher(tmp_path)
    make_instance(launcher, "A", "A")
    launcher.set_group("A", "One")
    launcher.set_group("A", "Two")
    data = json.loads((launcher.instances_dir / "instgroups.json").read_text())
    assert data["groups"]["One"]["instances"] == [] and data["groups"]["Two"]["instances"] == ["A"]


# ---- install helpers -----------------------------------------------------------


def test_safe_paths():
    assert str(safe_relpath("mods/a.jar")) == "mods/a.jar"
    for bad in ("../x", "/etc/passwd", "mods/../../x", "C:/x", ""):
        with pytest.raises(LauncherError):
            safe_relpath(bad)
    assert safe_name("a.jar") == "a.jar"
    for bad in ("../a.jar", ".hidden", "a/b.jar"):
        with pytest.raises(LauncherError):
            safe_name(bad)


def test_mrpack_files_skip_server_only_and_optional():
    index = {"files": [
        {"path": "mods/a.jar", "downloads": ["https://cdn.modrinth.com/a.jar"], "hashes": {"sha1": "1"}},
        {"path": "mods/s.jar", "downloads": ["https://x/s.jar"], "hashes": {"sha1": "2"}, "env": {"client": "unsupported"}},
        {"path": "mods/o.jar", "downloads": ["https://x/o.jar"], "hashes": {"sha1": "3"}, "env": {"client": "optional"}},
        {"path": "mods/h.jar", "downloads": ["http://insecure/h.jar"], "hashes": {"sha1": "4"}},
    ]}
    files, skipped = mrpack_files(index)
    assert [f["relpath"] for f in files] == ["mods/a.jar"]
    assert len(skipped) == 2


def test_cf_manifest(tmp_path: Path):
    zp = tmp_path / "pack.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("manifest.json", json.dumps({
            "manifestType": "minecraftModpack", "name": "P",
            "minecraft": {"version": "1.20.1", "modLoaders": [{"id": "forge-47.2.0", "primary": True}]},
            "files": [], "overrides": "overrides"}))
    assert cf_components(read_cf_manifest(zp)) == [
        {"uid": "net.minecraft", "version": "1.20.1", "important": True},
        {"uid": "net.minecraftforge", "version": "47.2.0"}]


# ---- CurseForge conversions -----------------------------------------------------


def test_fingerprint_ignores_whitespace():
    assert fingerprint(b"a b\tc\r\n") == fingerprint(b"abc")
    assert fingerprint(b"abc") != fingerprint(b"abd")
    assert fingerprint(b"") == fingerprint(b"  \n")


def test_cf_index_versions_merge_and_order():
    project = {"id": "cf:1", "_indexes": [
        {"gameVersion": "1.21", "fileId": 20, "filename": "a.jar", "releaseType": 1, "modLoader": 6},
        {"gameVersion": "1.21.1", "fileId": 20, "filename": "a.jar", "releaseType": 1, "modLoader": 6},
        {"gameVersion": "1.12.2", "fileId": 5, "filename": "old.jar", "releaseType": 1},
        {"gameVersion": "1.21.1-Snapshot", "fileId": 30, "filename": "s.jar", "releaseType": 3, "modLoader": 4},
    ]}
    vs = index_versions(project)
    assert [v["_file_id"] for v in vs] == [20, 5]
    assert vs[0]["game_versions"] == ["1.21", "1.21.1"] and vs[0]["loaders"] == ["neoforge"]
    assert vs[1]["loaders"] == ["forge"]  # untagged pre-1.14 files are Forge


def test_cf_to_version():
    v = to_version({"id": 9, "modId": 3, "displayName": "X 1.0", "fileName": "x.jar", "releaseType": 2,
                    "fileDate": "2026-01-01", "downloadUrl": None, "gameVersions": ["1.21.1", "NeoForge", "Client"],
                    "hashes": [{"value": "md5", "algo": 2}, {"value": "abc", "algo": 1}], "fileLength": 10,
                    "dependencies": [{"modId": 4, "relationType": 3}, {"modId": 5, "relationType": 5}]})
    assert v["id"] == "cf:file:9" and v["project_id"] == "cf:3" and v["version_type"] == "beta"
    assert v["loaders"] == ["neoforge"] and v["game_versions"] == ["1.21.1"]
    assert v["files"][0]["url"] == "" and v["files"][0]["hashes"]["sha1"] == "abc"
    assert [d["dependency_type"] for d in v["dependencies"]] == ["required", "incompatible"]


# ---- matching ---------------------------------------------------------------------


@pytest.mark.parametrize("query,title,slug,ok", [
    ("xaeros minimap", "Xaero's Minimap", "xaeros-minimap", True),
    ("just enough items", "Just Enough Items (JEI)", "jei", True),
    ("JEI", "Just Enough Items (JEI)", "jei", True),
    ("jade", "Jade 🔍", "jade", True),
    ("dragon survival", "Dragon Survival Compatibility", "dragon-survival-compatibility", False),
    ("create", "Create: Steam 'n' Rails", "create-steam-n-rails", False),
])
def test_names_match(query, title, slug, ok):
    assert names_match(query, {"title": title, "slug": slug}) is ok


# ---- resolver with fake sources ----------------------------------------------------


def ver(vid, pid, mcs, loaders, vtype="release", date="2026-01-01", deps=()):
    return {"id": vid, "project_id": pid, "version_number": vid, "version_type": vtype, "game_versions": list(mcs),
            "loaders": list(loaders), "date_published": date,
            "files": [{"url": f"https://x/{vid}.jar", "filename": f"{vid}.jar", "primary": True, "size": 1,
                       "hashes": {"sha1": vid.ljust(40, "0")}}],
            "dependencies": [{"project_id": d, "version_id": None, "dependency_type": t} for d, t in deps]}


class FakeSources:
    cf = None
    cf_search_blocked = False
    names = "Fake"
    cf_hint = ""

    def __init__(self, projects, versions):
        self.p, self.v = projects, versions

    async def project(self, ref):
        return self.p.get(ref)

    async def search(self, query, **kw):
        return []

    async def projects(self, ids):
        return {i: self.p[i] for i in ids if i in self.p}

    async def versions_for(self, ids):
        return {i: sorted(self.v.get(i, []), key=lambda v: v["date_published"], reverse=True) for i in ids}

    async def version(self, vid):
        return next((v for vs in self.v.values() for v in vs if v["id"] == vid), None)

    async def complete(self, v):
        return v


class FakeMeta:
    mcs = {"1.21.1": "2024-08-08", "1.20.1": "2023-06-12", "1.20.2": "2023-09-21", "26.1": "2026-03-01"}

    async def minecraft_versions(self):
        return {k: {"type": "release", "releaseTime": t} for k, t in sorted(self.mcs.items(), key=lambda x: x[1], reverse=True)}

    async def newest_release(self):
        return "26.1"

    async def loader_version(self, loader, mc):
        return f"{loader}-for-{mc}"

    async def java_major(self, mc):
        return 25 if mc.startswith("26") else 21


def proj(pid, title, source="modrinth"):
    return {"id": pid, "slug": title.lower(), "title": title, "project_type": "mod", "source": source}


def run(coro):
    return asyncio.run(coro)


def test_plan_prefers_stable_release_and_walks_deps(monkeypatch):
    monkeypatch.setattr("multimc_mcp.resolver.installed_javas", lambda: {17: "j17", 21: "j21"})
    projects = {"a": proj("a", "A"), "lib": proj("lib", "Lib"), "b": proj("b", "B")}
    versions = {
        "a": [ver("a26", "a", ["26.1"], ["neoforge"], "beta", "2026-04-01"),
              ver("a121", "a", ["1.21.1"], ["neoforge", "fabric"], deps=[("lib", "required"), ("b", "optional")])],
        "lib": [ver("lib1", "lib", ["1.21.1"], ["neoforge"]), ver("lib0", "lib", ["1.20.1"], ["forge"], date="2023-01-01")],
        "b": [ver("b1", "b", ["1.21.1"], ["neoforge"])],
    }
    plan = run(Resolver(FakeSources(projects, versions), FakeMeta()).plan(["a"]))
    # 26.1 is newer but only has a beta and needs Java 25, so 1.21.1 + NeoForge wins.
    assert (plan.minecraft, plan.loader) == ("1.21.1", "neoforge")
    assert {p.version_id for p in plan.files} == {"a121", "lib1"}
    assert [o["slug"] for o in plan.optional_suggestions] == ["b"]
    assert not plan.unresolved


def test_plan_reports_missing_dependency():
    projects = {"a": proj("a", "A"), "lib": proj("lib", "Lib")}
    versions = {"a": [ver("a1", "a", ["1.21.1"], ["fabric"], deps=[("lib", "required")])],
                "lib": [ver("l1", "lib", ["1.20.1"], ["fabric"])]}
    plan = run(Resolver(FakeSources(projects, versions), FakeMeta()).plan(["a"]))
    assert plan.unresolved and "Lib" in plan.unresolved[0]


def test_walk_skips_same_library_from_other_source():
    projects = {"a": proj("a", "A"), "geo": proj("geo", "GeckoLib"),
                "cf:9": proj("cf:9", "Dragon", "curseforge"), "cf:10": proj("cf:10", "GeckoLib", "curseforge")}
    versions = {
        "a": [ver("a1", "a", ["1.21.1"], ["neoforge"], deps=[("geo", "required")])],
        "geo": [ver("g1", "geo", ["1.21.1"], ["neoforge"])],
        "cf:9": [ver("d1", "cf:9", ["1.21.1"], ["neoforge"], deps=[("cf:10", "required")])],
        "cf:10": [ver("g2", "cf:10", ["1.21.1"], ["neoforge"])],
    }
    w = run(Resolver(FakeSources(projects, versions), FakeMeta()).walk(
        "1.21.1", "neoforge", [("a", "requested", None), ("cf:9", "requested", None)]))
    assert sorted(w.picks) == ["a", "cf:9", "geo"]


def test_walk_flags_incompatible():
    projects = {"a": proj("a", "A"), "b": proj("b", "B")}
    versions = {"a": [ver("a1", "a", ["1.21.1"], ["fabric"], deps=[("b", "incompatible")])],
                "b": [ver("b1", "b", ["1.21.1"], ["fabric"])]}
    w = run(Resolver(FakeSources(projects, versions), FakeMeta()).walk(
        "1.21.1", "fabric", [("a", "requested", None), ("b", "requested", None)]))
    assert any("incompatible" in u for u in w.unresolved)


def test_quilt_accepts_fabric_mods_and_alpha_needs_opt_in():
    vs = [ver("x1", "x", ["1.21.1"], ["fabric"], "alpha")]
    assert best_version(vs, "1.21.1", "quilt", "mod") is None
    assert best_version(vs, "1.21.1", "quilt", "mod", allow_unstable=True)["id"] == "x1"
    assert best_version(vs, "1.21.1", "neoforge", "mod", allow_unstable=True) is None


def test_rename_rewrites_inner_paths_and_reports_shortcuts(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    (home / ".local/share/applications").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    (tmp_path / "mmc").mkdir()
    launcher = make_launcher(tmp_path / "mmc")
    make_instance(launcher, "1.21.4", "Yungs")
    cfg_path = launcher.instances_dir / "1.21.4" / "instance.cfg"
    cfg = inifile.read(cfg_path)
    cfg.update(OverrideJavaLocation="true", JavaPath=str(launcher.instances_dir / "1.21.4" / "jre/bin/java"))
    inifile.write(cfg_path, cfg)
    (home / ".local/share/applications/yungs.desktop").write_text('Exec=/opt/multimc/run.sh -l "1.21.4"\n')
    (home / ".local/share/applications/other.desktop").write_text('Exec=/opt/multimc/run.sh -l "1.21.40"\n')
    result = launcher.rename(launcher.get_instance("1.21.4"), "1.21.4 - Yungs")
    new_cfg = inifile.read(launcher.instances_dir / "1.21.4 - Yungs" / "instance.cfg")
    assert new_cfg["JavaPath"] == str(launcher.instances_dir / "1.21.4 - Yungs" / "jre/bin/java")
    assert result["shortcuts_to_update"] == [str(home / ".local/share/applications/yungs.desktop")]


def test_curseforge_files_are_not_kept_in_cache(tmp_path: Path):
    from multimc_mcp.install import Downloader, place_files

    class FakeDL(Downloader):
        async def fetch(self, url, sha1, sha512=None):
            dest = self.root / sha1[:2] / sha1
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"jar")
            return dest

    dl = FakeDL(http=None, root=tmp_path / "cache")
    import hashlib
    sha = hashlib.sha1(b"jar").hexdigest()
    game = tmp_path / "game"
    run(place_files(dl, game, [{"url": "https://edge.forgecdn.net/files/1/2/a.jar", "sha1": sha, "relpath": "mods/a.jar"}]))
    assert (game / "mods/a.jar").read_bytes() == b"jar"
    assert not (dl.root / sha[:2] / sha).exists()
    run(place_files(dl, game, [{"url": "https://cdn.modrinth.com/data/x/b.jar", "sha1": sha, "relpath": "mods/b.jar"}]))
    assert (dl.root / sha[:2] / sha).exists()  # Modrinth files stay cached
