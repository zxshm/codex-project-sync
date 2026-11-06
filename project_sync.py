"""独立的项目映射、单连接双向传输与精确安装流程。"""
import contextlib
import json
import os
import pathlib
import time
import uuid
import unicodedata
import re

import controller
import runtime
import sync_core as core
import transport


def make_plan(profile, source):
    cfg = runtime.activate(profile)
    local = core.inventory(cfg, include_git=profile.get("include_git", False))
    seen = set()
    for relative in source["files"]:
        normalized = unicodedata.normalize("NFC", relative).casefold()
        if normalized in seen:
            raise RuntimeError("源项目有仅大小写或 Unicode 形式不同的文件名，请先核对命名：" + relative)
        seen.add(normalized)
        if os.name == "nt":
            for component in relative.split("/")[1:]:
                if (any(character in '<>:"\\|?*' for character in component) or component.endswith((" ", "."))
                        or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", component)
                        or len(component.encode("utf-16-le")) // 2 > 255):
                    raise RuntimeError("目标 Windows 不支持该文件名：" + relative)
    baseline = core.read_json(core.APP / "baseline.json", {})
    bindings = core.read_json(core.APP / "bindings.json", {})
    plan = core.plan_sync(local, source, baseline, bindings)
    with contextlib.closing(core.ro(pathlib.Path(cfg["codex_home"]) / "state_5.sqlite")) as database:
        for action in plan["chats"]:
            if action["destination"] not in local["chats"] and database.execute("select 1 from threads where id=?", (action["destination"],)).fetchone():
                action["destination"] = str(uuid.uuid5(uuid.NAMESPACE_URL, profile["id"] + "/" + action["source"]))
                action["action"] = "fork"
    plan["include_git"] = bool(profile.get("include_git"))
    plan["git_changed"] = plan["include_git"] and any(set(tips) - set(local.get("git_reachable", {}).get(repo, []))
                                                        for repo, tips in source.get("git_ref_tips", {}).items())
    runtime.add_project_actions(plan, cfg)
    return {"local": local, "plan": plan}


def export(profile, request):
    cfg = runtime.activate(profile)
    records_path = core.APP / "exports.json"
    records = core.read_json(records_path, {})
    key = core.digest(json.dumps(request, sort_keys=True, ensure_ascii=False).encode())
    previous = records.get(key)
    if previous:
        path = pathlib.Path(previous["path"])
        if path.is_file() and core.file_hash(path) == previous["sha"]:
            return previous
    descriptor = core.export_package(cfg, request)
    descriptor["cache_key"] = key
    records[key] = descriptor
    core.save_json(records_path, records)
    return descriptor


def receive_path(profile, descriptor):
    runtime.activate(profile)
    if len(descriptor["sha"]) != 64 or any(character not in "0123456789abcdef" for character in descriptor["sha"]):
        raise RuntimeError("更新包校验标识不合法")
    directory = core.APP / "work" / descriptor["sha"]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "received.tar"
    size = path.stat().st_size if path.exists() else 0
    return {"path": str(path), "size": size, "prefix_sha": core.file_hash(path) if size else core.digest(b"")}


def prefix(profile, descriptor, length):
    runtime.activate(profile)
    path = core.validate_export_path(descriptor)
    if not 0 <= length <= path.stat().st_size:
        raise RuntimeError("断点长度不合法")
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        remaining = length
        while remaining:
            block = stream.read(min(8388608, remaining))
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def release(profile, descriptor, received):
    cfg = runtime.activate(profile)
    path = core.validate_export_path(descriptor)
    if path.parent.parent != (core.APP / "work").resolve():
        raise RuntimeError("发送缓存目录不合法")
    confirmed = core.acknowledge_received_files(cfg, path, received)
    core.remove_sync_tree(path.parent)
    records_path = core.APP / "exports.json"
    records = core.read_json(records_path, {})
    records.pop(descriptor.get("cache_key"), None)
    core.save_json(records_path, records)
    return {"confirmed": confirmed}


def pending():
    return [str(path.parent) for path in (core.APP / "backups").glob("*/transaction.json")
            if core.read_json(path).get("status") not in ["complete", "rolled_back"]]


@contextlib.contextmanager
def database_lock(allow_pending=False):
    lock = runtime.STATE / "database.lock"
    try:
        lock.mkdir()
    except FileExistsError:
        owner = core.read_json(lock / "owner.json", {})
        if owner and not controller.owner_alive(owner.get("pid")):
            (lock / "owner.json").unlink()
            lock.rmdir()
            lock.mkdir()
        else:
            raise RuntimeError("本机已有其他项目安装正在执行")
    core.save_json(lock / "owner.json", {"pid": os.getpid(), "time": time.time()})
    try:
        unfinished = [path for path in (runtime.STATE / "contexts").glob("*/backups/*/transaction.json")
                      if core.read_json(path).get("status") not in ["complete", "rolled_back"]]
        if unfinished and not allow_pending:
            raise RuntimeError("存在未完成的安装，请先恢复：" + str(unfinished[0].parent))
        yield
    finally:
        (lock / "owner.json").unlink(missing_ok=True)
        lock.rmdir()


def apply(profile, source, plan, expected, descriptor):
    with database_lock():
        return apply_locked(profile, source, plan, expected, descriptor)


def recover_profile(profile):
    import recover
    with database_lock(allow_pending=True):
        cfg = runtime.activate(profile)
        backups = pending()
        if not backups:
            return {"done": True, "message": "此映射没有未完成的安装"}
        was_running = False
        try:
            if any(core.needs_database(core.read_json(pathlib.Path(path) / "transaction.json")["plan"]) for path in backups):
                core.check_idle(cfg)
                was_running = core.close_local_app(cfg)
            for path in backups:
                recover.recover(cfg, path)
            return {"done": True, "recovered": len(backups), "message": "已恢复目标项目，恢复数据暂留供核对"}
        finally:
            if was_running and not pending():
                core.reopen(cfg)


def apply_locked(profile, source, plan, expected, descriptor):
    cfg = runtime.activate(profile)
    if pending():
        raise RuntimeError("存在未完成的安装，请先恢复：" + pending()[0])
    packet = receive_path(profile, descriptor)
    package = pathlib.Path(packet["path"])
    if package.stat().st_size != descriptor["size"] or core.file_hash(package) != descriptor["sha"]:
        raise RuntimeError("更新包大小或 SHA-256 校验失败")
    lock = core.APP / "lock"
    try:
        lock.mkdir()
    except FileExistsError:
        owner = core.read_json(lock / "owner.json", {})
        if owner and not controller.owner_alive(owner.get("pid")):
            (lock / "owner.json").unlink()
            lock.rmdir()
            lock.mkdir()
        else:
            raise RuntimeError("该项目映射已有同步操作正在执行")
    core.save_json(lock / "owner.json", {"pid": os.getpid(), "time": time.time()})
    was_running = False
    started = time.monotonic()
    try:
        if core.needs_database(plan):
            try:
                core.check_idle(cfg)
            except RuntimeError as error:
                if "本机有聊天尚未结束" in str(error):
                    return {"waiting": True, "message": "目标电脑仍有聊天正在生成，更新包已校验，等待聊天结束"}
                raise
            was_running = core.close_local_app(cfg)
        result = core.apply_import(cfg, plan, package, expected)
        backup = pathlib.Path(result["backup"])
        metadata = core.read_json(backup / "incoming/metadata.json")
        aliases = core.read_json(core.APP / "aliases.json", {"paths": {}, "images": {}})
        for raw, name in metadata.get("path_assets", {}).items():
            target = str(pathlib.Path(cfg["codex_home"]) / "migrated-assets/project-sync" / name)
            aliases["paths"][target] = metadata.get("canonical_assets", {}).get(raw, core.canonical(raw, cfg))
        core.save_json(core.APP / "aliases.json", aliases)
        cfg = runtime.activate(profile)
        refreshed = core.inventory(cfg, include_git=False)
        for action in plan["files"]:
            if core.file_hash(core.project_file(cfg, action["destination"])) != action["sha"]:
                raise RuntimeError("安装后的文件校验失败，已保留回退数据")
        for action in plan["chats"]:
            actual = refreshed["chats"].get(action["destination"])
            if actual is None or core.compare_history(actual, source["chats"][action["source"]]) != "same":
                raise RuntimeError("聊天历史安装校验失败，已保留回退数据")
        for action in plan.get("project_actions", []):
            root = core.normalized_path(core.project_path(cfg, action["course"]))
            matches = [row for row in runtime.catalog()["projects"] if core.normalized_path(row["path"]) == root]
            if len(matches) != 1:
                raise RuntimeError("项目注册结果无法唯一确认，已保留回退数据")
        controller.save_baseline(refreshed, source)
        bindings = core.read_json(core.APP / "bindings.json", {})
        lineage = core.read_json(core.APP / "lineage.json", {})
        for action in plan["chats"]:
            bindings[action["binding"]] = action["destination"]
            lineage[action["destination"]] = source["chats"][action["source"]].get("lineage", action["source"])
        core.save_json(core.APP / "bindings.json", bindings)
        core.save_json(core.APP / "lineage.json", lineage)
        core.git(core.APP, "add", "-f", "config.json", "profile.json", "aliases.json", "baseline.json", "bindings.json", "lineage.json", ".gitignore")
        if core.git(core.APP, "status", "--porcelain"):
            core.git(core.APP, "commit", "-m", "记录项目同步映射与成功安装后的基线")
        core.discard_completed_backup(cfg, backup)
        core.remove_sync_tree(package.parent)
        result.pop("backup", None)
        result.update(done=True, temporary_cleaned=True, elapsed=round(time.monotonic() - started, 3),
                      received={action["source"]: action["sha"] for action in plan["files"] if action["source"] == action["destination"]})
        return result
    finally:
        (lock / "owner.json").unlink(missing_ok=True)
        lock.rmdir()
        if was_running and not pending():
            core.reopen(cfg)


def dispatch(request):
    if request.get("protocol") != 1:
        raise RuntimeError("两端独立程序版本不兼容")
    operation = request["op"]
    if operation == "hello":
        return runtime.local_info()
    if operation == "catalog":
        return runtime.catalog()
    profile = request["profile"]
    if operation == "inventory":
        return runtime.inventory(profile)
    if operation == "plan":
        return make_plan(profile, request["source"])
    if operation == "export":
        return export(profile, request["request"])
    if operation == "receive_path":
        return receive_path(profile, request["descriptor"])
    if operation == "prefix":
        return prefix(profile, request["descriptor"], request["length"])
    if operation == "release":
        return release(profile, request["descriptor"], request.get("received", {}))
    if operation == "apply":
        return apply(profile, request["source"], request["plan"], request["expected"], request["descriptor"])
    if operation == "recover":
        return recover_profile(profile)
    raise RuntimeError("未知操作")


def synchronize(connection, profile, direction, preview=False, progress=lambda message: None):
    profile = dict(profile)
    source_remote = direction == "pull"
    progress("正在读取已选择项目的文件和主聊天")
    source = transport.rpc(connection, "inventory", profile=profile) if source_remote else runtime.inventory(profile)
    target = make_plan(profile, source) if source_remote else transport.rpc(connection, "plan", profile=profile, source=source)
    plan, expected = target["plan"], target["local"]
    summary = controller.summarize(plan)
    summary["新增项目"] = len(plan.get("project_actions", []))
    if preview:
        return {"preview": True, "summary": summary, "plan": plan}
    if not plan["files"] and not core.needs_database(plan) and not plan["git_changed"]:
        return {"done": True, "unchanged": True, "summary": summary, "message": "两端已选内容已对齐"}
    request = dict(plan, source_chats=source["chats"], git_have=expected.get("repos", {}),
                   git_reachable=expected.get("git_reachable", {}), git_file_blobs=expected.get("git_file_blobs", {}), include_snapshots=False)
    progress("正在制作本次增量更新包")
    descriptor = transport.rpc(connection, "export", profile=profile, request=request) if source_remote else export(profile, request)
    target_packet = receive_path(profile, descriptor) if source_remote else transport.rpc(connection, "receive_path", profile=profile, descriptor=descriptor)
    if source_remote:
        local = pathlib.Path(target_packet["path"])
        offset = target_packet["size"]
        if offset and target_packet["prefix_sha"] != transport.rpc(connection, "prefix", profile=profile, descriptor=descriptor, length=offset):
            local.unlink()
            offset = 0
        transport.transfer(connection, "get", local, descriptor["path"], progress, resume=bool(offset))
    else:
        local = pathlib.Path(descriptor["path"])
        offset = target_packet["size"]
        if offset:
            with local.open("rb") as stream:
                digest = __import__("hashlib").sha256()
                remaining = offset
                while remaining:
                    block = stream.read(min(remaining, 8388608))
                    if not block:
                        raise RuntimeError("远端断点超过本地更新包长度")
                    digest.update(block)
                    remaining -= len(block)
            if digest.hexdigest() != target_packet["prefix_sha"]:
                raise RuntimeError("远端断点与本次更新包不一致，停止覆盖")
        transport.transfer(connection, "put", local, target_packet["path"], progress, resume=bool(offset))
    progress("更新包传输完成，正在精确备份并安装")
    while True:
        result = apply(profile, source, plan, expected, descriptor) if source_remote else transport.rpc(
            connection, "apply", profile=profile, source=source, plan=plan, expected=expected, descriptor=descriptor)
        if not result.get("waiting"):
            break
        progress(result["message"])
        time.sleep(3)
    try:
        if source_remote:
            transport.rpc(connection, "release", profile=profile, descriptor=descriptor, received=result["received"])
        else:
            release(profile, descriptor, result["received"])
    except Exception as error:
        result["cleanup_warning"] = "数据已安装，发送端缓存清理需要重试：" + str(error)
    result["summary"] = summary
    return result
