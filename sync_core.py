"""五门课程的增量同步核心：内容比较、分叉保留、离线导入与 Git 备份。"""
from __future__ import annotations

import base64
import collections
import contextlib
import copy
import datetime
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import time
import urllib.parse
import uuid

APP = pathlib.Path(__file__).resolve().parent
SYNC_VERSION = 3
COURSES = ["高频电子", "通信原理", "数字信号处理", "微波原理", "单片机"]
STATE_TABLES = ["thread_dynamic_tools", "thread_attachments", "thread_read_receipts"]
HISTORY_TABLES = ["thread_turns", "thread_items", "thread_history_projection_state", "thread_realtime_items"]


def read_json(path, default=None):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8")) if pathlib.Path(path).exists() else default


def save_json(path, data):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".pending-" + uuid.uuid4().hex)
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def config():
    return read_json(APP / "config.json")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with pathlib.Path(path).open("rb") as f:
        for block in iter(lambda: f.read(8388608), b""):
            h.update(block)
    return h.hexdigest()


def file_hashes(path):
    """一次读取同时计算传输校验和 Git 原始文件对象，复用已有课件内容。"""
    path = pathlib.Path(path)
    sha = hashlib.sha256()
    blob = hashlib.sha1(("blob " + str(path.stat().st_size) + "\0").encode())
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8388608), b""):
            sha.update(block)
            blob.update(block)
    return sha.hexdigest(), blob.hexdigest()


def run(args, **kwargs):
    options = dict(capture_output=True, text=True, encoding="utf-8", errors="replace")
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    options.update(kwargs)
    result = subprocess.run([str(x) for x in args], **options)
    if result.returncode:
        raise RuntimeError("命令执行失败：" + str(args[0]) + "\n" + result.stderr[-3000:])
    return result.stdout.strip()


def git(path, *args, **kwargs):
    return run(["git", "-c", "core.longpaths=true", "-c", "user.name=CourseSync", "-c", "user.email=course-sync@local.invalid", "-C", path, *args], **kwargs)


