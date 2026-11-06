"""验证任意项目目录映射、空聊天项目迁移及身份边界。"""
import contextlib
import json
import io
import pathlib
import sqlite3
import tarfile
import tempfile
import unittest
from unittest import mock

import project_sync
import runtime
import sync_core as core
import test_chat_import
import transport


class ProjectMappings(unittest.TestCase):
    def test_sftp_first_transfer_and_resume_use_distinct_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            for direction, resume, expected in [("put", False, b"put "), ("put", True, b"reput "),
                                                ("get", False, b"get "), ("get", True, b"reget ")]:
                process = mock.Mock(returncode=0)
                process.stdin = io.BytesIO()
                process.stdin.close = mock.Mock()
                process.poll.return_value = 0
                connection = {"address": "127.0.0.1", "user": "tester", "port": 22, "platform": "macos"}
                with mock.patch.object(transport.subprocess, "Popen", return_value=process):
                    transport.transfer(connection, direction, root / "packet", "/tmp/packet", lambda _: None, resume=resume)
                self.assertTrue(process.stdin.getvalue().startswith(expected))

    def test_plain_project_checkpoint_keeps_repository_kind(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            cfg = {"projects": {"p_a": str(root)}, "preserve_repository_kind": True}
            with mock.patch.object(core, "checkpoint_repo") as checkpoint:
                core.checkpoint_project(cfg, "p_a", "同步前检查")
                checkpoint.assert_not_called()
            self.assertFalse((root / ".git").exists())

    def test_tagged_images_recover_when_other_turn_image_is_external(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            log = root / "history.jsonl"
            message = {"type": "response_item", "payload": {"role": "user", "content": [
                {"type": "input_text", "text": '<image name=[Image #1] path="C:\\临时\\原图.jpg">'},
                {"type": "input_image", "image_url": "data:image/jpeg;base64,aW1hZ2U="}]}}
            log.write_text(json.dumps(message), encoding="utf-8")
            tables = {"thread_turns": [{"turn_id": "t", "rollout_ordinal": 0}],
                      "thread_items": [{"turn_id": "t", "item_type": "userMessage", "rollout_ordinal": 0,
                                        "item_json": json.dumps({"content": [
                                            {"type": "localImage", "path": "C:\\临时\\原图.jpg"},
                                            {"type": "localImage", "path": "外部图片.png"}]})}]}
            _, images = core.recover_log_images(log, tables, root)
            self.assertEqual(images["C:\\临时\\原图.jpg"].read_bytes(), b"image")
            self.assertNotIn("外部图片.png", images)

    def fixture(self, root):
        return test_chat_import.MainChatImport().fixture(root)

    def test_same_leaf_directories_remain_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            cfg, _, _, _, _ = self.fixture(root)
            cfg["projects"] = {"p_a": str(root / "A/dataset"), "p_b": str(root / "B/dataset")}
            for key, path in cfg["projects"].items():
                directory = pathlib.Path(path)
                directory.mkdir(parents=True)
                (directory / "note.txt").write_text(key, encoding="utf-8")
            inventory = core.inventory(cfg, include_git=False)
            self.assertEqual(set(inventory["files"]), {"p_a/note.txt", "p_b/note.txt"})
            self.assertNotEqual(inventory["files"]["p_a/note.txt"]["sha"], inventory["files"]["p_b/note.txt"]["sha"])
            with self.assertRaises(RuntimeError):
                core.project_file(cfg, "p_a/../../escape")

    def test_arbitrary_windows_to_mac_roots_are_rewritten(self):
        cfg = {"projects": {"p_a": "/Users/target/Documents/另一个名字"}, "course_root": "/unused",
               "home": "/Users/target", "codex_home": "/Users/target/.codex", "native_provider": "openai"}
        source = {"project_roots": {"p_a": "Z:/数据/机器学习"}, "course_root": "Z:/unused",
                  "home": "C:/Users/source", "codex_home": "C:/Users/source/.codex"}
        mapped = core.map_value("Z:/数据/机器学习/课本.pdf", source, cfg, {}, "source", "target")
        self.assertEqual(mapped, "/Users/target/Documents/另一个名字/课本.pdf")

    def test_project_without_chats_is_registered_and_can_roll_back(self):
        for failure in [None, "databases"]:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = pathlib.Path(temporary)
                cfg, _, _, _, original = self.fixture(root)
                target = root / "new-parent/机器学习"
                cfg.update(projects={"p_new": str(target)}, new_projects=["p_new"], project_names={"p_new": "机器学习"})
                cfg["test_failure_stage"] = failure
                plan = {"peer": "Mac", "files": [], "chats": [], "project_actions": [{"course": "p_new", "action": "create"}]}
                manifest = root / "manifest.json"
                core.save_json(manifest, {"source": {"device": "Mac"}, "threads": {}, "assets": {}, "bundles": []})
                package = root / "packet.tar"
                with tarfile.open(package, "w") as archive:
                    archive.add(manifest, arcname="metadata.json")
                if failure:
                    with self.assertRaisesRegex(RuntimeError, "数据库阶段"):
                        core.apply_import(cfg, plan, package, core.inventory(cfg, include_git=False))
                else:
                    core.apply_import(cfg, plan, package, core.inventory(cfg, include_git=False))
                state = core.read_json(pathlib.Path(cfg["codex_home"]) / ".codex-global-state.json")
                with contextlib.closing(core.ro(pathlib.Path(cfg["codex_home"]) / "state_5.sqlite")) as db:
                    count = db.execute("select count(*) from projects where name='机器学习'").fetchone()[0]
                    self.assertEqual(count, 0 if failure else 1)
                    self.assertEqual(db.execute("select count(*) from threads").fetchone()[0], 1)
                if failure:
                    self.assertEqual(state, original)
                else:
                    self.assertEqual(len(state["local-projects"]), len(original["local-projects"]) + 1)
                    self.assertTrue(target.is_dir())

    def test_existing_identity_outside_selection_is_not_moved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            cfg, _, _, _, _ = self.fixture(root)
            cfg.update(projects={"p_a": str(root / "empty")}, new_projects=["p_a"], project_names={"p_a": "目标"})
            source = {"device": "peer", "files": {}, "chats": {"original": {"id": "original", "course": "p_a", "name": "原有聊天", "turns": [], "active": False}}}
            with mock.patch.object(runtime, "activate", return_value=cfg), mock.patch.object(runtime, "catalog", return_value={"projects": []}), mock.patch.object(core, "APP", root):
                plan = project_sync.make_plan({"id": "a" * 32}, source)["plan"]
            self.assertNotEqual(plan["chats"][0]["destination"], "original")
            self.assertEqual(plan["chats"][0]["action"], "fork")


if __name__ == "__main__":
    unittest.main()
