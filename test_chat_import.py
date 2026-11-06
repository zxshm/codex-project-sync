"""覆盖原项目归属、侧边聊天排除和重复日志回滚的回归检查。"""
import contextlib
import json
import pathlib
import sqlite3
import tarfile
import tempfile
import unittest
from unittest import mock

import sync_core as core
import controller
import recover


class MainChatImport(unittest.TestCase):
    def test_live_refresh_selects_one_existing_aligned_chat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            cfg, _, _, _, _ = self.fixture(root)
            local = core.inventory(cfg, include_git=False)
            remote = dict(local, device="Mac")
            plan = core.plan_sync(local, remote)
            plan["git_changed"] = False
            with mock.patch.object(core, "APP", root), mock.patch.object(controller, "load_config", return_value=cfg), \
                 mock.patch.object(controller, "inspect", return_value=(local, remote, plan)):
                result = controller.execute(preview=True, refresh_threads=["original", "original"])
            self.assertEqual(len(result["plan"]["chats"]), 1)
            self.assertEqual(result["plan"]["chats"][0]["destination"], "original")

    def test_live_refresh_rejects_unknown_chat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            cfg, _, _, _, _ = self.fixture(root)
            local = core.inventory(cfg, include_git=False)
            remote = dict(local, device="Mac")
            plan = core.plan_sync(local, remote)
            with mock.patch.object(core, "APP", root), mock.patch.object(controller, "load_config", return_value=cfg), \
                 mock.patch.object(controller, "inspect", return_value=(local, remote, plan)):
                with self.assertRaisesRegex(RuntimeError, "已对齐的主聊天"):
                    controller.execute(preview=True, refresh_threads=["unknown"])

    def add_unrelated_history(self, cfg):
        home = pathlib.Path(cfg["codex_home"])
        with contextlib.closing(sqlite3.connect(home / "state_5.sqlite")) as db:
            db.row_factory = sqlite3.Row
            row = dict(db.execute("select * from threads where id='original'").fetchone())
            row.update(id="unrelated", name="其他项目聊天")
            core.insert_row(db, "threads", row)
            db.commit()
        with contextlib.closing(sqlite3.connect(home / "thread_history_1.sqlite")) as db:
            for tid, body in [("original", "本机旧消息"), ("unrelated", "其他项目内容" * 300000)]:
                db.execute("insert into thread_turns values (?,?,0,'completed')", (tid, "old-turn"))
                db.execute("insert into thread_items values (?,?,0,?,'userMessage',?)", (tid, "old-turn", "old-item", json.dumps({"text": body})))
            db.commit()

    def test_backup_contains_only_selected_rows_and_changed_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            cfg, plan, package, _, _ = self.fixture(root)
            self.add_unrelated_history(cfg)
            home = pathlib.Path(cfg["codex_home"])
            with mock.patch.object(core, "snapshot", side_effect=AssertionError("聊天同步应只导出目标记录")):
                result = core.apply_import(cfg, plan, package, core.inventory(cfg, include_git=False))
            backup = pathlib.Path(result["backup"])
            for stage in ["before", "after"]:
                with contextlib.closing(core.ro(backup / stage / "state_5.sqlite")) as db:
                    self.assertEqual([row[0] for row in db.execute("select id from threads")], ["original"])
                with contextlib.closing(core.ro(backup / stage / "thread_history_1.sqlite")) as db:
                    self.assertEqual(db.execute("select count(*) from thread_items where thread_id='unrelated'").fetchone()[0], 0)
                self.assertLess((backup / stage / "thread_history_1.sqlite").stat().st_size, 1048576)
                self.assertFalse((backup / stage / ".codex-global-state.json").exists())
            self.assertFalse((backup / "before/replaced-logs").exists())
            with contextlib.closing(core.ro(home / "thread_history_1.sqlite")) as db:
                self.assertEqual(db.execute("select count(*) from thread_items where thread_id='unrelated'").fetchone()[0], 1)

    def test_partial_database_failure_restores_selected_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            cfg, plan, package, old_log, globals_ = self.fixture(root)
            self.add_unrelated_history(cfg)
            cfg["test_failure_stage"] = "state_database"
            home = pathlib.Path(cfg["codex_home"])
            with self.assertRaisesRegex(RuntimeError, "两库提交之间"):
                core.apply_import(cfg, plan, package, core.inventory(cfg, include_git=False))
            with contextlib.closing(core.ro(home / "state_5.sqlite")) as db:
                self.assertEqual(db.execute("select rollout_path from threads where id='original'").fetchone()[0], str(old_log))
            with contextlib.closing(core.ro(home / "thread_history_1.sqlite")) as db:
                self.assertEqual(db.execute("select count(*) from thread_items").fetchone()[0], 2)
            self.assertEqual(core.read_json(home / ".codex-global-state.json"), globals_)

    def test_new_chat_rollback_keeps_existing_chats(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg, plan, package, old_log, globals_ = self.fixture(pathlib.Path(temporary))
            plan["chats"][0].update(destination="forked", action="fork")
            cfg["test_failure_stage"] = "databases"
            with self.assertRaisesRegex(RuntimeError, "数据库阶段回滚"):
                core.apply_import(cfg, plan, package, core.inventory(cfg, include_git=False))
            home = pathlib.Path(cfg["codex_home"])
            with contextlib.closing(core.ro(home / "state_5.sqlite")) as db:
                self.assertEqual([row[0] for row in db.execute("select id from threads")], ["original"])
            self.assertEqual(core.read_json(home / ".codex-global-state.json"), globals_)
            self.assertTrue(old_log.is_file())

    def test_selected_backup_reads_committed_wal_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg, plan, package, _, _ = self.fixture(pathlib.Path(temporary))
            home = pathlib.Path(cfg["codex_home"])
            with contextlib.closing(sqlite3.connect(home / "thread_history_1.sqlite")) as writer:
                writer.execute("pragma journal_mode=wal")
                writer.execute("insert into thread_turns values ('original','wal-turn',0,'completed')")
                writer.commit()
                result = core.apply_import(cfg, plan, package, core.inventory(cfg, include_git=False))
                with contextlib.closing(core.ro(pathlib.Path(result["backup"]) / "before/thread_history_1.sqlite")) as saved:
                    self.assertEqual(saved.execute("select turn_id from thread_turns").fetchone()[0], "wal-turn")

    def test_interrupted_recovery_preserves_other_chat_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            cfg, plan, package, old_log, _ = self.fixture(root)
            self.add_unrelated_history(cfg)
            home = pathlib.Path(cfg["codex_home"])
            backup = pathlib.Path(cfg["state_dir"]) / "backups/模拟中断"
            backup.mkdir(parents=True)
            core.prepare_import(cfg, plan, package, backup)
            core.git(backup, "init", "-b", "codex/test")
            core.save_json(backup / "transaction.json", {"status": "prepared", "plan": plan})
            cfg["test_failure_stage"] = "state_database"
            with self.assertRaisesRegex(RuntimeError, "两库提交之间"):
                core.install_selected_databases(cfg, backup, "after")
            with contextlib.closing(sqlite3.connect(home / "state_5.sqlite")) as db:
                db.execute("update threads set name='其他聊天后续更名' where id='unrelated'")
                db.commit()
            globals_ = core.read_json(home / ".codex-global-state.json")
            globals_["不相关的新设置"] = {"保留": True}
            core.save_json(home / ".codex-global-state.json", globals_)
            recover.recover(cfg, backup)
            core.restore_chat_backup(cfg, backup)
            with contextlib.closing(core.ro(home / "state_5.sqlite")) as db:
                self.assertEqual(db.execute("select name from threads where id='unrelated'").fetchone()[0], "其他聊天后续更名")
                self.assertEqual(db.execute("select rollout_path from threads where id='original'").fetchone()[0], str(old_log))
            self.assertEqual(core.read_json(home / ".codex-global-state.json")["不相关的新设置"], {"保留": True})
            self.assertFalse((backup / "中断后现场/thread_history_1.sqlite").exists())

    def test_controller_cleans_backup_and_both_transfer_caches_after_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            core.git(root, "init", "-b", "codex/test")
            cfg, plan, _, _, _ = self.fixture(root)
            cfg["device"] = "Windows"
            cfg["peer"] = {"device": "Mac"}
            target = pathlib.Path(cfg["course_root"]) / "通信原理/笔记.md"
            target.write_text("原笔记", encoding="utf-8")
            incoming = root / "新笔记.md"
            incoming.write_text("更新笔记", encoding="utf-8")
            plan.update(chats=[], same_chats=1, local_ahead_chats=0, git_changed=False)
            plan["files"] = [{"source": "通信原理/笔记.md", "destination": "通信原理/笔记.md", "sha": core.file_hash(incoming), "action": "update"}]
            manifest = root / "文件清单.json"
            core.save_json(manifest, {"source": {"device": "Mac"}, "threads": {}, "assets": {}, "bundles": []})
            package = root / "文件更新.tar"
            with tarfile.open(package, "w") as archive:
                archive.add(manifest, arcname="metadata.json")
                archive.add(incoming, arcname="files/通信原理/笔记.md")
            local = core.inventory(cfg, include_git=False)
            remote = {"device": "Mac", "chats": {}, "files": {plan["files"][0]["source"]: {"sha": core.file_hash(incoming)}}}
            descriptor = {"path": "远端缓存", "size": package.stat().st_size, "sha": core.file_hash(package)}
            def receive(_cfg, _descriptor, destination, _progress):
                destination.write_bytes(package.read_bytes())
            with mock.patch.object(core, "APP", root), mock.patch.object(controller, "load_config", return_value=cfg), \
                 mock.patch.object(controller, "inspect", return_value=(local, remote, plan)), \
                 mock.patch.object(controller, "peer_rpc", return_value=descriptor) as rpc, \
                 mock.patch.object(controller, "download", side_effect=receive), \
                 mock.patch.object(core, "close_local_app", side_effect=AssertionError("文件同步不应退出应用")):
                result = controller.execute()
            self.assertTrue(result.get("临时备份已清理"), result)
            self.assertNotIn("backup", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "更新笔记")
            self.assertEqual(list((pathlib.Path(cfg["state_dir"]) / "backups").iterdir()), [])
            self.assertEqual(list((root / "work").iterdir()), [])
            self.assertEqual(rpc.call_args.args[1]["op"], "release_export")

    def test_only_completed_local_backups_can_be_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            cfg = {"state_dir": str(root)}
            backup = root / "backups/本次同步"
            for status in ["prepared", "rolled_back", "complete"]:
                core.save_json(backup / "transaction.json", {"status": status})
                if status == "complete":
                    core.discard_completed_backup(cfg, backup)
                    self.assertFalse(backup.exists())
                else:
                    with self.assertRaisesRegex(RuntimeError, "尚未完成"):
                        core.discard_completed_backup(cfg, backup)
                    self.assertTrue(backup.is_dir())
            outside = root / "课程原件"
            core.save_json(outside / "transaction.json", {"status": "complete"})
            with self.assertRaisesRegex(RuntimeError, "不属于"):
                core.discard_completed_backup(cfg, outside)
            self.assertTrue(outside.is_dir())

    def test_file_only_import_preserves_chat_databases_and_works_with_app_open(self):
        for failure in [None, "files"]:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = pathlib.Path(temporary)
                cfg, plan, _, _, _ = self.fixture(root)
                cfg["test_failure_stage"] = failure
                home = pathlib.Path(cfg["codex_home"])
                original = {name: (home / name).read_bytes() for name in ["state_5.sqlite", "thread_history_1.sqlite", ".codex-global-state.json"]}
                target = pathlib.Path(cfg["course_root"]) / "通信原理/笔记.md"
                target.write_text("原笔记", encoding="utf-8")
                incoming = root / "新笔记.md"
                incoming.write_text("更新笔记", encoding="utf-8")
                plan["chats"] = []
                plan["files"] = [{"source": "通信原理/笔记.md", "destination": "通信原理/笔记.md", "sha": core.file_hash(incoming), "action": "update"}]
                manifest = root / "文件清单.json"
                core.save_json(manifest, {"source": {"device": "Mac"}, "threads": {}, "assets": {}, "bundles": []})
                package = root / "文件更新.tar"
                with tarfile.open(package, "w") as archive:
                    archive.add(manifest, arcname="metadata.json")
                    archive.add(incoming, arcname="files/通信原理/笔记.md")
                expected = core.inventory(cfg, include_git=False)
                with mock.patch.object(core, "require_offline", side_effect=AssertionError("文件同步无需退出聊天应用")), mock.patch.object(core, "snapshot", side_effect=AssertionError("文件同步无需复制聊天库")):
                    if failure:
                        with self.assertRaisesRegex(RuntimeError, "文件阶段回滚"):
                            core.apply_import(cfg, plan, package, expected)
                    else:
                        core.apply_import(cfg, plan, package, expected)
                self.assertEqual(target.read_text(encoding="utf-8"), "原笔记" if failure else "更新笔记")
                for name, data in original.items():
                    self.assertEqual((home / name).read_bytes(), data)

    def fixture(self, root, side_chat=False):
        home = root / "codex"
        home.mkdir()
        courses = root / "课程"
        for course in core.COURSES:
            (courses / course).mkdir(parents=True)
        cwd = str(courses / "通信原理")
        old_log = home / "sessions/旧目录/rollout-original.jsonl"
        old_log.parent.mkdir(parents=True)
        old_log.write_text(json.dumps({"type": "session_meta", "payload": {"id": "original", "cwd": cwd}}) + "\n", encoding="utf-8")
        thread = {"id": "original", "cwd": cwd, "source": "vscode", "thread_source": "user",
                  "name": "本机主聊天名称", "title": "原有学习内容", "created_at": 1700000000,
                  "updated_at": 1700000001, "archived": 0, "archived_at": None,
                  "rollout_path": str(old_log), "model_provider": "openai", "project_id": "backend-project",
                  "thread_section_id": None, "section_position": None, "section_entered_at_ms": None,
                  "attached_thread_parent_id": None}
        with contextlib.closing(sqlite3.connect(home / "state_5.sqlite")) as db:
            columns = ",".join('"' + key + '" ' + ("text primary key" if key == "id" else "integer" if key in ["created_at", "updated_at", "archived", "archived_at", "section_position", "section_entered_at_ms"] else "text") for key in thread)
            db.execute("create table threads (" + columns + ")")
            db.execute("create table projects (id text,name text,metadata text,position integer,created_at_ms integer,updated_at_ms integer)")
            db.execute("create table project_roots (project_id text,position integer,path text)")
            db.execute("insert into projects values ('backend-project','通信原理','{}',0,0,0)")
            db.execute("insert into project_roots values ('backend-project',0,?)", (cwd,))
            for table in core.STATE_TABLES:
                db.execute('create table "' + table + '" (thread_id text primary key)')
            core.insert_row(db, "threads", thread)
            db.commit()
        with contextlib.closing(sqlite3.connect(home / "thread_history_1.sqlite")) as db:
            db.execute("create table thread_turns (thread_id text,turn_id text,rollout_ordinal integer,status text,primary key(thread_id,turn_id))")
            db.execute("create table thread_items (thread_id text,turn_id text,rollout_ordinal integer,item_id text,item_type text,item_json text,primary key(thread_id,item_id))")
            db.execute("create table thread_history_projection_state (thread_id text primary key,next_rollout_byte_offset integer,next_rollout_ordinal integer)")
            db.execute("create table thread_realtime_items (thread_id text primary key)")
            db.commit()
        globals_ = {"local-projects": {"sidebar-project": {"id": "sidebar-project", "name": "通信原理", "rootPaths": [cwd]}},
                    "thread-project-assignments": {"original": {"projectKind": "local", "projectId": "sidebar-project"}},
                    "app-server-project-id-by-legacy-project-id-by-host": {"local:" + str(home): {"sidebar-project": "backend-project"}}}
        core.save_json(home / ".codex-global-state.json", globals_)
        (home / "session_index.jsonl").write_text("", encoding="utf-8")
        cfg = {"device": "Windows", "course_root": str(courses), "codex_home": str(home), "home": str(root),
               "state_dir": str(root / "运行记录"), "canonical_roots": {}, "native_provider": "openai", "test_mode": True}
        source = {"device": "Mac", "course_root": "/来源/课程", "codex_home": "/来源/.codex", "home": "/来源"}
        source_thread = dict(thread, cwd="/来源/课程/通信原理", name="另一端名称", archived=1, archived_at=1700000002)
        data = {"type": "session_meta", "payload": {"id": "original", "cwd": source_thread["cwd"], "attached_thread": side_chat}}
        incoming = root / "来源.jsonl"
        incoming.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
        tables = {table: [] for table in core.STATE_TABLES + core.HISTORY_TABLES}
        metadata = {"version": core.SYNC_VERSION, "source": source, "threads": {"original": {"thread": source_thread, "tables": tables, "log_sha": core.file_hash(incoming)}},
                    "assets": {}, "path_assets": {}, "bundles": [], "packs": []}
        manifest = root / "metadata.json"
        core.save_json(manifest, metadata)
        package = root / "更新.tar"
        with tarfile.open(package, "w") as archive:
            archive.add(manifest, arcname="metadata.json")
            archive.add(incoming, arcname="logs/original.jsonl")
        plan = {"peer": "Mac", "files": [], "chats": [{"source": "original", "destination": "original", "course": "通信原理", "name": "学习", "action": "update"}]}
        return cfg, plan, package, old_log, globals_

    def test_main_chat_keeps_original_sidebar_project_and_local_visibility(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            cfg, plan, package, old_log, original_globals = self.fixture(root)
            result = core.apply_import(cfg, plan, package, core.inventory(cfg, include_git=False))
            home = pathlib.Path(cfg["codex_home"])
            globals_ = core.read_json(home / ".codex-global-state.json")
            self.assertEqual(globals_["local-projects"], original_globals["local-projects"])
            self.assertEqual(globals_["thread-project-assignments"]["original"]["projectId"], "sidebar-project")
            with contextlib.closing(core.ro(home / "state_5.sqlite")) as db:
                row = db.execute("select * from threads where id='original'").fetchone()
                self.assertEqual(row["project_id"], "backend-project")
                self.assertEqual(row["archived"], 0)
                self.assertEqual(row["name"], "本机主聊天名称")
            self.assertFalse(old_log.exists())
            self.assertEqual(len(list((home / "sessions").rglob("*original.jsonl*"))), 1)
            self.assertTrue((pathlib.Path(result["backup"]) / "before/retired-logs/sessions/旧目录/rollout-original.jsonl").is_file())

    def test_side_chat_marker_in_old_package_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            cfg, plan, package, old_log, _ = self.fixture(root, side_chat=True)
            backup = root / "验证"
            backup.mkdir()
            with self.assertRaisesRegex(RuntimeError, "侧边聊天"):
                core.prepare_import(cfg, plan, package, backup)
            self.assertTrue(old_log.is_file())

    def test_log_retirement_is_rolled_back_after_database_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            cfg, plan, package, old_log, _ = self.fixture(root)
            original = old_log.read_bytes()
            cfg["test_failure_stage"] = "databases"
            with self.assertRaisesRegex(RuntimeError, "数据库阶段回滚"):
                core.apply_import(cfg, plan, package, core.inventory(cfg, include_git=False))
            self.assertEqual(old_log.read_bytes(), original)
            self.assertEqual(len(list((pathlib.Path(cfg["codex_home"]) / "sessions").rglob("*original.jsonl*"))), 1)

    def test_windows_preview_link_prefix_does_not_create_a_fork(self):
        cfg = {"canonical_roots": {"C:/课程": "@course"}}
        left = {"id": "message", "type": "agentMessage", "text": "[查看预览](</C:/课程/图.png>)"}
        right = {"id": "message", "type": "agentMessage", "text": "[查看预览](<C:/课程/图.png>)"}
        self.assertEqual(core.canonical(left, cfg), core.canonical(right, cfg))

    def test_side_chat_with_missing_database_parent_is_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg, _, _, old_log, _ = self.fixture(pathlib.Path(temporary))
            record = json.loads(old_log.read_text(encoding="utf-8"))
            record["payload"]["attached_thread"] = True
            old_log.write_text(json.dumps(record) + "\n", encoding="utf-8")
            self.assertEqual(core.chat_snapshot(cfg)[0], {})



if __name__ == "__main__":
    unittest.main()
