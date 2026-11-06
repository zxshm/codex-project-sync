"""同步到本机的控制器；仅在用户操作时建立连接。"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import time
import uuid
import csv
import io
import tempfile
import tarfile

import sync_core as core


def load_config():
    cfg = core.config()
    aliases = core.read_json(core.APP / "aliases.json", {})
    cfg["aliases"] = aliases.get("paths", {})
    cfg["image_fallbacks"] = aliases.get("images", {})
    cfg["lineage"] = core.read_json(core.APP / "lineage.json", {})
    return cfg


def ssh_options(cfg):
    return ["-i", cfg["peer"]["key"], "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3", "-o", "StrictHostKeyChecking=yes",
            "-o", "UserKnownHostsFile=" + cfg["peer"]["known_hosts"]]


def peer_rpc(cfg, request):
    request = dict(request, protocol=core.SYNC_VERSION)
    peer = cfg["peer"]
    if peer["platform"] == "windows":
        def quote(value):
            return "'" + value.replace("'", "''") + "'"
        code = "$env:PYTHONIOENCODING='utf-8'; & " + quote(peer["python"]) + " " + quote(peer["script"])
        command = "powershell -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(code.encode("utf-16le")).decode()
    else:
        command = shlex.quote(peer["python"]) + " " + shlex.quote(peer["script"])
    try:
        output = core.run(["ssh", *ssh_options(cfg), peer["user"] + "@" + peer["address"], command], input=json.dumps(request, ensure_ascii=False), timeout=900)
    except RuntimeError as exc:
        if any(x in str(exc).lower() for x in ["connection timed out", "connection refused", "no route to host", "operation timed out"]):
            raise RuntimeError("暂时连接不到 " + peer["device"] + "。请让另一台电脑保持唤醒并连接 Tailscale，再点一次。此次未安装任何更新。") from exc
        raise
    response = json.loads(output)
    if not response["ok"]:
        raise RuntimeError(response["error"])
    return response["result"]


def download(cfg, remote, destination, progress=lambda _: None):
    """校验已有断点，通过 SFTP 接续传输，并持续报告字节数、速度和剩余时间。"""
    destination = pathlib.Path(destination)
    peer = cfg["peer"]
    path = remote["path"].replace("\\", "/")
    if peer.get("platform") == "windows" and len(path) > 2 and path[1:3] == ":/":
        path = "/" + path
    total = remote["size"]
    offset = destination.stat().st_size if destination.exists() else 0
    if offset > total:
        raise RuntimeError("已有断点大于更新包，停止以保护缓存")
    if offset:
        progress("正在校验已接收的断点，确认后继续传输…")
        prefix = peer_rpc(cfg, {"op": "export_prefix", "export": remote, "length": offset})
        if core.file_hash(destination) != prefix["sha"]:
            quarantine = destination.with_name(destination.name + ".invalid-" + uuid.uuid4().hex[:8])
            destination.rename(quarantine)
            offset = 0
    if offset < total:
        def quote(value):
            if "\n" in value or "\r" in value:
                raise RuntimeError("传输路径不合法")
            return '"' + value.replace('"', '\\"') + '"'
        command = "reget " + quote(path) + " " + quote(str(destination).replace("\\", "/")) + "\n"
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(["sftp", "-b", "-", *ssh_options(cfg), peer["user"] + "@" + peer["address"]],
                                       stdin=subprocess.PIPE, stdout=output, stderr=errors, **options)
            started = last_change = time.monotonic()
            previous = offset
            try:
                process.stdin.write(command.encode("utf-8")); process.stdin.close()
                while process.poll() is None:
                    size = destination.stat().st_size if destination.exists() else 0
                    now = time.monotonic()
                    if size != previous:
                        last_change = now
                        previous = size
                    speed = (size - offset) / max(now - started, .1)
                    eta = "，预计剩余 " + str(max(1, round((total - size) / speed))) + " 秒" if speed > 1024 else "，正在等待数据"
                    progress("正在接收：" + str(round(size / 1048576, 1)) + " / " + str(round(total / 1048576, 1)) +
                             " MB（" + str(round(size * 100 / max(total, 1), 1)) + "%），" + str(round(speed / 1048576, 2)) + " MB/秒" + eta)
                    if now - last_change > 90:
                        raise RuntimeError("传输 90 秒没有推进，已保存断点。确认两端在线后再次点击同步即可继续。")
                    time.sleep(.5)
                if process.returncode:
                    errors.seek(0)
                    raise RuntimeError("传输中断，已保存断点；再次点击同步可继续。\n" + errors.read().decode("utf-8", "replace")[-1500:])
            finally:
                if process.poll() is None:
                    process.kill(); process.wait(timeout=10)
    progress("更新包接收完成，正在校验 SHA-256…")
    if core.file_hash(destination) != remote["sha"]:
        raise RuntimeError("接收包的 SHA-256 校验失败，未导入")


def check_connection(cfg):
    """每次交接先触发设备发现，并独立保存线路状态，避免影响更新包缓存。"""
    result = {"mode": "unknown", "message": "暂时无法确认 Tailscale 线路，将按实际连接继续", "checked_at": time.time()}
    executable = shutil.which("tailscale")
    if not executable and os.name != "nt":
        candidate = pathlib.Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
        if candidate.is_file():
            executable = str(candidate)
    if executable:
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        try:
            subprocess.run([executable, "ping", "--c", "3", "--timeout", "1s", cfg["peer"]["address"]],
                           capture_output=True, timeout=6, **options)
            status = subprocess.run([executable, "status", "--json"], capture_output=True, timeout=4, **options)
            data = json.loads(status.stdout)
            peer = next((p for p in (data.get("Peer") or {}).values()
                         if cfg["peer"]["address"] in p.get("TailscaleIPs", [])), {})
            if peer.get("CurAddr"):
                result.update(mode="direct", address=peer["CurAddr"], message="Tailscale 已直连，两台设备直接传输")
            elif peer.get("Online"):
                result.update(mode="relay", relay=peer.get("Relay"),
                              message="Tailscale 当前使用中继，传输可能较慢；换一个网络后再次点击可重新尝试直连")
            else:
                result.update(mode="offline", message="另一台设备暂未在线，请检查唤醒状态和 Tailscale 连接")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    core.save_json(core.APP / "connection.json", result)
    return result


def inspect(cfg, progress=lambda _: None, include_git=False):
    progress("正在尝试建立设备直连并检查线路…")
    check_connection(cfg)
    progress("正在检查本机课程和聊天…")
    local = core.inventory(cfg, include_git=include_git)
    progress("正在连接另一台电脑并检查更新…")
    remote = peer_rpc(cfg, {"op": "inventory", "include_git": include_git})
    if remote.get("version") != core.SYNC_VERSION:
        raise RuntimeError("两端同步工具版本不同，请更新并重新打开两端桌面入口")
    baseline = core.read_json(core.APP / "baseline.json", {})
    bindings = core.read_json(core.APP / "bindings.json", {})
    plan = core.plan_sync(local, remote, baseline, bindings)
    plan["git_changed"] = include_git and any(set(tips) - set(local.get("git_reachable", {}).get(repo, []))
                                              for repo, tips in remote.get("git_ref_tips", {}).items())
    plan["include_git"] = bool(include_git)
    return local, remote, plan


def summarize(plan):
    return {"新增文件": sum(x["action"] == "add" for x in plan["files"]),
            "更新文件": sum(x["action"] == "update" for x in plan["files"]),
            "保留冲突文件": sum(x["action"] == "conflict" for x in plan["files"]),
            "新增对话": sum(x["action"] == "add" for x in plan["chats"]),
            "续接对话": sum(x["action"] == "update" for x in plan["chats"]),
            "另存对话分支": sum(x["action"] == "fork" for x in plan["chats"]),
            "本机领先对话": plan["local_ahead_chats"], "相同对话": plan["same_chats"],
            "Git历史更新": "有" if plan.get("git_changed") else "无"}


def pending_transactions():
    return [str(p.parent) for p in (core.APP / "backups").glob("*/transaction.json")
            if core.read_json(p, {}).get("status") not in ["complete", "rolled_back"]]


def owner_alive(pid):
    if not isinstance(pid, int) or pid < 1:
        return False
    if os.name == "nt":
        listing = core.run(["tasklist", "/FI", "PID eq " + str(pid), "/FO", "CSV", "/NH"])
        return any(len(row) > 1 and row[1] == str(pid) for row in csv.reader(io.StringIO(listing)))
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def save_baseline(local, remote):
    baseline = {"files": {rel: {"remote": data["sha"], "local": local["files"][rel]["sha"]}
                           for rel, data in remote["files"].items() if rel in local["files"]}}
    core.save_json(core.APP / "baseline.json", baseline)


def execute(preview=False, progress=lambda _: None, include_git=False, refresh_threads=()):
    paused = core.read_json(core.APP / "sync-paused.json", {})
    if paused:
        raise RuntimeError(paused.get("reason", "同步已暂停，等待聊天完整性检查完成"))
    cfg = load_config()
    lock = core.APP / "lock"
    if lock.exists():
        owner = core.read_json(lock / "owner.json", {})
        if owner and not owner_alive(owner.get("pid")):
            stale = core.APP / "stale-locks" / uuid.uuid4().hex
            stale.parent.mkdir(exist_ok=True)
            lock.rename(stale)
    try:
        lock.mkdir()
    except FileExistsError:
        raise RuntimeError("已有同步在进行；若上次意外中断，请先查看运行记录，勿同时启动两次")
    core.save_json(lock / "owner.json", {"pid": os.getpid(), "time": time.time()})
    try:
        pending = pending_transactions()
        if pending:
            raise RuntimeError("上次安装被意外中断，请先按使用说明恢复备份，勿继续覆盖：" + pending[0])
        local, remote, plan = inspect(cfg, progress, include_git=include_git)
        # 实测可显式选择已对齐的主聊天，验证重复接收同一聊天的完整安装流程。
        for tid in dict.fromkeys(refresh_threads):
            source = remote["chats"].get(tid)
            destination = local["chats"].get(tid)
            if source is None or destination is None or core.compare_history(destination, source) != "same":
                raise RuntimeError("原位重装测试只允许两端已对齐的主聊天")
            plan["chats"].append({"source": tid, "destination": tid, "action": "update", "name": source["name"],
                                  "course": source["course"], "binding": remote["device"] + "/" + tid})
        summary = summarize(plan)
        if preview:
            result = {"预览": True, "方向": remote["device"] + " → " + cfg["device"], "统计": summary, "plan": plan}
            core.save_json(core.APP / "latest.json", result)
            return result
        if not plan["files"] and not plan["chats"] and not plan["git_changed"]:
            save_baseline(local, remote)
            core.git(core.APP, "add", "-f", "baseline.json")
            if core.git(core.APP, "status", "--porcelain", "baseline.json"):
                core.git(core.APP, "commit", "-m", "保存已确认相同的课程文件基线")
            return {"完成": True, "说明": "没有需要接收的新内容。本机已有内容和保留的冲突版本保持原样。", "统计": summary}
        request = dict(plan, protocol=core.SYNC_VERSION, source_chats=remote["chats"], git_have=local.get("repos", {}),
                       include_snapshots=False, git_reachable=local.get("git_reachable", {}), git_file_blobs=local.get("git_file_blobs", {}))
        package = None
        resume = None
        exported = None
        for record in sorted((core.APP / "work").glob("*/package.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:5]:
            cached = core.read_json(record)
            candidate = record.parent / "received.tar"
            if cached.get("request") == request and candidate.exists() and core.file_hash(candidate) == cached["sha"]:
                package = candidate
                exported = cached.get("export")
                progress("复用已完整校验的更新包，无需重复传输…")
                break
            if cached.get("request") == request and cached.get("export"):
                resume = (record.parent, cached["export"])
                break
        if package is None:
            if resume:
                work, exported = resume
            else:
                progress("正在制作课程文件、聊天和附件的压缩更新包…" if not include_git else "正在制作压缩增量包，复用本机已有的 Git 文件对象…")
                exported = peer_rpc(cfg, {"op": "export", "request": request})
                work = core.APP / "work" / uuid.uuid4().hex
                work.mkdir(parents=True)
            package = work / "received.tar"
            core.save_json(work / "package.json", {"request": request, "sha": exported["sha"], "export": exported})
            download(cfg, exported, package, progress)
        try:
            if core.needs_database(plan):
                core.check_idle(cfg)
        except RuntimeError as exc:
            if "本机有聊天尚未结束" not in str(exc):
                raise
            return {"等待聊天结束": True, "说明": "更新包已下载并校验。本机仍有聊天正在回答，等回答结束后再次点击同步即可复用更新包完成安装。", "统计": summary}
        chat_backup_size = 0
        if core.needs_database(plan):
            home = pathlib.Path(cfg["codex_home"])
            with contextlib.closing(core.ro(home / "thread_history_1.sqlite")) as history, contextlib.closing(core.ro(home / "state_5.sqlite")) as state:
                for tid in {action["destination"] for action in plan["chats"]}:
                    size = history.execute("select coalesce(sum(length(cast(item_json as blob))),0) from thread_items where thread_id=?", (tid,)).fetchone()[0]
                    chat_backup_size += size * 8 + 1048576
                    row = state.execute("select rollout_path from threads where id=?", (tid,)).fetchone()
                    if row and core.resolve_log(row[0]).is_file():
                        chat_backup_size += core.resolve_log(row[0]).stat().st_size * 2
        with tarfile.open(package, "r:*") as archive:
            payload_size = sum(member.size for member in archive.getmembers() if member.isfile())
        needed = chat_backup_size + payload_size * 4 + 1024 ** 3
        if shutil.disk_usage(core.APP).free < needed:
            raise RuntimeError("本机磁盘空间不足以保存本次更新的临时备份，请至少预留 " + str(round(needed / 1024 ** 3, 1)) + " GB")
        was_running = False
        if core.needs_database(plan):
            progress("正在正常退出本机 ChatGPT，准备保存聊天备份…")
            was_running = core.close_local_app(cfg)
        shutdown = cfg.get("_last_shutdown")
        try:
            progress("正在备份并安装聊天；请勿手动打开本机 ChatGPT…" if plan["chats"] else "正在备份并合并课程文件…")
            result = core.apply_import(cfg, plan, package, local)
            metadata = core.read_json(pathlib.Path(result["backup"]) / "incoming/metadata.json")
            aliases = core.read_json(core.APP / "aliases.json", {"paths": {}, "images": {}})
            for raw, name in metadata.get("path_assets", {}).items():
                target = str(pathlib.Path(cfg["codex_home"]) / "migrated-assets/course-sync" / name)
                aliases["paths"][target] = metadata.get("canonical_assets", {}).get(raw, core.canonical(raw, cfg))
            core.save_json(core.APP / "aliases.json", aliases)
            cfg = load_config()
            refreshed = core.inventory(cfg, include_git=False)
            for action in plan["files"]:
                target = core.project_file(cfg, action["destination"])
                if not target.is_file() or core.file_hash(target) != action["sha"]:
                    raise RuntimeError("安装后的文件校验失败，已保留回退备份")
            for action in plan["chats"]:
                actual = refreshed["chats"].get(action["destination"])
                if actual is None or core.compare_history(actual, remote["chats"][action["source"]]) != "same":
                    raise RuntimeError("安装后的聊天校验失败，已保留回退备份")
            save_baseline(refreshed, remote)
            bindings = core.read_json(core.APP / "bindings.json", {})
            lineage = core.read_json(core.APP / "lineage.json", {})
            for chat in plan["chats"]:
                bindings[chat["binding"]] = chat["destination"]
                lineage[chat["destination"]] = remote["chats"][chat["source"]].get("lineage", chat["source"])
            core.save_json(core.APP / "bindings.json", bindings)
            core.save_json(core.APP / "lineage.json", lineage)
            core.git(core.APP, "add", "-f", "baseline.json", "bindings.json", "aliases.json", "lineage.json")
            if core.git(core.APP, "status", "--porcelain", "baseline.json", "bindings.json", "aliases.json", "lineage.json"):
                core.git(core.APP, "commit", "-m", "保存本次同步基线、分叉映射和附件路径")
            result.update({"完成": True, "方向": remote["device"] + " → " + cfg["device"], "统计": summary})
            if shutdown:
                result["应用退出检查"] = shutdown
            core.save_json(core.APP / "latest.json", result)
            progress("更新校验完成，正在清理本次临时备份和传输包…")
            try:
                core.discard_completed_backup(cfg, result["backup"])
                result.pop("backup")
                result["临时备份已清理"] = True
                if package.parent.resolve().parent != (core.APP / "work").resolve() or package.parent.is_symlink():
                    raise RuntimeError("接收缓存目录不合法，停止清理")
                core.remove_sync_tree(package.parent)
                if exported:
                    received = {action["source"]: action["sha"] for action in plan["files"] if action["source"] == action["destination"]}
                    acknowledgement = peer_rpc(cfg, {"op": "release_export", "export": exported, "received_files": received})
                    result["发送端基线确认文件数"] = acknowledgement.get("confirmed_files", 0)
            except Exception as exc:
                result["清理提示"] = "同步已完成，部分临时数据清理失败：" + str(exc)
            core.save_json(core.APP / "latest.json", result)
            return result
        finally:
            if was_running and not pending_transactions():
                core.reopen(cfg)
    finally:
        owner = lock / "owner.json"
        if owner.exists():
            owner.unlink()
        lock.rmdir()
