"""独立程序的设备身份、项目目录和每组映射的状态。"""
import json
import os
import pathlib
import platform
import re
import socket
import uuid

import sync_core as core

STATE = pathlib.Path(os.environ.get("CODEX_PROJECT_SYNC_HOME", str(pathlib.Path.home() / ".codex-project-sync")))
VERSION = "1.0.1"


def settings():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = STATE / "settings.json"
    value = core.read_json(path, {})
    if not value:
        value = {"device_id": "host-" + uuid.uuid4().hex, "name": socket.gethostname(), "version": VERSION}
        core.save_json(path, value)
    return value


def local_info():
    value = settings()
    home = pathlib.Path.home()
    return dict(value, version=VERSION, platform="windows" if os.name == "nt" else "macos", home=str(home),
                codex_home=str(home / ".codex"), app="Codex Project Sync", protocol=1,
                system=platform.platform())


def catalog():
    info = local_info()
    global_state = core.read_json(pathlib.Path(info["codex_home"]) / ".codex-global-state.json", {})
    result = []
    for pid, project in global_state.get("local-projects", {}).items():
        roots = project.get("rootPaths", [])
        for index, path in enumerate(roots):
            label = project.get("name") or pathlib.Path(path).name
            if len(roots) > 1:
                label += " · 目录 " + str(index + 1)
            result.append({"id": pid, "name": label, "path": path, "exists": pathlib.Path(path).is_dir(),
                           "git": (pathlib.Path(path) / ".git").exists()})
    parents = [str(pathlib.Path(row["path"]).parent) for row in result if row["exists"]]
    default_parent = max(set(parents), key=parents.count) if parents else str(pathlib.Path.home() / "Documents")
    return {"device": info, "projects": result, "default_parent": default_parent}


def validate_root(raw):
    path = pathlib.Path(raw).expanduser()
    if not path.is_absolute():
        raise RuntimeError("项目目录必须使用绝对路径")
    resolved = path.resolve()
    home = pathlib.Path.home().resolve()
    protected = [home, home / ".codex", STATE.resolve(), home / ".course-sync"]
    if resolved.parent == resolved or resolved in protected:
        raise RuntimeError("请选择具体项目目录，个人配置目录和磁盘根目录不可作为项目")
    for parent in [STATE.resolve(), home / ".course-sync"]:
        if parent == resolved or parent in resolved.parents or resolved in parent.parents:
            raise RuntimeError("项目目录不能覆盖同步程序或旧程序")
    if path.is_symlink():
        raise RuntimeError("项目根目录为符号链接，请直接选择真实目录")
    return str(resolved)


def activate(profile):
    import controller
    owner = core.read_json(STATE / "database.lock/owner.json", {})
    if owner and owner.get("pid") != os.getpid() and controller.owner_alive(owner["pid"]):
        raise RuntimeError("本机正在安装其他项目，请稍候重试")
    if not re.fullmatch(r"[a-f0-9]{32}", profile.get("id", "")):
        raise RuntimeError("同步映射标识不合法")
    info = local_info()
    side = "left" if profile["left_device"]["device_id"] == info["device_id"] else "right"
    if profile[side + "_device"]["device_id"] != info["device_id"]:
        raise RuntimeError("同步映射不属于本机")
    other = "right" if side == "left" else "left"
    projects, names, new_projects, canonical_roots = {}, {}, [], {}
    selected_paths = []
    for mapping in profile["mappings"]:
        key = mapping["key"]
        if not re.fullmatch(r"p_[a-f0-9]{12}", key):
            raise RuntimeError("项目映射标识不合法")
        endpoint = mapping[side]
        path = validate_root(endpoint["path"])
        current = pathlib.Path(path)
        if any(current == previous or current in previous.parents or previous in current.parents for previous in selected_paths):
            raise RuntimeError("同一组同步中的项目目录不能重叠")
        selected_paths.append(current)
        projects[key] = path
        names[key] = endpoint["name"]
        if endpoint.get("create"):
            new_projects.append(key)
        for part in [side, other]:
            canonical_roots[mapping[part]["path"]] = "@course/" + key
    context = STATE / "contexts" / profile["id"]
    context.mkdir(parents=True, exist_ok=True)
    unfinished = [path for path in (context / "backups").glob("*/transaction.json")
                  if core.read_json(path).get("status") not in ["complete", "rolled_back"]]
    previous = core.read_json(context / "profile.json", {})
    if unfinished and previous:
        def signature(value):
            return sorted((item["key"], sorted([(value["left_device"]["device_id"], item["left"]["path"]),
                                               (value["right_device"]["device_id"], item["right"]["path"])])) for item in value["mappings"])
        if signature(previous) != signature(profile):
            raise RuntimeError("此映射仍有待恢复安装，请先按原映射恢复")
    if not (context / ".git").exists():
        core.git(context, "init", "-b", "codex/sync-state")
        (context / ".gitignore").write_text("work/\nbackups/\ncache.json\nlock/\n", encoding="utf-8")
    core.APP = context
    for device in [profile[side + "_device"], profile[other + "_device"]]:
        canonical_roots[device["codex_home"]] = "@codex"
        canonical_roots[device["home"]] = "@home"
    canonical_roots = dict(sorted(canonical_roots.items(), key=lambda item: -len(item[0])))
    config_text = pathlib.Path(info["codex_home"]) / "config.toml"
    provider = "openai"
    if config_text.is_file():
        top = config_text.read_text(encoding="utf-8").split("\n[", 1)[0]
        match = re.search(r'^model_provider\s*=\s*"([^"\n]+)"', top, re.MULTILINE)
        if match:
            provider = match.group(1)
    cfg = {"device": info["device_id"], "home": info["home"], "codex_home": info["codex_home"],
           "course_root": str(pathlib.Path.home() / "Documents"), "projects": projects, "project_names": names,
           "new_projects": new_projects, "native_provider": provider, "canonical_roots": canonical_roots,
           "preserve_repository_kind": True,
           "state_dir": str(context), "exclude_threads": profile.get("exclude_threads", []),
           "app_launch": "shell:AppsFolder\\OpenAI.Codex_2p2nqsd0c76g0!App"}
    core.save_json(context / "config.json", cfg)
    core.save_json(context / "profile.json", profile)
    core.git(context, "add", "config.json", "profile.json", ".gitignore")
    if core.git(context, "status", "--porcelain", "config.json", "profile.json", ".gitignore"):
        core.git(context, "commit", "--only", "-m", "保存独立项目映射配置", "--", "config.json", "profile.json", ".gitignore")
    cfg["aliases"] = core.read_json(context / "aliases.json", {}).get("paths", {})
    cfg["image_fallbacks"] = core.read_json(context / "aliases.json", {}).get("images", {})
    cfg["lineage"] = core.read_json(context / "lineage.json", {})
    return cfg


def inventory(profile):
    cfg = activate(profile)
    return core.inventory(cfg, include_git=profile.get("include_git", False))


def add_project_actions(plan, cfg):
    existing = catalog()["projects"]
    actions = []
    for key in core.project_keys(cfg):
        target = core.normalized_path(core.project_path(cfg, key))
        if not any(core.normalized_path(row["path"]) == target for row in existing):
            if key not in cfg["new_projects"]:
                raise RuntimeError("目标项目不存在，请明确选择新建项目")
            actions.append({"course": key, "action": "create", "name": cfg["project_names"][key]})
    plan["project_actions"] = actions
    return plan
