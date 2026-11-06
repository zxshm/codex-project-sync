"""验证历史分叉、三方文件比较、路径隔离与重复同步规则。"""
import copy
import contextlib
import os
import shutil
import pathlib
import sqlite3
import tarfile
import controller
import tempfile
import unittest

import sync_core as core
import windows_app


def chat(turns):
    return {"id": "original", "name": "学习", "course": "高频电子", "turns": turns, "active": False}


def inv(device, turns, files=None):
    return {"device": device, "files": files or {}, "chats": {"original": chat(turns)}}


class SyncRules(unittest.TestCase):
    def test_received_file_acknowledgement_enables_reverse_update(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            core.git(root, "init", "-b", "codex/test")
            courses = root / "课程"
            file = courses / "高频电子/新增笔记.txt"
            file.parent.mkdir(parents=True)
            file.write_text("共同版本", encoding="utf-8")
            relative = "高频电子/新增笔记.txt"
            sha = core.file_hash(file)
            export = root / "work/package/export.tar.gz"
            core.save_json(export.parent / "metadata.json", {"sent_files": {relative: sha}})
            with patch.object(core, "APP", root):
                count = core.acknowledge_received_files({"course_root": str(courses)}, export, {relative: sha})
            self.assertEqual(count, 1)
            baseline = core.read_json(root / "baseline.json")
            local = inv("Windows", [], {relative: {"sha": sha}})
            peer = inv("Mac", [], {relative: {"sha": "对端新内容"}})
            self.assertEqual(core.plan_sync(local, peer, baseline)["files"][0]["action"], "update")

    def test_received_file_acknowledgement_preserves_newer_source(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            file = root / "课程/高频电子/笔记.txt"
            file.parent.mkdir(parents=True)
            file.write_text("较新版本", encoding="utf-8")
            export = root / "work/package/export.tar.gz"
            core.save_json(export.parent / "metadata.json", {"sent_files": {"高频电子/笔记.txt": "旧内容哈希"}})
            with patch.object(core, "APP", root):
                count = core.acknowledge_received_files({"course_root": str(root / "课程")}, export, {"高频电子/笔记.txt": "旧内容哈希"})
            self.assertEqual(count, 0)
            self.assertFalse((root / "baseline.json").exists())
            self.assertEqual(file.read_text(encoding="utf-8"), "较新版本")

    def test_received_file_acknowledgement_rejects_unexported_content(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            export = root / "work/package/export.tar.gz"
            core.save_json(export.parent / "metadata.json", {"sent_files": {}})
            with patch.object(core, "APP", root):
                with self.assertRaisesRegex(RuntimeError, "接收确认"):
                    core.acknowledge_received_files({"course_root": str(root)}, export, {"高频电子/未发送.txt": "hash"})
            self.assertFalse((root / "lock").exists())

    def test_windows_quit_uses_app_exit_and_checks_remaining_processes(self):
        from unittest.mock import patch
        cfg = {}
        evidence = {"方式": "Ctrl+Q", "全部结束": True}
        with patch.object(core.os, "name", "nt"), patch.object(core, "app_running", side_effect=[True, False]), \
             patch.object(core, "check_idle") as idle, patch.object(windows_app, "request_quit", return_value=evidence) as quit_app:
            self.assertTrue(core.close_local_app(cfg))
        idle.assert_called_once_with(cfg)
        quit_app.assert_called_once_with(cfg)
        self.assertEqual(cfg["_last_shutdown"], evidence)

    def test_windows_quit_preserves_independent_cli_processes(self):
        desktop, owned = windows_app.owned_processes([
            {"Name": "ChatGPT.exe", "ProcessId": 1, "ParentProcessId": 0, "ExecutablePath": r"C:\WindowsApps\OpenAI.Codex_v\app\ChatGPT.exe"},
            {"Name": "codex.exe", "ProcessId": 2, "ParentProcessId": 1, "ExecutablePath": r"C:\Codex\codex.exe"},
            {"Name": "codex.exe", "ProcessId": 3, "ParentProcessId": 99, "ExecutablePath": r"C:\Codex\codex.exe"},
            {"Name": "ChatGPT.exe", "ProcessId": 4, "ParentProcessId": 0, "ExecutablePath": r"C:\Other\ChatGPT.exe"}])
        self.assertEqual(desktop, {1})
        self.assertEqual(owned, {1, 2})

    def test_windows_quit_waits_for_active_chat(self):
        from unittest.mock import patch
        with patch.object(core, "app_running", return_value=True), \
             patch.object(core, "check_idle", side_effect=RuntimeError("本机有聊天尚未结束")), \
             patch.object(windows_app, "request_quit") as quit_app:
            with self.assertRaisesRegex(RuntimeError, "尚未结束"):
                core.close_local_app({})
        quit_app.assert_not_called()

    def minimal_course_fixture(self, root):
        """创建与真实安装使用同一路径的隔离文件、数据库和课程目录。"""
        home=root/'codex';home.mkdir()
        for name,tables in [('state_5.sqlite',['threads']+core.STATE_TABLES),('thread_history_1.sqlite',core.HISTORY_TABLES)]:
            with contextlib.closing(sqlite3.connect(home/name)) as db:
                for table in tables:
                    key='id' if table=='threads' else 'thread_id'
                    db.execute('create table "'+table+'" ("'+key+'" text primary key)')
                db.commit()
        core.save_json(home/'.codex-global-state.json',{})
        (home/'session_index.jsonl').write_text('',encoding='utf-8')
        courses=root/'课程'
        for course in core.COURSES:(courses/course).mkdir(parents=True)
        return {'course_root':str(courses),'codex_home':str(home),'state_dir':str(root/'运行记录'),
                'device':'Mac','home':str(root),'canonical_roots':{},'native_provider':'openai','test_mode':True}

    def test_remote_append_and_local_ahead(self):
        a, b = [["a", "1"]], [["a", "1"], ["b", "2"]]
        self.assertEqual(core.plan_sync(inv("Windows", a), inv("Mac", b))["chats"][0]["action"], "update")
        self.assertEqual(core.plan_sync(inv("Windows", b), inv("Mac", a))["local_ahead_chats"], 1)

    def test_divergence_preserves_original_and_deduplicates(self):
        local = inv("Windows", [["a", "1"], ["win", "2"]])
        remote = inv("Mac", [["a", "1"], ["mac", "3"]])
        planned = core.plan_sync(local, remote)
        action = planned["chats"][0]
        self.assertEqual(action["action"], "fork")
        self.assertNotEqual(action["destination"], "original")
        local["chats"][action["destination"]] = copy.deepcopy(remote["chats"]["original"])
        self.assertEqual(core.plan_sync(local, remote)["chats"], [])

    def test_changed_content_with_same_turn_id_is_a_fork(self):
        self.assertEqual(core.compare_history(chat([["a", "1"]]), chat([["a", "changed"]])), "fork")

    def test_round_trip_forks_reuse_both_original_branches(self):
        a = inv("Windows", [["a", "1"], ["w", "2"]])
        b = inv("Mac", [["a", "1"], ["m", "3"]])
        for _ in range(3):
            for target, source in [(a,b),(b,a)]:
                plan=core.plan_sync(target,source)
                for action in plan["chats"]:
                    row=copy.deepcopy(source["chats"][action["source"]])
                    row["lineage"]=row.get("lineage",action["source"])
                    target["chats"][action["destination"]]=row
        self.assertEqual(len(a["chats"]),2)
        self.assertEqual(len(b["chats"]),2)
        self.assertEqual(core.plan_sync(a,b)["chats"],[])

    def test_three_way_files(self):
        a, b, c = {"sha": "a"}, {"sha": "b"}, {"sha": "c"}
        base = {"files": {"高频电子/笔记.md": {"local": "a", "remote": "a"}}}
        local, remote = inv("Windows", [], {"高频电子/笔记.md": a}), inv("Mac", [], {"高频电子/笔记.md": b})
        self.assertEqual(core.plan_sync(local, remote, base)["files"][0]["action"], "update")
        local["files"]["高频电子/笔记.md"] = c
        action = core.plan_sync(local, remote, base)["files"][0]
        self.assertEqual(action["action"], "conflict")
        self.assertNotEqual(action["destination"], "高频电子/笔记.md")

    def test_source_deletion_never_deletes_local(self):
        self.assertEqual(core.plan_sync(inv("Windows", [], {"高频电子/课本.pdf": {"sha": "a"}}), inv("Mac", []))["files"], [])

    def test_unresolved_file_conflict_stays_a_conflict_on_next_edit(self):
        rel="高频电子/笔记.md"
        base={"files":{rel:{"local":"win-version","remote":"mac-version"}}}
        local=inv("Windows",[],{rel:{"sha":"win-version"}})
        remote=inv("Mac",[],{rel:{"sha":"mac-new-version"}})
        self.assertEqual(core.plan_sync(local,remote,base)["files"][0]["action"],"conflict")

    def test_image_link_slashes_and_aliases_keep_same_history(self):
        raw='C:\\Users\\me\\Temp\\图.png';target='/Users/me/.codex/migrated-assets/course-sync/hash.png'
        source={'course_root':'C:/课程','codex_home':'C:/Users/me/.codex','home':'C:/Users/me'}
        cfg={'course_root':'/Users/me/课程','codex_home':'/Users/me/.codex','home':'/Users/me','path_assets':{raw:target},'normalized_assets':{core.asset_key(raw):target}}
        for link in ['![图](C:/Users/me/Temp/图.png)','![图](<C:/Users/me/Temp/图.png>)']:
            mapped=core.map_value(link,source,cfg,{},'a','b')
            self.assertIn(target,mapped)
            self.assertEqual('<' in link,'<' in mapped)
        canonical_cfg={'canonical_roots':{'/Users/me/.codex':'@codex'},'aliases':{target:'@original-image'}}
        self.assertEqual(core.canonical('![图]('+target+')',canonical_cfg),'![图](@original-image)')

    def test_reused_image_filename_is_scoped_to_its_message(self):
        source={'course_root':'C:/课程','codex_home':'C:/home/.codex','home':'C:/home'}
        cfg={'course_root':'/课程','codex_home':'/home/.codex','home':'/home','scoped_assets':{'chat/first':{'C:/temp/page.png':'/saved/first.png'},'chat/second':{'C:/temp/page.png':'/saved/second.png'}}}
        for message in ['first','second']:
            obj={'type':'agentMessage','id':message,'text':'![课件](C:/temp/page.png)'}
            mapped=core.map_value(obj,source,cfg,{},'chat','target')
            self.assertEqual(mapped['text'],'![课件](/saved/'+message+'.png)')
            canonical_cfg={'canonical_roots':{},'aliases':{}}
            self.assertEqual(core.canonical(obj,canonical_cfg),core.canonical(mapped,canonical_cfg))

    def test_active_branch_blocks_import(self):
        remote = inv("Mac", [["a", "1"]])
        remote["chats"]["original"]["active"] = True
        with self.assertRaisesRegex(RuntimeError, "仍在生成"):
            core.plan_sync(inv("Windows", []), remote)

    def test_stale_unfinished_turn_does_not_block_new_backend(self):
        old={"status":"inProgress","started_at":10}
        self.assertFalse(core.unfinished_in_current_backend(old,20,100))
        self.assertTrue(core.unfinished_in_current_backend(old,120,100))
        self.assertTrue(core.unfinished_in_current_backend({"status":"inProgress","started_at":110},110,100))
        self.assertTrue(core.unfinished_in_current_backend(old,20,0))

    def test_archive_escape_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            for rel in ["../outside", "/outside", "C:/outside"]:
                with self.assertRaises(RuntimeError):
                    core.safe(root, rel)

    def test_unreadable_directory_must_not_be_treated_as_empty(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary)
            for course in core.COURSES:(root/course).mkdir()
            def denied(*args,**kwargs):
                kwargs['onerror'](PermissionError('系统拒绝读取文稿'))
                return iter([])
            with patch('sync_core.os.walk',denied),self.assertRaisesRegex(RuntimeError,'系统未允许读取'):
                core.inventory({'course_root':str(root),'state_dir':str(root)})

    def test_windows_extended_paths_and_formula_preservation(self):
        cfg = {"course_root": "C:\\Users\\me\\课程", "canonical_roots": {"C:\\Users\\me\\课程": "@course", "/Users/me/课程": "@course"}}
        self.assertEqual(core.course_of("\\\\?\\C:\\Users\\me\\课程\\高频电子", cfg), "高频电子")
        formula = "公式 \\(x\\) [课件](C:\\Users\\me\\课程\\高频电子\\a.pdf)"
        mapped = core.canonical(formula, cfg)
        self.assertIn("\\(x\\)", mapped)
        self.assertIn("@course/高频电子/a.pdf", mapped)
        self.assertEqual(core.canonical('[课件](C:/Users/me/课程/高频电子/a.pdf)',cfg),'[课件](@course/高频电子/a.pdf)')

    def test_git_bundle_preserves_long_tree_refs_and_current_branch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary);source=root/'source';target=root/'target'
            source.mkdir();target.mkdir()
            core.git(source,'init','-b','main')
            (source/'笔记.txt').write_text('原始内容',encoding='utf-8')
            core.checkpoint_repo(source,'源版本')
            tree=core.git(source,'rev-parse','HEAD^{tree}')
            reference='refs/codex/'+('很长的快照路径/'*12)+'snapshot'
            core.git(source,'update-ref',reference,tree)
            bundle=root/'history.bundle';core.git(source,'bundle','create',bundle,'--all')
            mapping=core.import_bundle(target,bundle,'Mac')
            self.assertEqual(core.git(target,'rev-parse',mapping[reference]),tree)
            self.assertEqual(core.git(target,'symbolic-ref','HEAD'),'refs/heads/codex/course-sync')

    def test_checkpoint_saves_all_empty_nested_repositories_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary)/'课程';root.mkdir()
            child=root/'子工程';grandchild=child/'下一层';grandchild.mkdir(parents=True)
            sibling=root/'另一个空仓库';sibling.mkdir()
            for path in [child,grandchild,sibling]:core.git(path,'init','-b','original')
            core.checkpoint_repo(root,'备份各级空仓库')
            for path in [child,grandchild,sibling,root]:
                core.git(path,'rev-parse','--verify','HEAD^{commit}')
                self.assertEqual(core.git(path,'status','--porcelain'),'')
            self.assertEqual(core.git(child,'symbolic-ref','HEAD'),'refs/heads/original')
            head=core.git(root,'rev-parse','HEAD')
            core.checkpoint_repo(root,'重复备份不产生额外提交')
            self.assertEqual(core.git(root,'rev-parse','HEAD'),head)

    def test_git_object_pack_reuses_course_files_without_losing_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary);source=root/'来源';source.mkdir()
            payload=os.urandom(2*1024*1024)
            (source/'课件.bin').write_bytes(payload);(source/'笔记.txt').write_text('第一版',encoding='utf-8')
            core.checkpoint_repo(source,'来源首次提交');first=core.git(source,'rev-parse','HEAD')
            core.git(source,'tag','历史版本',first)
            tree=core.git(source,'rev-parse','HEAD^{tree}')
            core.git(source,'update-ref','refs/codex/旧快照',tree)
            courses=root/'接收';target=courses/'单片机';target.mkdir(parents=True)
            core.git(target,'init','-b','local')
            (target/'本机笔记.txt').write_text('保留本机内容',encoding='utf-8')
            core.git(target,'add','本机笔记.txt');core.git(target,'commit','-m','本机已有提交')
            local_head=core.git(target,'rev-parse','HEAD')
            (target/'课件.bin').write_bytes(payload)
            blob=core.file_hashes(target/'课件.bin')[1]
            inventory={'git_file_blobs':{'单片机/课件.bin':blob}}
            request={'git_have':{'单片机':core.git(target,'cat-file','--batch-all-objects','--batch-check=%(objectname)').splitlines()},
                     'git_file_blobs':inventory['git_file_blobs'],'files':[]}
            unpack=root/'待安装';packs=unpack/'packs';packs.mkdir(parents=True)
            pack=core.export_git_pack(source,'单片机',request,inventory,packs)
            self.assertLess((packs/pack['file']).stat().st_size,65536)
            self.assertIn(blob,pack['prime_files'])
            mapping=core.import_git_pack(target,pack,unpack,courses,'Mac')
            for ref,oid in pack['refs'].items():self.assertEqual(core.git(target,'rev-parse',mapping[ref]),oid)
            self.assertEqual(core.git(target,'rev-parse','HEAD'),local_head)
            self.assertEqual(core.git(target,'symbolic-ref','HEAD'),'refs/heads/local')
            core.git(target,'fsck','--full')
            (source/'笔记.txt').write_text('第二版',encoding='utf-8');core.checkpoint_repo(source,'来源追加提交')
            request['git_have']['单片机']=core.git(target,'cat-file','--batch-all-objects','--batch-check=%(objectname)').splitlines()
            next_pack=core.export_git_pack(source,'单片机',request,inventory,packs)
            self.assertLess((packs/next_pack['file']).stat().st_size,65536)
            self.assertNotIn(blob,next_pack['prime_files'])
            core.import_git_pack(target,next_pack,unpack,courses,'Mac')
            self.assertIn(first,core.git(target,'rev-list','--all').splitlines())
            core.git(target,'fsck','--full')
            self.assertEqual((target/'本机笔记.txt').read_text(encoding='utf-8'),'保留本机内容')
            self.assertEqual((target/'课件.bin').read_bytes(),payload)

    def test_changed_reused_git_file_stops_before_updating_refs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary);source=root/'来源';source.mkdir()
            (source/'课件.txt').write_text('原始课件',encoding='utf-8');core.checkpoint_repo(source,'来源提交')
            blob=core.file_hashes(source/'课件.txt')[1]
            courses=root/'接收';target=courses/'单片机';target.mkdir(parents=True)
            (target/'课件.txt').write_text('传输时被编辑的课件',encoding='utf-8')
            packs=root/'待安装/packs';packs.mkdir(parents=True)
            request={'git_have':{},'git_file_blobs':{'单片机/课件.txt':blob},'files':[]}
            pack=core.export_git_pack(source,'单片机',request,{},packs)
            with self.assertRaisesRegex(RuntimeError,'本机文件发生变化'):
                core.import_git_pack(target,pack,packs.parent,courses,'Mac')
            self.assertEqual(core.git(target,'for-each-ref'),'')
            self.assertEqual((target/'课件.txt').read_text(encoding='utf-8'),'传输时被编辑的课件')

    def test_compressed_install_reuses_existing_and_incoming_git_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary);cfg=self.minimal_course_fixture(root)
            parent=pathlib.Path(cfg['course_root'])/'单片机'
            source=root/'来源';source.mkdir()
            binary=os.urandom(1024*1024)
            (parent/'课件.bin').write_bytes(binary);(source/'课件.bin').write_bytes(binary)
            (source/'答案.txt').write_text('新的学习答案',encoding='utf-8');core.checkpoint_repo(source,'完整来源历史')
            expected=core.inventory(cfg)
            rel='单片机/答案.txt';action={'source':rel,'destination':rel,'action':'add','sha':core.file_hash(source/'答案.txt')}
            plan={'peer':'Windows','files':[action],'chats':[]}
            request=dict(plan,git_have=expected['repos'],git_file_blobs=expected['git_file_blobs'])
            source_inventory={'git_file_blobs':{rel:core.file_hashes(source/'答案.txt')[1]}}
            stage=root/'打包';stage.mkdir()
            pack=core.export_git_pack(source,'单片机',request,source_inventory,stage)
            self.assertEqual(set(pack['prime_files'].values()),{'单片机/课件.bin',rel})
            metadata={'version':core.SYNC_VERSION,'source':dict(expected,device='Windows'),'threads':{},'assets':{},'path_assets':{},'bundles':[],'packs':[pack]}
            manifest=stage/'metadata.json';core.save_json(manifest,metadata);package=root/'package.tar.gz'
            with tarfile.open(package,'w:gz') as archive:
                archive.add(manifest,arcname='metadata.json');archive.add(source/'答案.txt',arcname='files/'+rel)
                archive.add(stage/pack['file'],arcname='packs/'+pack['file'])
            self.assertLess(package.stat().st_size,65536)
            result=core.apply_import(cfg,plan,package,expected)
            self.assertEqual(core.read_json(pathlib.Path(result['backup'])/'transaction.json')['status'],'complete')
            self.assertEqual((parent/'课件.bin').read_bytes(),binary)
            self.assertEqual((parent/'答案.txt').read_text(encoding='utf-8'),'新的学习答案')
            self.assertIn(core.git(source,'rev-parse','HEAD'),core.git(parent,'rev-list','--all').splitlines())
            self.assertEqual(core.git(parent,'status','--porcelain'),'')
            core.git(parent,'fsck','--full')

    def test_daily_export_does_not_read_or_send_git_history(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary);cfg=self.minimal_course_fixture(root)
            course=pathlib.Path(cfg['course_root'])/'单片机'
            note=course/'笔记.txt';note.write_text('只同步学习文件',encoding='utf-8')
            core.checkpoint_repo(course,'原有本机版本')
            rel='单片机/笔记.txt';request={'files':[{'source':rel,'destination':rel,'action':'add','sha':core.file_hash(note)}],'chats':[],'source_chats':{}}
            with patch('sync_core.git',side_effect=AssertionError('日常模式不应读取 Git 历史')):
                result=core.export_package(cfg,request)
            metadata=core.read_json(pathlib.Path(result['path']).parent/'metadata.json')
            self.assertEqual(metadata['packs'],[]);self.assertEqual(metadata['bundles'],[])
            self.assertEqual(result['git_bytes'],0)
            with tarfile.open(result['path']) as archive:
                self.assertEqual(archive.extractfile('files/'+rel).read(),note.read_bytes())
                self.assertFalse(any(name.startswith(('packs/','bundles/')) for name in archive.getnames()))

    def test_optional_git_export_excludes_automatic_snapshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary);source=root/'来源';source.mkdir()
            (source/'笔记.txt').write_text('正式版本',encoding='utf-8');core.checkpoint_repo(source,'正式提交')
            core.git(source,'tag','保留标签')
            core.git(source,'update-ref','refs/codex/自动快照',core.git(source,'rev-parse','HEAD^{tree}'))
            pack=core.export_git_pack(source,'单片机',{'git_have':{},'include_snapshots':False},{},root)
            self.assertTrue(any(name.startswith('refs/heads/') for name in pack['refs']))
            self.assertTrue(any(name.startswith('refs/tags/') for name in pack['refs']))
            self.assertFalse(any(name.startswith('refs/codex/') for name in pack['refs']))

    def test_active_chat_keeps_download_for_next_click_without_closing_app(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary);home=root/'codex';home.mkdir()
            with contextlib.closing(sqlite3.connect(home/'state_5.sqlite')) as db:
                db.execute('create table threads (id text,rollout_path text)');db.commit()
            with contextlib.closing(sqlite3.connect(home/'thread_history_1.sqlite')) as db:
                db.execute('create table thread_items (thread_id text,item_json text)');db.commit()
            cfg={'device':'Windows','codex_home':str(home)}
            plan={'peer':'Mac','files':[{'action':'add','source':'单片机/答案.txt','destination':'单片机/答案.txt','sha':'unused'}],
                  'chats':[{'action':'update','source':'chat','destination':'chat'}],'same_chats':0,'local_ahead_chats':0,'git_changed':False,'include_git':False}
            local={'repos':{},'git_reachable':{},'git_file_blobs':{}};remote={'device':'Mac','chats':{}}
            package=root/'fixture.tar'
            with tarfile.open(package,'w'):pass
            payload=package.read_bytes();descriptor={'path':'fixture','size':len(payload),'sha':core.digest(payload)}
            def receive(cfg,exported,destination,progress):destination.write_bytes(payload)
            with patch.object(core,'APP',root),patch.object(controller,'load_config',return_value=cfg),\
                 patch.object(controller,'inspect',return_value=(local,remote,plan)),\
                 patch.object(controller,'peer_rpc',return_value=descriptor) as export,\
                 patch.object(controller,'download',side_effect=receive) as download,\
                 patch.object(core,'close_local_app',return_value=False) as close:
                with patch.object(core,'check_idle',side_effect=RuntimeError('本机有聊天尚未结束')):
                    result=controller.execute()
                self.assertTrue(result['等待聊天结束']);close.assert_not_called()
                with patch.object(core,'check_idle'),patch.object(core,'apply_import',side_effect=RuntimeError('已进入安装阶段')):
                    with self.assertRaisesRegex(RuntimeError,'已进入安装阶段'):controller.execute()
                self.assertEqual(download.call_count,1);self.assertEqual(export.call_count,1)
                self.assertFalse((root/'lock').exists())

    def test_install_new_or_rolled_back_nested_repository(self):
        for leftover in [False,True]:
            with self.subTest(回滚遗留=leftover),tempfile.TemporaryDirectory() as temporary:
                root=pathlib.Path(temporary);cfg=self.minimal_course_fixture(root)
                parent=pathlib.Path(cfg['course_root'])/'单片机';child=parent/'单片机手写答案_20261102'
                (parent/'原有笔记.txt').write_text('保留原有内容',encoding='utf-8')
                core.checkpoint_repo(parent,'原有课程备份')
                source=root/'来源仓库';source.mkdir()
                (source/'答案.txt').write_text('要同步的答案',encoding='utf-8')
                core.checkpoint_repo(source,'来源答案提交')
                source_head=core.git(source,'rev-parse','HEAD')
                bundle=root/'source.bundle';core.git(source,'bundle','create',bundle,'--all')
                if leftover:
                    child.mkdir();core.import_bundle(child,bundle,'Windows')
                    with self.assertRaises(RuntimeError):core.git(child,'rev-parse','--verify','HEAD^{commit}')
                expected=core.inventory(cfg)
                rel='单片机/单片机手写答案_20261102/答案.txt'
                action={'source':rel,'destination':rel,'action':'add','sha':core.file_hash(source/'答案.txt')}
                plan={'peer':'Windows','files':[action],'chats':[]}
                metadata={'version':1,'source':dict(expected,device='Windows'),'threads':{},'assets':{},'path_assets':{},
                          'bundles':[] if leftover else [{'repo':'单片机/单片机手写答案_20261102','file':'source.bundle'}]}
                manifest=root/'metadata.json';core.save_json(manifest,metadata)
                package=root/'package.tar'
                with tarfile.open(package,'w') as archive:
                    archive.add(manifest,arcname='metadata.json')
                    archive.add(source/'答案.txt',arcname='files/'+rel)
                    if not leftover:archive.add(bundle,arcname='bundles/source.bundle')
                result=core.apply_import(cfg,plan,package,expected)
                self.assertEqual(core.read_json(pathlib.Path(result['backup'])/'transaction.json')['status'],'complete')
                self.assertEqual((child/'答案.txt').read_text(encoding='utf-8'),'要同步的答案')
                self.assertEqual((parent/'原有笔记.txt').read_text(encoding='utf-8'),'保留原有内容')
                self.assertEqual(core.git(child,'symbolic-ref','HEAD'),'refs/heads/codex/course-sync')
                self.assertIn(source_head,core.git(child,'rev-list','--all').splitlines())
                self.assertEqual(core.git(parent,'status','--porcelain'),'')
                entry=core.git(parent,'ls-tree','HEAD','单片机手写答案_20261102')
                self.assertIn('160000 commit '+core.git(child,'rev-parse','HEAD'),entry)


if __name__ == "__main__":
    unittest.main()
