"""恢复因断电或进程中断而未完成的安装；恢复前再保存当前现场。"""
import pathlib
import shutil
import sys

import controller
import sync_core as core


def recover(cfg, backup):
    backup = pathlib.Path(backup).resolve()
    base = pathlib.Path(cfg.get("state_dir",str(core.APP))).resolve()/"backups"
    if backup.parent != base:
        raise RuntimeError("只能恢复本机同步目录中的单次备份")
    transaction = core.read_json(backup/"transaction.json",{})
    if transaction.get("status") != "prepared":
        raise RuntimeError("只恢复未完成的安装，已完成的备份请先核对后续改动")
    plan=transaction["plan"]
    if core.needs_database(plan):
        core.require_offline(cfg)
    live=pathlib.Path(cfg["codex_home"])
    root=pathlib.Path(cfg["course_root"])
    saved=backup/"中断后现场"
    saved.mkdir(exist_ok=True)
    names=["state_5.sqlite","thread_history_1.sqlite",".codex-global-state.json","session_index.jsonl"]
    selected_backup = (backup / "backup-scope.json").exists()
    chat_files = names+[n+s for n in names[:2] for s in ["-wal","-shm"]] if core.needs_database(plan) and not selected_backup else []
    for name in chat_files:
        p=live/name
        if p.exists():shutil.copy2(p,saved/name)
    for action in plan["files"]:
        p=core.project_file(cfg,action["destination"])
        if p.exists():
            target=core.safe(saved/"files",action["destination"])
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
    logs=live/"sessions/course-sync"/backup.name
    if logs.exists():shutil.copytree(logs,saved/"logs",dirs_exist_ok=True)
    core.git(backup,"add","-A","-f");core.git(backup,"commit","-m","恢复前保存意外中断的当前现场")
    # 原件已提交 Git，新增内容移动到备份内隔离，避免再次被应用扫描。
    for action in plan["files"]:
        p=core.project_file(cfg,action["destination"])
        old=core.safe(backup/"before/files",action["destination"])
        if old.exists():shutil.copy2(old,p)
        elif p.exists():
            target=core.safe(saved/"新增文件隔离",action["destination"])
            target.parent.mkdir(parents=True,exist_ok=True);p.rename(target)
    if logs.exists():logs.rename(saved/"新增日志隔离")
    for entry in core.read_json(backup/"retired-logs.json", []):
        original = core.safe(backup/"before/retired-logs", entry["path"])
        target = core.safe(live, entry["path"])
        if original.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and core.file_hash(target) != entry["sha"]:
                copy = core.safe(saved/"恢复前历史日志", entry["path"])
                copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, copy)
            shutil.copy2(original, target)
    if core.needs_database(plan):
        if not selected_backup:
            for name in names[:2]:
                for suffix in ["-wal","-shm"]:
                    p=live/(name+suffix)
                    if p.exists():p.rename(saved/(p.name+".isolated"))
        core.restore_chat_backup(cfg, backup)
    core.save_json(backup/"transaction.json",dict(transaction,status="rolled_back"))
    core.git(backup,"add","-A","-f");core.git(backup,"commit","-m","完成中断事务恢复")
    return str(backup)


if __name__=="__main__":
    print("已恢复："+recover(controller.load_config(),sys.argv[1]))