def ro(path):
    db = sqlite3.connect(pathlib.Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    return db


def snapshot(src, dst):
    a = ro(src)
    b = sqlite3.connect(dst)
    a.backup(b)
    b.execute("pragma journal_mode=delete")
    b.close()
    a.close()


def safe(root, relative):
    rel = pathlib.PurePosixPath(relative)
    if rel.is_absolute() or ".." in rel.parts or any(":" in x for x in rel.parts):
        raise RuntimeError("同步包路径不合法：" + relative)
    result = pathlib.Path(root).joinpath(*rel.parts)
    if not result.resolve().is_relative_to(pathlib.Path(root).resolve()):
        raise RuntimeError("同步路径越界")
    return result


def normalized_path(path):
    value = str(path).replace("\\", "/").rstrip("/")
    return value[4:] if value.startswith("//?/") else value


def project_keys(cfg):
    """项目使用稳定映射标识，目录名和两端父目录可以不同。"""
    return list(cfg["projects"]) if "projects" in cfg else COURSES


def project_path(cfg, key):
    if "projects" in cfg:
        if key not in cfg["projects"]:
            raise RuntimeError("未选择的项目标识：" + key)
        return pathlib.Path(cfg["projects"][key])
    return pathlib.Path(cfg["course_root"]) / key


def project_file(cfg, relative):
    parts = pathlib.PurePosixPath(relative).parts
    if not parts or parts[0] not in project_keys(cfg):
        raise RuntimeError("文件不属于已选择的项目")
    return safe(project_path(cfg, parts[0]), "/".join(parts[1:]))


def project_relative(cfg, path):
    key = course_of(path, cfg)
    if key is None:
        raise RuntimeError("路径不属于已选择的项目")
    relative = pathlib.Path(path).relative_to(project_path(cfg, key)).as_posix()
    return key if relative == "." else key + "/" + relative


def needs_database(plan):
    return bool(plan["chats"] or plan.get("project_actions"))


def course_of(path, cfg):
    normal = normalized_path(path)
    for course in sorted(project_keys(cfg), key=lambda key: -len(str(project_path(cfg, key)))):
        prefix = normalized_path(project_path(cfg, course))
        current, expected = (normal.casefold(), prefix.casefold()) if re.match(r"^[A-Za-z]:/", prefix) else (normal, prefix)
        if current == expected or current.startswith(expected + "/"):
            return course
    return None


def canonical(value, cfg):
    if isinstance(value, dict):
        result = {k: canonical(v, cfg) for k, v in value.items()}
        if str(value.get("type", "")).lower() == "imageview":
            result["path"] = "@image/" + str(value.get("id"))
        if value.get("type") == "userMessage":
            for i, item in enumerate(result.get("content", [])):
                if item.get("type") == "localImage":
                    item["path"] = "@user-image/" + str(value.get("id")) + "/" + str(i)
        if value.get("type") == "agentMessage" and isinstance(result.get("text"), str):
            index = 0
            def stable_image(match):
                nonlocal index
                path = match.group(2).strip("<>")
                if path.startswith(("@", "/", "file://")) or re.match(r"^[A-Za-z]:[\\/]", path):
                    token = match.group(1) + "@assistant-image/" + str(value.get("id")) + "/" + str(index) + ")"
                    index += 1
                    return token
                return match.group(0)
            result["text"] = re.sub(r"(!\[[^\]]*\]\()([^\n]+?)\)(?=\s|$|[，。；])", stable_image, result["text"])
        return result
    if isinstance(value, list):
        return [canonical(v, cfg) for v in value]
    if not isinstance(value, str) or value.startswith("data:"):
        return value
    if value.startswith("\\\\?\\"):
        value = value[4:]
    aliases = cfg.get("aliases", {})
    if value in aliases:
        return aliases[value]
    if "](" in value or "](<" in value:
        for old, new in aliases.items():
            value = value.replace("](" + old + ")", "](" + new + ")").replace("](<" + old + ">)", "](<" + new + ">)")
    for old, new in cfg["canonical_roots"].items():
        value = value.replace(old, new).replace(old.replace("/", "\\"), new).replace(old.replace("\\", "/"), new)
        if re.match(r"^[A-Za-z]:[/\\]", old):
            for variant in {old, old.replace("/", "\\"), old.replace("\\", "/")}:
                value = re.sub(re.escape(variant), lambda match: new, value, flags=re.IGNORECASE)
    if "](" in value:
        value = re.sub(r"(\]\(<?)/(@(?:course|codex|home)(?=[/\\>)]))", r"\1\2", value)
        value = re.sub(r"\]\(<?@(?:course|codex|home)[^)\n]*", lambda m: m.group(0).replace("\\", "/"), value)
    if value.startswith(("@course", "@codex", "@home")) and "\n" not in value:
        value = value.replace("\\", "/")
    return value


def chat_snapshot(cfg):
    home = pathlib.Path(cfg["codex_home"])
    state, history = ro(home / "state_5.sqlite"), ro(home / "thread_history_1.sqlite")
    chats, rows = {}, {}
    try:
        for row in state.execute("select * from threads"):
            r = dict(row)
            course = course_of(r["cwd"], cfg)
            if not course or r["id"] in cfg.get("exclude_threads", []) or r.get("thread_source") not in (None, "user"):
                continue
            if is_side_chat(r, cfg):
                continue
            tid = r["id"]
            turns = [dict(x) for x in history.execute("select * from thread_turns where thread_id=? order by rollout_ordinal", (tid,))]
            fingerprints = []
            for turn in turns:
                items = [dict(x) for x in history.execute("select * from thread_items where thread_id=? and turn_id=? order by rollout_ordinal", (tid, turn["turn_id"]))]
                content = []
                for item in items:
                    obj = json.loads(item["item_json"])
                    body = canonical(obj, cfg) if item["item_type"] in ["userMessage", "agentMessage"] else {"id": item["item_id"], "type": item["item_type"]}
                    content.append(body)
                fingerprints.append([turn["turn_id"], digest(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())])
            chats[tid] = {"id": tid, "course": course, "name": r.get("name") or r["title"][:80], "turns": fingerprints,
                          "active": bool(turns and turns[-1]["status"] in ["inProgress", "in_progress"]), "archived": bool(r["archived"]),
                          "lineage": cfg.get("lineage", {}).get(tid, tid)}
            rows[tid] = r
    finally:
        state.close()
        history.close()
    return chats, rows


def is_side_chat(row, cfg):
    """同时识别数据库和日志中的侧边聊天标记，兼容旧导入曾丢失父级字段的数据。"""
    if row.get("attached_thread_parent_id"):
        return True
    path = resolve_log(row.get("rollout_path", ""))
    if not path.is_file():
        return False
    first = next(log_lines(path, cfg), None)
    if not first:
        return False
    record = json.loads(first)
    return record.get("type") == "session_meta" and bool(record.get("payload", {}).get("attached_thread"))


def walk_error(error):
    if isinstance(error, PermissionError):
        raise RuntimeError("系统未允许读取课程目录。Mac 请在首次打开课程同步时允许访问文稿；若未出现提示，请在系统设置的隐私与安全性中允许课程同步访问文稿，再重开入口。未安装任何更新。") from error
    raise error


def inventory(cfg, include_git=True):
    root = pathlib.Path(cfg["course_root"])
    cache_path = pathlib.Path(cfg.get("state_dir", str(APP))) / "cache.json"
    cache = read_json(cache_path, {})
    files, file_blobs = {}, {}
    for course in project_keys(cfg):
        directory = project_path(cfg, course)
        if not directory.is_dir():
            if course in cfg.get("new_projects", []) and not directory.exists():
                continue
            raise RuntimeError("课程目录不存在：" + str(directory))
        for current, dirs, names in os.walk(directory, onerror=walk_error):
            # 系统缓存和本机生成的检索索引留在各自设备，资料与学习草稿照常同步。
            dirs[:] = [x for x in dirs if x not in [".git", ".question-notes-index", "__pycache__", "node_modules", ".venv", ".course-sync-conflicts"]]
            for name in names:
                if name in [".DS_Store", "Thumbs.db", ".git"]:
                    continue
                path = pathlib.Path(current) / name
                if path.is_symlink():
                    raise RuntimeError("发现需要单独处理的符号链接：" + str(path))
                rel = course + "/" + path.relative_to(directory).as_posix()
                stat = path.stat()
                signature = [stat.st_size, stat.st_mtime_ns]
                old = cache.get(rel, {})
                if old.get("signature") == signature and old.get("sha") and (not include_git or old.get("git_blob")):
                    sha, blob = old["sha"], old.get("git_blob")
                else:
                    sha, blob = file_hashes(path) if include_git else (file_hash(path), None)
                if [path.stat().st_size, path.stat().st_mtime_ns] != signature:
                    raise RuntimeError("文件正在变化，请稍后同步：" + rel)
                cache[rel] = {"signature": signature, "sha": sha, "git_blob": blob}
                files[rel] = {"sha": sha, "size": stat.st_size}
                if blob:
                    file_blobs[rel] = blob
    save_json(cache_path, cache)
    repos, reachable, ref_tips = {}, {}, {}
    for course in (project_keys(cfg) if include_git else []):
        for current, dirs, names in os.walk(project_path(cfg, course), onerror=walk_error):
            if ".git" in dirs or ".git" in names:
                repo = pathlib.Path(current)
                objects = git(repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").splitlines()
                key = project_relative(cfg, repo)
                repos[key] = sorted(set(objects))
                reachable[key] = sorted(set(git(repo, "rev-list", "--objects", "--no-object-names", "--all").splitlines()))
                rows = git(repo, "for-each-ref", "--format=%(objectname) %(refname)").splitlines()
                ref_tips[key] = sorted({line.split(" ", 1)[0] for line in rows if line.split(" ", 1)[1].startswith(("refs/heads/", "refs/tags/"))})
            dirs[:] = [x for x in dirs if x not in [".git", "node_modules", ".venv"]]
    chats, _ = chat_snapshot(cfg)
    return {"version": SYNC_VERSION, "device": cfg["device"], "files": files, "chats": chats, "git_file_blobs": file_blobs if include_git else {},
            "repos": repos, "git_reachable": reachable, "git_ref_tips": ref_tips,
            "course_root": cfg["course_root"], "project_roots": {key: str(project_path(cfg, key)) for key in project_keys(cfg)}, "codex_home": cfg["codex_home"], "home": cfg["home"]}


def compare_history(local, remote):
    a, b = local["turns"], remote["turns"]
    if a == b:
        return "same"
    if a == b[:len(a)]:
        return "update"
    if b == a[:len(b)]:
        return "local_ahead"
    return "fork"


def plan_sync(local, remote, baseline=None, bindings=None):
    baseline, bindings = baseline or {}, bindings or {}
    result = {"peer": remote["device"], "files": [], "chats": [], "same_files": 0, "local_ahead_chats": 0, "same_chats": 0}
    for rel, source in remote["files"].items():
        dest = local["files"].get(rel)
        old = baseline.get("files", {}).get(rel)
        if dest is None:
            action = "add"
        elif dest["sha"] == source["sha"]:
            result["same_files"] += 1
            continue
        elif old and old["local"] == old["remote"] and dest["sha"] == old["local"] and source["sha"] != old["remote"]:
            action = "update"
        elif old and source["sha"] == old["remote"]:
            continue
        else:
            action = "conflict"
        destination = rel if action != "conflict" else rel.split("/", 1)[0] + "/.course-sync-conflicts/" + remote["device"] + "/" + source["sha"][:12] + "/" + rel.split("/", 1)[1]
        result["files"].append({"source": rel, "destination": destination, "action": action, "sha": source["sha"]})
    for tid, source in remote["chats"].items():
        binding_key = remote["device"] + "/" + tid
        dest_id = bindings.get(binding_key, tid)
        dest = local["chats"].get(dest_id)
        # 分支回传原设备时按共同来源复用已有聊天，避免两端来回同步制造副本。
        lineage = source.get("lineage", tid)
        if dest is None or compare_history(dest, source) == "fork":
            candidates = [(k, v) for k, v in local["chats"].items()
                          if v.get("lineage", k) == lineage and compare_history(v, source) != "fork"]
            if candidates:
                candidates.sort(key=lambda kv: (compare_history(kv[1], source) != "same", -len(kv[1]["turns"]), kv[0]))
                dest_id, dest = candidates[0]
        action = "add" if dest is None else compare_history(dest, source)
        if action == "same":
            result["same_chats"] += 1
            continue
        if action == "local_ahead":
            result["local_ahead_chats"] += 1
            continue
        if source["active"] or (dest and dest["active"]):
            raise RuntimeError("课程对话仍在生成，请等本轮完成：" + source["name"])
        if action == "fork":
            # 同一条远端分支重复同步时复用确定性的标识，避免重复建立副本。
            common = 0
            for a, b in zip(dest["turns"], source["turns"]):
                if a != b:
                    break
                common += 1
            seed = tid + "/" + remote["device"] + "/" + json.dumps(source["turns"][common:common + 1])
            dest_id = str(uuid.uuid5(uuid.NAMESPACE_URL, seed))
            existing = local["chats"].get(dest_id)
            if existing and compare_history(existing, source) in ["same", "local_ahead"]:
                continue
        result["chats"].append({"source": tid, "destination": dest_id, "action": action, "name": source["name"], "course": source["course"], "binding": binding_key})
    return result


def resolve_log(path):
    path = pathlib.Path(path)
    if not path.is_file() and path.with_suffix(path.suffix + ".zst").is_file():
        path = path.with_suffix(path.suffix + ".zst")
    return path


def log_lines(path, cfg):
    path = resolve_log(path)
    if path.suffix == ".zst":
        if cfg.get("python_extra"):
            sys.path.insert(0, cfg["python_extra"])
        import zstandard
        with path.open("rb") as raw, zstandard.ZstdDecompressor().stream_reader(raw) as stream:
            yield from io.BufferedReader(stream)
    else:
        with path.open("rb") as f:
            yield from f


def add_to_tar(archive, path, name):
    archive.add(path, arcname=name, recursive=False)


def recover_log_images(log, tables, directory):
    """只按事件标识或单轮严格一一对应关系恢复内嵌图片。"""
    views, users, pending = {}, {}, {}
    active = None
    turns = sorted(tables["thread_turns"], key=lambda x: x["rollout_ordinal"])
    index, offset = 0, 0
    user_images = collections.defaultdict(list)

    def decode(item):
        url = item.get("image_url", "")
        if not url.startswith("data:image/"):
            return None
        header, body = url.split(",", 1)
        binary = base64.b64decode(body, validate=True)
        suffix = ".jpg" if "jpeg" in header else ".png" if "png" in header else ".webp"
        path = directory / (digest(binary) + suffix)
        if not path.exists():
            path.write_bytes(binary)
        return path

    with log.open("rb") as f:
        for line in f:
            while index + 1 < len(turns) and (turns[index + 1].get("rollout_byte_offset") or 0) <= offset:
                index += 1
            row = json.loads(line)
            data = row.get("payload", {})
            if row.get("type") == "response_item" and data.get("type") in ["custom_tool_call", "function_call"]:
                active = data.get("call_id")
                pending[active] = []
            if data.get("type") == "item_completed" and data.get("item", {}).get("type") == "ImageView" and active:
                pending[active].append(data["item"])
            if row.get("type") == "response_item" and data.get("type") in ["custom_tool_call_output", "function_call_output"]:
                matches = pending.pop(data.get("call_id"), [])
                images = [x for x in data.get("output", []) if isinstance(x, dict) and x.get("type") == "input_image" and x.get("image_url", "").startswith("data:image/")]
                if len(matches) == len(images):
                    for item, image in zip(matches, images):
                        views[item["id"]] = decode(image)
            if row.get("type") == "response_item" and data.get("role") == "user" and turns:
                tagged_path = None
                for item in data.get("content", []):
                    if isinstance(item, dict) and item.get("type") == "input_text":
                        tag = re.fullmatch(r'<image\s+[^>]*path="([^"]+)"[^>]*>', item.get("text", "").strip())
                        tagged_path = tag.group(1) if tag else None
                    if isinstance(item, dict) and item.get("type") == "input_image":
                        path = decode(item)
                        if path:
                            user_images[turns[index]["turn_id"]].append(path)
                            if tagged_path:
                                users[tagged_path] = path
                        tagged_path = None
            offset += len(line)
    for turn in turns:
        paths = []
        items = sorted([x for x in tables["thread_items"] if x["turn_id"] == turn["turn_id"] and x["item_type"] == "userMessage"], key=lambda x: x["rollout_ordinal"])
        for row in items:
            paths.extend(x["path"] for x in json.loads(row["item_json"]).get("content", []) if x.get("type") == "localImage")
        images = user_images[turn["turn_id"]]
        if len(paths) == len(images):
            users.update(zip(paths, images))
    return views, users


def referenced_paths(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from referenced_paths(item)
    elif isinstance(value, list):
        for item in value:
            yield from referenced_paths(item)
    elif isinstance(value, str) and not value.startswith("data:"):
        if "\n" not in value and len(value) < 1500 and (value.startswith(("/", "file://")) or re.match(r"^[A-Z]:[\\/]", value)):
            yield value
        for match in re.finditer(r"\]\(<?([^\n]+?)>?\)(?=\s|$|[，。；])", value):
            yield match.group(1)


def export_git_pack(repo, relative, request, source_inventory, directory):
    """按对象集合发送真正缺少的历史，已有文件可在接收端构建相同的 Git 对象。"""
    have = set(request.get("git_have", {}).get(relative, []))
    refs = dict(line.split(" ", 1)[::-1] for line in git(repo, "for-each-ref", "--format=%(objectname) %(refname)").splitlines())
    if not request.get("include_snapshots", True):
        refs = {name: oid for name, oid in refs.items() if name.startswith(("refs/heads/", "refs/tags/"))}
    protected = set(request.get("git_reachable", request.get("git_have", {})).get(relative, []))
    wanted = {name: oid for name, oid in refs.items() if oid not in protected}
    if not wanted:
        return None
    required = set(git(repo, "rev-list", "--objects", "--no-object-names", *sorted(set(wanted.values()))).splitlines())
    candidates = {}
    for rel, oid in request.get("git_file_blobs", {}).items():
        if rel.startswith(relative + "/"):
            candidates.setdefault(oid, rel)
    for action in request.get("files", []):
        if action["destination"].startswith(relative + "/"):
            oid = source_inventory.get("git_file_blobs", {}).get(action["source"])
            if oid:
                candidates.setdefault(oid, action["destination"])
    prime = {oid: candidates[oid] for oid in sorted(required - have) if oid in candidates}
    missing = sorted(required - have - set(prime))
    path = directory / (digest(relative.encode())[:16] + ".pack")
    if missing:
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        with path.open("wb") as output:
            result = subprocess.run(["git", "-C", str(repo), "pack-objects", "--stdout"],
                                    input=("\n".join(missing) + "\n").encode(), stdout=output, stderr=subprocess.PIPE, **options)
        if result.returncode:
            raise RuntimeError("Git 增量对象打包失败：" + result.stderr.decode("utf-8", "replace")[-2000:])
    return {"repo": relative, "file": path.name if missing else None, "sha": file_hash(path) if missing else None,
            "refs": wanted, "required": sorted(required), "prime_files": prime,
            "object_format": git(repo, "rev-parse", "--show-object-format"),
            "sent_objects": len(missing), "reused_objects": len(required) - len(missing)}


def export_package(cfg, request):
    directory = pathlib.Path(cfg.get("state_dir", str(APP))) / "work" / uuid.uuid4().hex
    directory.mkdir(parents=True)
    archive_path = directory / "export.tar.gz"
    source_inventory = inventory(cfg, include_git=request.get("include_git", False))
    for action in request["files"]:
        if source_inventory["files"].get(action["source"], {}).get("sha") != action["sha"]:
            raise RuntimeError("检查后源文件发生变化，请重新同步")
    chats, rows = chat_snapshot(cfg)
    for action in request["chats"]:
        if chats.get(action["source"]) != request["source_chats"].get(action["source"]):
            raise RuntimeError("检查后源对话发生变化，请重新同步")
        if chats[action["source"]]["active"]:
            raise RuntimeError("源对话仍在生成，请等本轮完成")
    metadata = {"version": SYNC_VERSION, "source": source_inventory, "threads": {}, "assets": {}, "path_assets": {}, "bundles": [], "packs": []}
    metadata["sent_files"] = {action["source"]: action["sha"] for action in request["files"]}
    archived_assets = set()
    home = pathlib.Path(cfg["codex_home"])
    state, history = ro(home / "state_5.sqlite"), ro(home / "thread_history_1.sqlite")
    try:
        with tarfile.open(archive_path, "w:gz", compresslevel=3) as archive:
            for action in request["files"]:
                add_to_tar(archive, project_file(cfg, action["source"]), "files/" + action["source"])
            for action in request["chats"]:
                tid = action["source"]
                data = {"thread": rows[tid], "tables": {}}
                for table in STATE_TABLES:
                    data["tables"][table] = [dict(x) for x in state.execute('select * from "' + table + '" where thread_id=?', (tid,))]
                for table in HISTORY_TABLES:
                    data["tables"][table] = [dict(x) for x in history.execute('select * from "' + table + '" where thread_id=?', (tid,))]
                log = directory / (tid + ".jsonl")
                with log.open("wb") as f:
                    for line in log_lines(rows[tid]["rollout_path"], cfg):
                        json.loads(line)
                        f.write(line)
                add_to_tar(archive, log, "logs/" + tid + ".jsonl")
                data["log_sha"] = file_hash(log)
                recovered_views, recovered_users = recover_log_images(log, data["tables"], directory)
                for row in data["tables"]["thread_items"]:
                    obj = json.loads(row["item_json"])
                    if obj.get("type") == "userMessage":
                        for item in obj.get("content", []):
                            if item.get("type") == "localImage":
                                raw = item["path"]
                                decoded = urllib.parse.unquote(raw.removeprefix("file://"))
                                if os.name == "nt" and re.match(r"^/[A-Za-z]:/", decoded):
                                    decoded = decoded[1:]
                                p = recovered_users.get(raw) or pathlib.Path(decoded)
                                if p and p.is_file():
                                    name = file_hash(p) + p.suffix.lower()
                                    if name not in archived_assets:
                                        add_to_tar(archive, p, "assets/" + name)
                                        archived_assets.add(name)
                                    metadata["path_assets"][raw] = name
                                else:
                                    raise RuntimeError("用户上传的历史图片无法恢复：" + raw)
                    if obj.get("type") != "imageView":
                        continue
                    raw_path = obj["path"]
                    # 临时文件名可能已被后来课程复用，优先采用按事件标识保存的历史原图。
                    fallback = cfg.get("image_fallbacks", {}).get(tid + "/" + obj["id"])
                    candidates = [pathlib.Path(fallback)] if fallback else []
                    if recovered_views.get(obj["id"]):
                        candidates.append(recovered_views[obj["id"]])
                    candidates.append(pathlib.Path(urllib.parse.unquote(raw_path.removeprefix("file://"))))
                    path = next((p for p in candidates if p.is_file()), candidates[-1])
                    if not path.is_file():
                        raise RuntimeError("历史图片缺失，已停止导入以保护完整性：" + raw_path)
                    name = file_hash(path) + path.suffix.lower()
                    key = tid + "/" + obj["id"]
                    if name not in archived_assets:
                        add_to_tar(archive, path, "assets/" + name)
                        archived_assets.add(name)
                    metadata["assets"][key] = {"name": name, "original": raw_path}
                    metadata["path_assets"][raw_path] = name
                for row in data["tables"]["thread_items"]:
                    for raw in referenced_paths(json.loads(row["item_json"])):
                        decoded = urllib.parse.unquote(raw.removeprefix("file://"))
                        p = pathlib.Path(decoded)
                        if not p.is_file() or raw in metadata["path_assets"]:
                            continue
                        if not any(p.resolve().is_relative_to(home / folder) for folder in ["visualizations", "migrated-assets"]):
                            continue
                        relatives = [p]
                        if p.suffix.lower() == ".html":
                            relatives = [q for q in p.parent.rglob("*") if q.is_file() and not any(x in q.parts for x in [".git", "node_modules"])]
                        group = digest(canonical(str(p), cfg).rsplit("/", 1)[0].encode())[:16]
                        for q in relatives:
                            name = "linked/" + group + "/" + q.relative_to(p.parent).as_posix()
                            if name not in archived_assets:
                                add_to_tar(archive, q, "assets/" + name)
                                archived_assets.add(name)
                            metadata["path_assets"][str(q)] = name
                            if q == p:
                                metadata["path_assets"][raw] = name
                metadata["threads"][tid] = data
            # 所有历史与快照引用都保留，只传目标端缺少的对象。
            for course in (project_keys(cfg) if request.get("include_git", False) else []):
                root = project_path(cfg, course)
                repos = [root] if (root / ".git").exists() else []
                for current, dirs, _ in os.walk(root, onerror=walk_error):
                    if ".git" in dirs:
                        if pathlib.Path(current) != root:
                            repos.append(pathlib.Path(current))
                        dirs.remove(".git")
                for repo in repos:
                    if len(git(repo, "worktree", "list", "--porcelain").split("worktree ")) > 2:
                        raise RuntimeError("检测到额外链接工作树，需要先单独接力：" + str(repo))
                    rel = project_relative(cfg, repo)
                    pack = export_git_pack(repo, rel, request, source_inventory, directory)
                    if pack:
                        if pack["file"]:
                            add_to_tar(archive, directory / pack["file"], "packs/" + pack["file"])
                        metadata["packs"].append(pack)
            # 同一临时文件名在历史中可能反复使用，正文按最近的图像事件匹配，不能用最后一张图覆盖所有轮次。
            metadata["scoped_assets"] = {}
            for tid, data in metadata["threads"].items():
                known = {}
                for row in sorted(data["tables"]["thread_items"], key=lambda x: x["rollout_ordinal"]):
                    obj = json.loads(row["item_json"])
                    key = tid + "/" + row["item_id"]
                    if obj.get("type", "").lower() == "imageview" and key in metadata["assets"]:
                        known[asset_key(obj["path"])] = metadata["assets"][key]["name"]
                    scoped = {raw: known[asset_key(raw)] for raw in referenced_paths(obj) if asset_key(raw) in known}
                    if scoped:
                        metadata["scoped_assets"][key] = scoped
            metadata["canonical_assets"] = {raw: canonical(raw, cfg) for raw in metadata["path_assets"]}
            save_json(directory / "metadata.json", metadata)
            add_to_tar(archive, directory / "metadata.json", "metadata.json")
        # 导出过程中若聊天发生变化，不交付可能不一致的数据包。
        latest, _ = chat_snapshot(cfg)
        for action in request["chats"]:
            if latest.get(action["source"]) != chats[action["source"]]:
                raise RuntimeError("导出时源对话发生变化，请等聊天停止后重试")
        for action in request["files"]:
            if file_hash(project_file(cfg, action["source"])) != action["sha"]:
                raise RuntimeError("导出时源文件发生变化，请保存后重试")
    except Exception:
        remove_sync_tree(directory)
        raise
    finally:
        state.close()
        history.close()
    return {"path": str(archive_path), "sha": file_hash(archive_path), "size": archive_path.stat().st_size,
            "git_bytes": sum((directory / p["file"]).stat().st_size for p in metadata["packs"] if p["file"]),
            "reused_git_objects": sum(p["reused_objects"] for p in metadata["packs"])}


def extract_package(path, target):
    with tarfile.open(path) as archive:
        for member in archive:
            destination = safe(target, member.name)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            elif member.isfile() and not member.issym() and not member.islnk():
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as f:
                    shutil.copyfileobj(archive.extractfile(member), f)
            else:
                raise RuntimeError("同步包含不支持的文件类型")


def asset_key(value):
    decoded = urllib.parse.unquote(value.removeprefix("file://"))
    if re.match(r"^/[A-Za-z]:/", decoded):
        decoded = decoded[1:]
    return normalized_path(decoded)


def map_value(value, source, cfg, image_paths, source_id, dest_id):
    if isinstance(value, list):
        return [map_value(x, source, cfg, image_paths, source_id, dest_id) for x in value]
    if isinstance(value, dict):
        scoped = cfg.get("scoped_assets", {}).get(source_id + "/" + str(value.get("id")))
        if scoped:
            cfg = dict(cfg, path_assets={**cfg.get("path_assets", {}), **scoped},
                       normalized_assets={**cfg.get("normalized_assets", {}), **{asset_key(k): v for k, v in scoped.items()}})
        out = {k: map_value(v, source, cfg, image_paths, source_id, dest_id) for k, v in value.items()}
        for key in ["model_provider", "modelProvider"]:
            if key in out:
                out[key] = cfg["native_provider"]
        for key in ["thread_id", "threadId", "session_id", "sessionId", "id"]:
            if out.get(key) == source_id:
                out[key] = dest_id
        if str(value.get("type", "")).lower() == "imageview":
            path = image_paths.get(source_id + "/" + str(value.get("id")))
            if path:
                out["path"] = path
        return out
    if not isinstance(value, str) or value.startswith("data:"):
        return value
    if value in cfg.get("path_assets", {}):
        return cfg["path_assets"][value]
    if "\n" not in value and asset_key(value) in cfg.get("normalized_assets", {}):
        return cfg["normalized_assets"][asset_key(value)]
    if "](" in value:
        for old, new in cfg.get("path_assets", {}).items():
            value = value.replace("](" + old + ")", "](" + new + ")").replace("](<" + old + ">)", "](<" + new + ">)")
        def rewrite_link(match):
            old = match.group(1)
            new = map_value(old, source, cfg, image_paths, source_id, dest_id)
            return "](<" + new + ">)" if match.group(0).startswith("](<") else "](" + new + ")"
        value = re.sub(r"\]\(<?([^\n]+?)>?\)(?=\s|$|[，。；])", rewrite_link, value)
    raw = value
    is_url = value.startswith("file://")
    decoded = urllib.parse.unquote(value[7:]) if is_url else value
    if re.match(r"^/[A-Za-z]:[/\\]", decoded):
        decoded = decoded[1:]
        if not is_url:
            raw = decoded
    normal = normalized_path(decoded)
    root_pairs = [(root, cfg["projects"][key] if "projects" in cfg else str(project_path(cfg, key)))
                  for key, root in source.get("project_roots", {}).items() if key in project_keys(cfg)]
    root_pairs.sort(key=lambda pair: -len(pair[0]))
    root_pairs.extend([(source["codex_home"], cfg["codex_home"]), (source["home"], cfg["home"])])
    if "project_roots" not in source:
        root_pairs.insert(0, (source["course_root"], cfg["course_root"]))
    for old, new in root_pairs:
        old_normal = normalized_path(old)
        current, expected = (normal.casefold(), old_normal.casefold()) if re.match(r"^[A-Za-z]:/", old_normal) else (normal, old_normal)
        if "\n" not in normal and len(normal) < 1500 and (current == expected or current.startswith(expected + "/")):
            path_type = pathlib.PureWindowsPath if re.match(r"^[A-Za-z]:[/\\]", new) else pathlib.PurePosixPath
            result = path_type(new).joinpath(*pathlib.PurePosixPath(normal[len(old_normal):].lstrip("/")).parts)
            return result.as_uri() if is_url else str(result)
    return raw


def insert_row(db, table, row):
    allowed = {x[1] for x in db.execute('pragma table_info("' + table + '")')}
    if set(row) - allowed:
        raise RuntimeError("两端聊天数据库版本不兼容，请先更新应用：" + table)
    keys = [k for k in row if k in allowed]
    db.execute('insert into "' + table + '" (' + ','.join('"' + k + '"' for k in keys) + ') values (' + ','.join('?' for _ in keys) + ')', [row[k] for k in keys])


def unaffected_digest(db, table, excluded):
    key = "id" if table == "threads" else "thread_id"
    # 历史库可能超过 GB，逐行验证，避免把全部历史消息载入内存。
    columns = db.execute('pragma table_info("' + table + '")').fetchall()
    order = [x[1] for x in sorted(columns, key=lambda x: x[5]) if x[5]]
    sql = 'select * from "' + table + '"'
    if excluded:
        sql += ' where "' + key + '" not in (' + ','.join('?' for _ in excluded) + ')'
    sql += ' order by ' + ','.join('"' + x + '"' for x in order)
    h = hashlib.sha256()
    for row in db.execute(sql, excluded):
        h.update(repr(tuple(row)).encode())
        h.update(b"\n")
    return h.hexdigest()


def snapshot_selected_chats(live, destination, plan):
    """只导出本次目标聊天的数据库行，以及这些聊天涉及的项目记录。"""
    ids = list(dict.fromkeys(action["destination"] for action in plan["chats"]))
    project_ids = set()
    roots = {normalized_path(project_path(live, action["course"])) for action in plan["chats"] + plan.get("project_actions", [])}
    home = pathlib.Path(live["codex_home"])
    with contextlib.closing(ro(home / "state_5.sqlite")) as source:
        for row in source.execute("select project_id,path from project_roots"):
            if normalized_path(row[1]) in roots:
                project_ids.add(row[0])
        for tid in ids:
            row = source.execute("select project_id from threads where id=?", (tid,)).fetchone()
            if row and row[0]:
                project_ids.add(row[0])
        next_position = source.execute("select coalesce(max(position),-1)+1 from projects").fetchone()[0]
    for name, tables in [("state_5.sqlite", ["projects", "project_roots", "threads"] + STATE_TABLES),
                         ("thread_history_1.sqlite", HISTORY_TABLES)]:
        with contextlib.closing(ro(home / name)) as source, contextlib.closing(sqlite3.connect(destination / name)) as target:
            source.execute("begin")
            for table in tables:
                definition = source.execute("select sql from sqlite_master where type='table' and name=?", (table,)).fetchone()
                if not definition:
                    raise RuntimeError("聊天数据库缺少所需数据表：" + table)
                target.execute(definition[0])
                key = "id" if table in ["threads", "projects"] else "project_id" if table == "project_roots" else "thread_id"
                selected = sorted(project_ids) if table in ["projects", "project_roots"] else ids
                for selected_id in selected:
                    for row in source.execute('select * from "' + table + '" where "' + key + '"=?', (selected_id,)):
                        insert_row(target, table, dict(row))
            # 保留相关表的触发器，使待安装数据与正式数据库采用相同的派生字段规则。
            for trigger in source.execute("select tbl_name,sql from sqlite_master where type='trigger'"):
                if trigger[0] in tables:
                    target.execute(trigger[1])
            target.commit()
    return {"format": "selected-chats-v1", "thread_ids": ids, "next_project_position": next_position}


def metadata_changes(before, after, path=()):
    """只保存实际改变的侧栏字段，保留其他项目的设置。"""
    changes = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if key in before and key in after and isinstance(old, dict) and isinstance(new, dict):
            changes.extend(metadata_changes(old, new, path + (key,)))
        elif (key in before) != (key in after) or old != new:
            changes.append({"path": list(path + (key,)), "before_exists": key in before, "before": old,
                            "after_exists": key in after, "after": new})
    return changes


def install_selected_databases(cfg, backup, stage):
    """逐库事务更新指定聊天，跨库中断时由已落盘的原记录进行幂等回退。"""
    ids = read_json(backup / "backup-scope.json")["thread_ids"]
    home = pathlib.Path(cfg["codex_home"])
    with contextlib.closing(ro(backup / "before/state_5.sqlite")) as old, contextlib.closing(ro(backup / "after/state_5.sqlite")) as new:
        created_projects = {row[0] for row in new.execute("select id from projects")} - {row[0] for row in old.execute("select id from projects")}
        created_rows = {pid: tuple(new.execute("select * from projects where id=?", (pid,)).fetchone()) for pid in created_projects}
    for name, tables in [("state_5.sqlite", ["threads"] + STATE_TABLES), ("thread_history_1.sqlite", HISTORY_TABLES)]:
        with contextlib.closing(ro(backup / stage / name)) as source, contextlib.closing(sqlite3.connect(home / name, timeout=15)) as target:
            try:
                target.execute("begin immediate")
                owned_projects = set()
                if name == "state_5.sqlite" and stage == "before":
                    for tid in ids:
                        row = target.execute("select project_id from threads where id=?", (tid,)).fetchone()
                        if row and row[0] in created_projects:
                            owned_projects.add(row[0])
                    for pid, expected in created_rows.items():
                        row = target.execute("select * from projects where id=?", (pid,)).fetchone()
                        if row and tuple(row) == expected:
                            owned_projects.add(pid)
                for table in list(reversed(tables)):
                    key = "id" if table == "threads" else "thread_id"
                    target.executemany('delete from "' + table + '" where "' + key + '"=?', [(tid,) for tid in ids])
                if name == "state_5.sqlite" and stage == "after":
                    for pid in sorted(created_projects):
                        if target.execute("select 1 from projects where id=?", (pid,)).fetchone():
                            raise RuntimeError("待创建项目标识已被使用，保留备份并停止安装")
                        insert_row(target, "projects", dict(source.execute("select * from projects where id=?", (pid,)).fetchone()))
                        for row in source.execute("select * from project_roots where project_id=?", (pid,)):
                            insert_row(target, "project_roots", dict(row))
                for table in tables:
                    for row in source.execute('select * from "' + table + '"'):
                        insert_row(target, table, dict(row))
                if name == "state_5.sqlite" and stage == "before":
                    for pid in sorted(owned_projects):
                        if not target.execute("select 1 from threads where project_id=?", (pid,)).fetchone():
                            target.execute("delete from project_roots where project_id=?", (pid,))
                            target.execute("delete from projects where id=?", (pid,))
                target.commit()
            except Exception:
                target.rollback()
                raise
        if stage == "after" and name == "state_5.sqlite" and cfg.get("test_failure_stage") == "state_database":
            raise RuntimeError("隔离测试主动触发两库提交之间的中断")


def install_chat_metadata(cfg, backup, stage):
    """合并本次侧栏差异和索引条目，允许重复恢复。"""
    home = pathlib.Path(cfg["codex_home"])
    current = read_json(home / ".codex-global-state.json", {})
    other = "after" if stage == "before" else "before"
    for change in read_json(backup / "metadata-changes.json"):
        parent = current
        for key in change["path"][:-1]:
            if key not in parent or not isinstance(parent[key], dict):
                raise RuntimeError("相关侧栏设置已改变，保留备份等待核对")
            parent = parent[key]
        key = change["path"][-1]
        exists = key in parent
        if exists == change[stage + "_exists"] and parent.get(key) == change[stage]:
            continue
        if exists != change[other + "_exists"] or parent.get(key) != change[other]:
            raise RuntimeError("相关侧栏字段已被其他操作修改，保留备份等待核对")
        if change[stage + "_exists"]:
            parent[key] = change[stage]
        else:
            parent.pop(key, None)
    save_json(home / ".codex-global-state.json", current)
    ids = set(read_json(backup / "backup-scope.json")["thread_ids"])
    index = home / "session_index.jsonl"
    remaining = [line for line in index.read_text(encoding="utf-8").splitlines(keepends=True)
                 if line.strip() and json.loads(line).get("id") not in ids]
    selected = (backup / stage / "session_index.jsonl").read_text(encoding="utf-8")
    temporary = index.with_name(index.name + ".course-sync-pending")
    prefix = "".join(remaining)
    if prefix and selected and not prefix.endswith("\n"):
        prefix += "\n"
    temporary.write_text(prefix + selected, encoding="utf-8")
    os.replace(temporary, index)


def restore_chat_backup(cfg, backup):
    """兼容按聊天保存的新备份与旧版本的完整库备份。"""
    if (backup / "backup-scope.json").exists():
        install_selected_databases(cfg, backup, "before")
        install_chat_metadata(cfg, backup, "before")
    else:
        live = pathlib.Path(cfg["codex_home"])
        for name in ["state_5.sqlite", "thread_history_1.sqlite", ".codex-global-state.json", "session_index.jsonl"]:
            shutil.copy2(backup / "before" / name, live / name)


def ensure_project(state, globals_, cfg, course, scope, now):
    """依据明确的目录映射复用或创建项目，项目标题只用于显示。"""
    live = pathlib.Path(cfg["codex_home"])
    cwd = str(project_path(cfg, course))
    host = "local:" + str(live)
    project_map = globals_.setdefault("app-server-project-id-by-legacy-project-id-by-host", {}).setdefault(host, {})
    candidates = [(key, project) for key, project in globals_.get("local-projects", {}).items()
                  if any(normalized_path(root) == normalized_path(cwd) for root in project.get("rootPaths", []))]
    if not candidates:
        if course not in cfg.get("new_projects", []):
            raise RuntimeError("目标项目不存在，请明确选择新建迁移：" + course)
        sidebar_id = str(uuid.uuid4())
        project = {"id": sidebar_id, "name": cfg.get("project_names", {}).get(course, course),
                   "rootPaths": [cwd], "createdAt": now, "updatedAt": now}
        globals_.setdefault("local-projects", {})[sidebar_id] = project
        candidates = [(sidebar_id, project)]
    candidates.sort(key=lambda item: (project_map.get(item[0]) == item[0], item[0]))
    sidebar_pid = candidates[0][0]
    existing_project = next(((x[0],) for x in state.execute("select project_id,path from project_roots") if normalized_path(x[1]) == normalized_path(cwd)), None)
    pid = existing_project[0] if existing_project else str(uuid.uuid5(uuid.NAMESPACE_URL, "course-sync:" + cwd))
    if existing_project is None:
        pos = scope["next_project_position"]
        scope["next_project_position"] += 1
        insert_row(state, "projects", {"id": pid, "name": cfg.get("project_names", {}).get(course, course), "metadata": "{}", "position": pos, "created_at_ms": now, "updated_at_ms": now})
        insert_row(state, "project_roots", {"project_id": pid, "position": 0, "path": cwd})
    for key, value in [("project-order", sidebar_pid), ("electron-saved-workspace-roots", cwd)]:
        if value not in globals_.setdefault(key, []):
            globals_[key].append(value)
    project_map[sidebar_pid] = pid
    return sidebar_pid, pid, cwd


def prepare_import(cfg, plan, package, backup):
    live = pathlib.Path(cfg["codex_home"])
    unpack = backup / "incoming"
    unpack.mkdir()
    extract_package(package, unpack)
    metadata = read_json(unpack / "metadata.json")
    if metadata["source"]["device"] != plan["peer"]:
        raise RuntimeError("同步包来自错误的设备")
    for action in plan["chats"]:
        source_row = dict(metadata["threads"][action["source"]]["thread"])
        source_row["rollout_path"] = str(unpack / "logs" / (action["source"] + ".jsonl"))
        if is_side_chat(source_row, cfg):
            raise RuntimeError("更新包包含侧边聊天，请重新检查更新。侧边聊天不参与同步")
    before, after = backup / "before", backup / "after"
    before.mkdir()
    after.mkdir()
    for action in plan["files"]:
        source = safe(unpack / "files", action["source"])
        if file_hash(source) != action["sha"]:
            raise RuntimeError("同步包文件哈希不一致")
        target = safe(after / "files", action["destination"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    if not needs_database(plan):
        save_json(backup / "plan.json", plan)
        save_json(backup / "retired-logs.json", [])
        return metadata
    scope = snapshot_selected_chats(cfg, before, plan)
    save_json(backup / "backup-scope.json", scope)
    for name in ["state_5.sqlite", "thread_history_1.sqlite"]:
        shutil.copy2(before / name, after / name)
    index_lines = [line if line.endswith("\n") else line + "\n" for line in (live / "session_index.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
                   if line.strip() and json.loads(line).get("id") in scope["thread_ids"]]
    for directory in [before, after]:
        (directory / "session_index.jsonl").write_text("".join(index_lines), encoding="utf-8")
    state, history = sqlite3.connect(after / "state_5.sqlite"), sqlite3.connect(after / "thread_history_1.sqlite")
    old_state, old_history = ro(before / "state_5.sqlite"), ro(before / "thread_history_1.sqlite")
    affected = [x["destination"] for x in plan["chats"]]
    original = {table: unaffected_digest(db, table, affected) for db, tables in [(old_state, ["threads"] + STATE_TABLES), (old_history, HISTORY_TABLES)] for table in tables}
    original_globals = read_json(live / ".codex-global-state.json", {})
    globals_ = copy.deepcopy(original_globals)
    retired_logs = []
    now = int(time.time() * 1000)
    for action in plan.get("project_actions", []):
        ensure_project(state, globals_, cfg, action["course"], scope, now)
    image_paths = {key: str(live / "migrated-assets/project-sync" / data["name"]) for key, data in metadata["assets"].items()}
    cfg = dict(cfg, path_assets={raw: str(live / "migrated-assets/project-sync" / name) for raw, name in metadata.get("path_assets", {}).items()})
    cfg["normalized_assets"] = {asset_key(raw): value for raw, value in cfg["path_assets"].items()}
    cfg["scoped_assets"] = {key: {raw: str(live / "migrated-assets/project-sync" / name) for raw, name in paths.items()}
                            for key, paths in metadata.get("scoped_assets", {}).items()}
    for action in plan["chats"]:
        source_id, tid, course = action["source"], action["destination"], action["course"]
        data = metadata["threads"][source_id]
        log_source = unpack / "logs" / (source_id + ".jsonl")
        assert file_hash(log_source) == data["log_sha"]
        sidebar_pid, pid, cwd = ensure_project(state, globals_, cfg, course, scope, now)
        project_map = globals_["app-server-project-id-by-legacy-project-id-by-host"]["local:" + str(live)]
        # 每次安装使用独立日志路径，失败时可完整隔离新日志，原日志不覆盖。
        stamp = datetime.datetime.fromtimestamp(data["thread"]["created_at"]).strftime("%Y-%m-%dT%H-%M-%S")
        rel = pathlib.Path("sessions/course-sync") / backup.name / ("rollout-" + stamp + "-" + tid + ".jsonl")
        log = after / rel
        log.parent.mkdir(parents=True, exist_ok=True)
        mapping = {0: 0}
        before_offset = after_offset = 0
        log_cfg = dict(cfg, path_assets=dict(cfg["path_assets"]), normalized_assets=dict(cfg["normalized_assets"]))
        def remember_images(value):
            if isinstance(value, dict):
                key = source_id + "/" + str(value.get("id"))
                if str(value.get("type", "")).lower() == "imageview" and key in image_paths:
                    log_cfg["path_assets"][value["path"]] = image_paths[key]
                    log_cfg["normalized_assets"][asset_key(value["path"])] = image_paths[key]
                for child in value.values():
                    remember_images(child)
            elif isinstance(value, list):
                for child in value:
                    remember_images(child)
        with log_source.open("rb") as src, log.open("wb") as dst:
            for line in src:
                obj = json.loads(line)
                remember_images(obj)
                transformed = map_value(obj, metadata["source"], log_cfg, image_paths, source_id, tid)
                encoded = line if obj == transformed else (json.dumps(transformed, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                dst.write(encoded)
                before_offset += len(line)
                after_offset += len(encoded)
                mapping[before_offset] = after_offset
        existing = state.execute("select rollout_path from threads where id=?", (tid,)).fetchone()
        old_paths = []
        if existing and resolve_log(existing[0]).is_file():
            old_paths.append(pathlib.Path(normalized_path(resolve_log(existing[0]))))
        for folder in [live / "sessions", live / "archived_sessions"]:
            for old_path in folder.rglob("*" + tid + "*.jsonl*"):
                if old_path.is_file() and not any(old_path.samefile(candidate) for candidate in old_paths):
                    old_paths.append(old_path)
        for old_path in old_paths:
            first = next(log_lines(old_path, cfg), None)
            record = json.loads(first) if first else {}
            if record.get("payload", {}).get("id") != tid:
                raise RuntimeError("旧日志的聊天标识与数据库不一致，停止安装")
            relative = old_path.relative_to(live)
            saved = before / "retired-logs" / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old_path, saved)
            retired_logs.append({"path": relative.as_posix(), "sha": file_hash(saved)})
        for table in STATE_TABLES:
            state.execute('delete from "' + table + '" where thread_id=?', (tid,))
        state.execute("delete from threads where id=?", (tid,))
        row = map_value(dict(data["thread"]), metadata["source"], cfg, image_paths, source_id, tid)
        row.update(id=tid, cwd=cwd, rollout_path=str(live / rel), model_provider=cfg["native_provider"], project_id=pid,
                   thread_section_id=None, section_position=None, section_entered_at_ms=None, attached_thread_parent_id=None)
        local_row = old_state.execute("select * from threads where id=?", (tid,)).fetchone()
        if local_row:
            local_row = dict(local_row)
            for key in ["archived", "archived_at", "thread_section_id", "section_position", "section_entered_at_ms"]:
                if key in local_row:
                    row[key] = local_row[key]
            if local_row.get("name"):
                row["name"] = local_row["name"]
        if action["action"] == "fork":
            row["name"] = action["name"] + " · " + plan["peer"] + "分支"
        if row.get("sandbox_policy"):
            row["sandbox_policy"] = json.dumps(map_value(json.loads(row["sandbox_policy"]), metadata["source"], cfg, image_paths, source_id, tid), ensure_ascii=False)
        insert_row(state, "threads", row)
        for table in STATE_TABLES:
            for source_row in data["tables"][table]:
                copied = map_value(dict(source_row), metadata["source"], cfg, image_paths, source_id, tid)
                copied["thread_id"] = tid
                if table == "thread_attachments" and tid != source_id:
                    copied["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, tid + copied["id"]))
                insert_row(state, table, copied)
        for table in HISTORY_TABLES:
            history.execute('delete from "' + table + '" where thread_id=?', (tid,))
            for source_row in data["tables"][table]:
                copied = dict(source_row)
                copied["thread_id"] = tid
                for key in ["rollout_byte_offset", "rollout_end_byte_offset", "next_rollout_byte_offset"]:
                    if key in copied and copied[key] is not None:
                        if copied[key] not in mapping:
                            raise RuntimeError("聊天分页位置与日志不一致，停止安装")
                        copied[key] = mapping[copied[key]]
                if "item_json" in copied:
                    copied["item_json"] = json.dumps(map_value(json.loads(copied["item_json"]), metadata["source"], cfg, image_paths, source_id, tid), ensure_ascii=False, separators=(",", ":"))
                insert_row(history, table, copied)
        globals_.setdefault("thread-project-assignments", {})[tid] = {"projectKind": "local", "projectId": sidebar_pid, "cwd": cwd, "pendingCoreUpdate": False}
        globals_.setdefault("thread-workspace-root-hints", {})[tid] = cwd
        globals_.setdefault("thread-writable-roots", {})[tid] = [cwd]
        for key, val in [("project-order", sidebar_pid), ("electron-saved-workspace-roots", cwd)]:
            if val not in globals_.setdefault(key, []):
                globals_[key].append(val)
        order = globals_.setdefault("sidebar-project-thread-orders", {}).setdefault(sidebar_pid, {"threadIds": []})["threadIds"]
        if tid not in order:
            order.append(tid)
        project_map[sidebar_pid] = pid
        with (after / "session_index.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": tid, "thread_name": row.get("name"), "updated_at": datetime.datetime.fromtimestamp(row["updated_at"], datetime.timezone.utc).isoformat().replace("+00:00", "Z")}, ensure_ascii=False) + "\n")
    save_json(backup / "metadata-changes.json", metadata_changes(original_globals, globals_))
    for db, tables in [(state, ["threads"] + STATE_TABLES), (history, HISTORY_TABLES)]:
        db.commit()
        assert db.execute("pragma integrity_check").fetchone()[0] == "ok"
        for table in tables:
            if unaffected_digest(db, table, affected) != original[table]:
                raise RuntimeError("未涉及的聊天发生改变，停止导入：" + table)
        db.close()
    old_state.close()
    old_history.close()
    save_json(backup / "plan.json", plan)
    save_json(backup / "retired-logs.json", retired_logs)
    return metadata


def app_running():
    if os.name == "nt":
        result = run(["tasklist", "/FI", "IMAGENAME eq ChatGPT.exe", "/FO", "CSV", "/NH"])
        return '"ChatGPT.exe"' in result
    return subprocess.run(["pgrep", "-x", "ChatGPT"], capture_output=True).returncode == 0


def backend_started_at():
    """用现存后端生命周期识别上次崩溃遗留的进行中标记，无法核验则保守停止。"""
    try:
        if os.name == "nt":
            command = "$syncProcesses=Get-Process -Name ChatGPT,codex -ErrorAction SilentlyContinue; $syncStart=($syncProcesses | Sort-Object StartTime | Select-Object -First 1).StartTime; if($syncStart){([DateTimeOffset]$syncStart).ToUnixTimeSeconds()}"
            return int(run(["powershell", "-NoProfile", "-Command", command])) - 5
        elapsed = []
        for line in run(["ps", "-axo", "etime=,comm="]).splitlines():
            duration, name = line.strip().split(None, 1)
            if pathlib.Path(name).name not in ["ChatGPT", "codex"]:
                continue
            days, duration = duration.split("-", 1) if "-" in duration else ("0", duration)
            seconds = 0
            for value in duration.split(":"):
                seconds = seconds * 60 + int(value)
            elapsed.append(int(days) * 86400 + seconds)
        return time.time() - max(elapsed) - 5 if elapsed else 0
    except (ValueError, RuntimeError):
        return 0


def unfinished_in_current_backend(turn, updated_at, started_at):
    return turn["status"] in ["inProgress", "in_progress"] and max(turn.get("started_at") or 0, updated_at or 0) >= started_at


def check_idle(cfg):
    if not app_running():
        return
    if not cfg.get("test_mode"):
        home = pathlib.Path(cfg["codex_home"])
        with contextlib.closing(ro(home / "thread_history_1.sqlite")) as db, contextlib.closing(ro(home / "state_5.sqlite")) as state:
            rows = {x[0]: x[1] for x in state.execute("select id,updated_at from threads")}
            started = backend_started_at()
            latest = db.execute("select thread_id,status,started_at from thread_turns t where rollout_ordinal=(select max(rollout_ordinal) from thread_turns s where s.thread_id=t.thread_id)")
            if any(t["thread_id"] in rows and unfinished_in_current_backend(dict(t), rows[t["thread_id"]], started) for t in latest):
                raise RuntimeError("本机有聊天尚未结束。请等待本轮完成后重试；若是旧聊天残留状态，请保存工作并完全退出本机 ChatGPT 后再同步。")


def close_local_app(cfg):
    if not app_running():
        return False
    check_idle(cfg)
    if os.name == "nt":
        import windows_app
        cfg["_last_shutdown"] = windows_app.request_quit(cfg)
    else:
        run(["osascript", "-e", 'tell application "ChatGPT" to quit'], timeout=20)
    for _ in range(20):
        if not app_running():
            if os.name != "nt":
                cfg["_last_shutdown"] = {"方式": "macOS app quit", "全部结束": True}
            return True
        time.sleep(.5)
    raise RuntimeError("ChatGPT 未正常退出。请保存工作并完全退出本机应用，再点击同步。")


def reopen(cfg):
    if os.name == "nt":
        subprocess.Popen(["explorer.exe", cfg["app_launch"]], creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        subprocess.Popen(["open", "-a", "ChatGPT"])


def require_offline(cfg):
    if cfg.get("test_mode"):
        return
    if app_running():
        raise RuntimeError("本机应用仍在运行，未写入聊天数据库")
    if os.name != "nt":
        for name in ["state_5.sqlite", "thread_history_1.sqlite"]:
            if subprocess.run(["lsof", str(pathlib.Path(cfg["codex_home"]) / name)], capture_output=True).returncode == 0:
                raise RuntimeError("Codex 终端仍在使用聊天库，请退出后重试")
    else:
        # 独立终端也可能持有数据库，不能仅检查桌面窗口。
        listing = run(["tasklist", "/FI", "IMAGENAME eq codex.exe", "/FO", "CSV", "/NH"])
        if '"codex.exe"' in listing:
            raise RuntimeError("仍有 Codex 后台或终端进程，请完全退出后重试")


def checkpoint_single_repo(path, message):
    """保存单个仓库；空工作目录也建立首次提交，让上级仓库可以记录它。"""
    path = pathlib.Path(path)
    if not (path / ".git").exists():
        git(path, "init", "-b", "codex/course-sync")
    git(path, "add", "-A")
    try:
        git(path, "rev-parse", "--verify", "HEAD^{commit}")
        has_head = True
    except RuntimeError:
        has_head = False
    if not has_head:
        git(path, "commit", "--allow-empty", "-m", message)
    elif git(path, "status", "--porcelain"):
        git(path, "commit", "-m", message)


def checkpoint_repo(path, message):
    """先保存各层嵌套仓库，再保存上级仓库，保留原有分支和导入历史。"""
    path = pathlib.Path(path)
    nested = []
    for current, dirs, names in os.walk(path, onerror=walk_error):
        candidate = pathlib.Path(current)
        if candidate != path and (".git" in dirs or ".git" in names):
            nested.append(candidate)
        dirs[:] = [name for name in dirs if name != ".git"]
    for repo in sorted(nested, key=lambda p: (-len(p.parts), str(p))):
        checkpoint_single_repo(repo, message)
    checkpoint_single_repo(path, message)


def checkpoint_project(cfg, key, message):
    path = project_path(cfg, key)
    if cfg.get("preserve_repository_kind") and not (path / ".git").exists():
        return
    checkpoint_repo(path, message)


def import_bundle(repo, path, peer):
    if not (repo / ".git").exists():
        git(repo, "init", "-b", "codex/course-sync")
    # Codex 的快照引用可能很长；用稳定短名保存，避免 Windows 路径长度限制。
    refs = [line.split(" ", 1)[1] for line in run(["git", "bundle", "list-heads", path]).splitlines()]
    mapping = {ref: "refs/course-sync/" + peer + "/" + digest(ref.encode())[:24] for ref in refs}
    if mapping:
        git(repo, "fetch", "--atomic", str(path), *["+" + ref + ":" + dest for ref, dest in mapping.items()])
    return mapping


def import_git_pack(repo, pack, unpack, course_root, peer):
    """复用本机相同文件，验证完整历史后原子写入来源引用。"""
    if not (repo / ".git").exists():
        repo.mkdir(parents=True, exist_ok=True)
        git(repo, "init", "-b", "codex/course-sync", "--object-format=" + pack["object_format"])
    if git(repo, "rev-parse", "--show-object-format") != pack["object_format"]:
        raise RuntimeError("两端 Git 对象格式不兼容，停止导入：" + pack["repo"])
    for oid, rel in pack.get("prime_files", {}).items():
        path = project_file(course_root, rel) if isinstance(course_root, dict) else safe(course_root, rel)
        written = git(repo, "hash-object", "--no-filters", "-w", str(path))
        if written != oid:
            raise RuntimeError("复用的本机文件发生变化，停止 Git 历史导入：" + rel)
    if pack.get("file"):
        path = safe(unpack / "packs", pack["file"])
        if file_hash(path) != pack["sha"]:
            raise RuntimeError("Git 对象包校验失败")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        with path.open("rb") as stream:
            result = subprocess.run(["git", "-C", str(repo), "index-pack", "--stdin"], stdin=stream, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
        if result.returncode:
            raise RuntimeError("Git 对象导入失败：" + result.stderr.decode("utf-8", "replace")[-2000:])
    required = pack.get("required", [])
    if required:
        checked = git(repo, "cat-file", "--batch-check=%(objectname) %(objecttype)", input="\n".join(required) + "\n")
        if any(line.endswith(" missing") for line in checked.splitlines()) or len(checked.splitlines()) != len(required):
            raise RuntimeError("Git 历史对象不完整，尚未更新来源引用")
    mapping = {}
    commands = ["start"]
    for ref, oid in pack["refs"].items():
        if not ref.startswith("refs/") or "\n" in ref or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
            raise RuntimeError("Git 引用格式不合法")
        target = "refs/course-sync/" + peer + "/" + digest(ref.encode())[:24]
        mapping[ref] = target
        commands.append("update " + target + " " + oid)
    commands.extend(["prepare", "commit"])
    if mapping:
        # 使用字节输入，防止 Windows 把事务指令的换行改成 CRLF。
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        result = subprocess.run(["git", "-C", str(repo), "update-ref", "--stdin"], input=("\n".join(commands) + "\n").encode(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
        if result.returncode:
            raise RuntimeError("Git 引用事务失败：" + result.stderr.decode("utf-8", "replace")[-2000:])
    return mapping


def remove_sync_tree(path):
    """清除已核定的临时目录，兼容 Windows Git 对象的只读属性。"""
    def retry_readonly(operation, target, error):
        if not isinstance(error[1], PermissionError):
            raise error[1]
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        operation(target)
    shutil.rmtree(path, onerror=retry_readonly)


def discard_completed_backup(cfg, backup):
    """只有安装完成的本机事务才能自动清理，未完成事务继续保留用于回退。"""
    backup = pathlib.Path(backup)
    base = pathlib.Path(cfg.get("state_dir", str(APP))) / "backups"
    if backup.is_symlink() or backup.resolve().parent != base.resolve():
        raise RuntimeError("备份目录不属于本机同步事务，停止清理")
    if read_json(backup / "transaction.json", {}).get("status") != "complete":
        raise RuntimeError("同步尚未完成，保留回退备份")
    remove_sync_tree(backup)


def apply_import(cfg, plan, package, expected_local):
    if needs_database(plan):
        require_offline(cfg)
    current = inventory(cfg, include_git=False)
    if current["files"] != expected_local["files"] or (needs_database(plan) and current["chats"] != expected_local["chats"]):
        raise RuntimeError("预览后本机文件或聊天已改变，请重新同步")
    state_dir = pathlib.Path(cfg.get("state_dir", str(APP)))
    backup = state_dir / "backups" / (datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    backup.mkdir(parents=True)
    preparation_started = time.perf_counter()
    metadata = prepare_import(cfg, plan, package, backup)
    preparation_seconds = time.perf_counter() - preparation_started
    database_bytes = sum(path.stat().st_size for directory in [backup / "before", backup / "after"] for path in directory.glob("*.sqlite"))
    git_started = time.perf_counter()
    git(backup, "init", "-b", "codex/sync-backup")
    git(backup, "add", "-f", "before", "after", "incoming", "plan.json", "retired-logs.json")
    if needs_database(plan):
        git(backup, "add", "backup-scope.json", "metadata-changes.json")
    git(backup, "commit", "-m", "同步前保存聊天原件与完整待安装结果")
    backup_git_seconds = time.perf_counter() - git_started
    root = pathlib.Path(cfg["course_root"])
    touched = {x["source"].split("/")[0] for x in plan["files"]}
    touched.update(action["course"] for action in plan.get("project_actions", []))
    # Git 历史也可能首次带来子仓库；导入前后都要建立可被上级记录的本机提交。
    touched.update(bundle["repo"].split("/")[0] for bundle in metadata["bundles"])
    touched.update(pack["repo"].split("/")[0] for pack in metadata.get("packs", []))
    touched = sorted(touched)
    for course in touched:
        project_path(cfg, course).mkdir(parents=True, exist_ok=True)
        checkpoint_project(cfg, course, "项目同步前保存当前工作目录")
    for action in plan["files"]:
        source = safe(backup / "incoming/files", action["source"])
        if file_hash(source) != action["sha"]:
            raise RuntimeError("文件传输校验失败")
        dest = project_file(cfg, action["destination"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            copy = safe(backup / "before/files", action["destination"])
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, copy)
    git(backup, "add", "-f", "before")
    if git(backup, "status", "--porcelain", "before"):
        git(backup, "commit", "-m", "备份本次会更新的原有课程文件")
    for action in plan["files"]:
        dest = project_file(cfg, action["destination"])
        expected = expected_local["files"].get(action["destination"])
        if action["action"] == "conflict":
            if dest.exists() and file_hash(dest) != action["sha"]:
                raise RuntimeError("冲突备份位置已有不同内容，停止安装")
        elif (expected is None and dest.exists()) or (expected is not None and (not dest.exists() or file_hash(dest) != expected["sha"])):
            raise RuntimeError("备份期间本机文件发生变化，请停止编辑后重试")
    save_json(backup / "transaction.json", {"status": "prepared", "plan": plan})
    git(backup, "add", "transaction.json")
    git(backup, "commit", "-m", "记录可恢复的待安装事务")
    installed_logs = []
    retired_logs = []
    database_started = False
    try:
        if needs_database(plan):
            require_offline(cfg)
        for action in plan["files"]:
            source = safe(backup / "incoming/files", action["source"])
            dest = project_file(cfg, action["destination"])
            tmp = dest.with_name(dest.name + ".course-sync-pending")
            shutil.copy2(source, tmp)
            os.replace(tmp, dest)
        if cfg.get("test_mode") and cfg.get("test_failure_stage") == "files":
            raise RuntimeError("隔离测试主动触发文件阶段回滚")
        for bundle in metadata["bundles"]:
            repo = project_file(cfg, bundle["repo"])
            mapping = import_bundle(repo, backup / "incoming/bundles" / bundle["file"], plan["peer"])
            save_json(backup / ("Git引用-" + digest(bundle["repo"].encode())[:12] + ".json"), mapping)
        for pack in metadata.get("packs", []):
            repo = project_file(cfg, pack["repo"])
            mapping = import_git_pack(repo, pack, backup / "incoming", cfg, plan["peer"])
            save_json(backup / ("Git引用-" + digest(pack["repo"].encode())[:12] + ".json"), mapping)
        live = pathlib.Path(cfg["codex_home"])
        asset_names = {x["name"] for x in metadata["assets"].values()} | set(metadata.get("path_assets", {}).values())
        for name in asset_names:
            dest = safe(live / "migrated-assets/project-sync", name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            source = safe(backup / "incoming/assets", name)
            if not dest.exists():
                shutil.copy2(source, dest)
            assert file_hash(dest) == file_hash(source)
        if needs_database(plan):
            for path in (backup / "after/sessions").rglob("*.jsonl"):
                destination = live / path.relative_to(backup / "after")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise RuntimeError("本次聊天日志目标已存在，停止以避免覆盖")
                installed_logs.append(destination)
                shutil.copy2(path, destination)
            if cfg.get("test_mode") and cfg.get("test_failure_stage") == "logs":
                raise RuntimeError("隔离测试主动触发日志阶段回滚")
            for entry in read_json(backup / "retired-logs.json", []):
                old_path = safe(live, entry["path"])
                saved = safe(backup / "before/retired-logs", entry["path"])
                if file_hash(old_path) != entry["sha"] or file_hash(saved) != entry["sha"]:
                    raise RuntimeError("备份后原聊天日志发生变化，停止安装")
                os.replace(old_path, saved)
                retired_logs.append(entry)
            database_started = True
            install_selected_databases(cfg, backup, "after")
            install_chat_metadata(cfg, backup, "after")
            if cfg.get("test_mode") and cfg.get("test_failure_stage") == "databases":
                raise RuntimeError("隔离测试主动触发数据库阶段回滚")
        for course in touched:
            checkpoint_project(cfg, course, "接收项目更新并保留冲突版本")
        save_json(backup / "transaction.json", {"status": "complete", "plan": plan})
        git(backup, "add", "-A", "-f")
        git(backup, "commit", "-m", "记录同步完成与回滚数据")
    except Exception:
        # 已经写入的文件与聊天恢复到本次安装前；新增文件保留到隔离目录。
        recovery = backup / "failed-new-files"
        for action in plan["files"]:
            dest = project_file(cfg, action["destination"])
            old = safe(backup / "before/files", action["destination"])
            if old.exists():
                shutil.copy2(old, dest)
            elif dest.exists():
                target = safe(recovery, action["destination"])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dest), str(target))
        for path in installed_logs:
            if path.exists():
                target = recovery / "sessions" / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(target))
        for entry in retired_logs:
            target = safe(live, entry["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(safe(backup / "before/retired-logs", entry["path"]), target)
        if database_started:
            restore_chat_backup(cfg, backup)
        save_json(backup / "transaction.json", {"status": "rolled_back", "plan": plan})
        git(backup, "add", "-A", "-f")
        git(backup, "commit", "-m", "记录失败后完整回滚并隔离新增文件")
        raise
    return {"backup": str(backup), "commit": git(backup, "rev-parse", "--short", "HEAD"), "files": len(plan["files"]), "chats": len(plan["chats"]),
            "精确备份": {"聊天数": len(plan["chats"]), "数据库副本字节数": database_bytes,
                         "准备耗时秒": round(preparation_seconds, 3), "初次Git提交耗时秒": round(backup_git_seconds, 3)}}


def acknowledge_received_files(cfg, export_path, received):
    """接收成功后记录发送端的共同基线，支持下一次反向更新。"""
    if not received:
        return 0
    metadata = read_json(export_path.parent / "metadata.json")
    sent = metadata.get("sent_files")
    if sent is None:
        with tarfile.open(export_path) as archive:
            names = {member.name[6:] for member in archive.getmembers() if member.isfile() and member.name.startswith("files/")}
        sent = {name: metadata["source"]["files"][name]["sha"] for name in names}
    for relative, sha in received.items():
        if sent.get(relative) != sha or relative.split("/", 1)[0] not in project_keys(cfg):
            raise RuntimeError("接收确认与原发送文件不一致，停止基线更新")
    lock = APP / "lock"
    try:
        lock.mkdir()
    except FileExistsError:
        return 0
    try:
        save_json(lock / "owner.json", {"pid": os.getpid(), "time": time.time()})
        baseline = read_json(APP / "baseline.json", {"files": {}})
        confirmed = 0
        for relative, sha in received.items():
            path = project_file(cfg, relative)
            if path.is_file() and file_hash(path) == sha:
                baseline.setdefault("files", {})[relative] = {"local": sha, "remote": sha}
                confirmed += 1
        if confirmed:
            save_json(APP / "baseline.json", baseline)
            git(APP, "add", "-f", "baseline.json")
            if git(APP, "status", "--porcelain", "baseline.json"):
                git(APP, "commit", "--only", "-m", "确认对端已接收文件并保存共同基线", "--", "baseline.json")
        return confirmed
    finally:
        owner = lock / "owner.json"
        if owner.exists():
            owner.unlink()
        lock.rmdir()


def rpc(request):
    cfg = config()
    cfg["aliases"] = read_json(APP / "aliases.json", {}).get("paths", {})
    cfg["image_fallbacks"] = read_json(APP / "aliases.json", {}).get("images", {})
    cfg["lineage"] = read_json(APP / "lineage.json", {})
    op = request["op"]
    if op == "inventory":
        return inventory(cfg, include_git=request.get("include_git", False))
    if op == "export":
        if request.get("protocol") != SYNC_VERSION:
            raise RuntimeError("两端同步工具需要使用相同版本，请重新打开两端桌面入口")
        return export_package(cfg, request["request"])
    if op == "export_prefix":
        path = validate_export_path(request["export"])
        length = int(request["length"])
        if length < 0 or length > path.stat().st_size:
            raise RuntimeError("断点长度不合法")
        sha = hashlib.sha256()
        with path.open("rb") as stream:
            remaining = length
            while remaining:
                block = stream.read(min(8388608, remaining))
                if not block:
                    raise RuntimeError("远端包不完整")
                sha.update(block)
                remaining -= len(block)
        return {"sha": sha.hexdigest()}
    if op == "release_export":
        if request.get("protocol") != SYNC_VERSION:
            raise RuntimeError("两端同步工具版本不同，保留发送缓存")
        path = validate_export_path(request["export"])
        if path.parent.parent != (APP / "work").resolve() or path.parent.is_symlink():
            raise RuntimeError("发送缓存目录不合法，停止清理")
        confirmed = acknowledge_received_files(cfg, path, request.get("received_files", {}))
        remove_sync_tree(path.parent)
        return {"released": True, "confirmed_files": confirmed}
    raise RuntimeError("不支持的请求")


def validate_export_path(descriptor):
    path = pathlib.Path(descriptor["path"]).resolve()
    if not path.is_relative_to((APP / "work").resolve()) or path.name != "export.tar.gz" or path.stat().st_size != descriptor["size"]:
        raise RuntimeError("更新包的位置或大小发生变化，请重新检查")
    return path


if __name__ == "__main__":
    try:
        print(json.dumps({"ok": True, "result": rpc(json.load(sys.stdin))}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
