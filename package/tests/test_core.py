# -*- coding: utf-8 -*-
"""核心契约测试: 扩展名即事实 / 写透默认 / batch 原语 / migrate / scheduler。

契约不变的覆盖(双模式读/typed/槽位管理/Mapping)。
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zlib

_MOD_PATH = os.path.join(os.path.dirname(__file__), "..", "runtime", "savegame.py")
spec = importlib.util.spec_from_file_location("savegame_core", os.path.abspath(_MOD_PATH))
sg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sg)

JSON, BIN, JSON_Z, BIN_Z = ".json", ".bin", ".json.z", ".bin.z"


class ApiCase(unittest.TestCase):
    """每个用例独立 root + api。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "game")
        self.api = sg.SaveGameApi(self.root)

    def tearDown(self):
        try:
            self.api.shutdown()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, key, suffix=JSON, slot="default"):
        return os.path.join(self.root, slot, key + suffix)


class TestFileIsValue(ApiCase):
    """文件即值 + 扩展名即事实。"""

    def test_plain_json_file_contains_raw_value(self):
        self.api.set("score", 700)
        self.assertTrue(os.path.isfile(self.path("score")))
        with open(self.path("score"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), 700)

    def test_bytes_go_to_bin(self):
        blob = bytes(range(256))
        self.api.set("portrait", blob)
        self.assertTrue(os.path.isfile(self.path("portrait", BIN)))
        with open(self.path("portrait", BIN), "rb") as f:
            self.assertEqual(f.read(), blob)

    def test_key_with_dots(self):
        self.api.set("player.name", "alice")
        self.assertTrue(os.path.isfile(self.path("player.name")))
        self.assertEqual(self.api.get_str("player.name"), "alice")
        self.assertIn("player.name", self.api.keys())

    def test_keys_enumeration_strips_suffixes(self):
        self.api.set("a", 1)
        self.api.set("b", b"x")
        self.assertEqual(set(self.api.keys()), {"a", "b"})


class TestWriteThrough(ApiCase):
    """写透默认: 变化即落盘。"""

    def test_set_writes_immediately(self):
        r = self.api.set("k", 1)
        self.assertTrue(r.ok)
        self.assertEqual(r.status, "written")
        self.assertTrue(os.path.isfile(self.path("k")))

    def test_same_value_shortcircuit_no_io(self):
        self.api.set("k", 1)
        os.remove(self.path("k"))  # 盘上清掉
        r = self.api.set("k", 1)   # 同值(缓存命中)
        self.assertTrue(r.ok)
        self.assertFalse(os.path.isfile(self.path("k")))  # 零 IO: 未重建文件

    def test_type_confusion_is_change(self):
        self.api.set("k", 1)
        self.api.set("k", True)    # 1 == True 但类型变 => 变化
        with open(self.path("k"), encoding="utf-8") as f:
            self.assertIs(json.load(f), True)
        self.api.set("k", 1.0)     # 1 == 1.0 但类型变
        with open(self.path("k"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), 1.0)
            self.assertIsInstance(json.load(open(self.path("k"))), float)

    def test_nan_rejected(self):
        r = self.api.set("k", float("nan"))
        self.assertFalse(r.ok)
        self.assertEqual(r.status, "rejected")
        r2 = self.api.set("k", float("inf"))
        self.assertFalse(r2.ok)

    def test_nested_bytes_rejected_with_reason(self):
        r = self.api.set("k", {"img": b"abc"})
        self.assertFalse(r.ok)
        self.assertIn("bytes", str(r.detail.get("reason", "")))

    def test_write_failure_reported_at_set(self):
        os.makedirs(self.path("k"))  # 同名目录 => 写失败
        r = self.api.set("k", 1)
        self.assertFalse(r.ok)
        self.assertEqual(r.status, "failed")


class TestStateInheritance(ApiCase):
    """槽出生流程: state 继承 defaults, 立户口惰性。"""

    def write_defaults(self, doc):
        d = os.path.join(self.root, ".savegame")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "defaults.json"), "w", encoding="utf-8") as f:
            json.dump(doc, f)

    def test_new_slot_inherits_compress_default(self):
        self.write_defaults({"compress": True})
        api2 = sg.SaveGameApi(self.root)  # defaults 在 api 构造前写入
        api2.set("k", "v")
        self.assertTrue(os.path.isfile(self.path("k", JSON_Z)))
        with open(os.path.join(self.root, "default", ".savegame", "state.json")) as f:
            self.assertTrue(json.load(f)["compress"])

    def test_defaults_change_does_not_affect_existing_slot(self):
        api = self.api
        api.set("k", "v")  # default 槽立户口(明文)
        self.write_defaults({"compress": True})
        api.set("k2", "v2")
        self.assertTrue(os.path.isfile(self.path("k2", JSON)))  # 仍明文

    def test_state_file_lazy_until_first_write(self):
        api = self.api
        api.get_slot("fresh")
        self.assertFalse(os.path.isfile(
            os.path.join(self.root, "fresh", ".savegame", "state.json")))
        api.get_slot("fresh").set("k", 1)
        self.assertTrue(os.path.isfile(
            os.path.join(self.root, "fresh", ".savegame", "state.json")))


class TestCompressState(ApiCase):
    """压缩槽: 全槽统一 .z; bytes 走 .bin.z。"""

    def compress_slot(self):
        self.api.migrate.compress(defaults=False)
        return self.api.get_slot("default")

    def test_compressed_roundtrip(self):
        self.api.set("a", {"x": [1, 2]})
        self.api.set("b", b"\x00\x01")
        self.api.migrate.compress(defaults=False)
        self.assertTrue(os.path.isfile(self.path("a", JSON_Z)))
        self.assertTrue(os.path.isfile(self.path("b", BIN_Z)))
        self.assertEqual(self.api.get("a"), {"x": [1, 2]})
        self.assertEqual(self.api.get("b"), b"\x00\x01")

    def test_new_writes_follow_state(self):
        self.compress_slot()
        self.api.set("c", 9)
        self.assertTrue(os.path.isfile(self.path("c", JSON_Z)))
        self.assertFalse(os.path.isfile(self.path("c", JSON)))

    def test_corrupt_z_detected(self):
        self.compress_slot()
        self.api.set("k", "hello")
        p = self.path("k", JSON_Z)
        raw = bytearray(open(p, "rb").read())
        raw[-1] ^= 0xFF
        with open(p, "wb") as f:
            f.write(bytes(raw))
        self.api.refresh(flush=False)
        self.assertEqual(self.api.get("k", "fb"), "fb")  # 回退
        with self.assertRaises(sg.SavegameError):
            self.api.get("k")  # 严格

    def test_decode_tolerance_mixed_forms(self):
        """迁移残留(混合态)可读: state=明文但 .z 文件在 => 找到并解出。"""
        self.api.set("k", "v")
        data = zlib.compress(json.dumps("v").encode(), 6)
        with open(self.path("k", JSON_Z), "wb") as f:
            f.write(data)
        os.remove(self.path("k"))
        self.api.refresh(flush=False)
        self.assertEqual(self.api.get("k"), "v")


class TestBatchPrimitive(ApiCase):
    """batch 原语: 进出语义与尾款。"""

    def test_batch_queues_and_flush(self):
        api = self.api
        api.batch(True)
        r = api.set("k", 1)
        self.assertEqual(r.status, "queued")
        self.assertFalse(os.path.isfile(self.path("k")))
        api.flush()
        self.assertTrue(os.path.isfile(self.path("k")))

    def test_batch_off_flushes_tail_and_restores(self):
        api = self.api
        api.batch(True)
        for i in range(5):
            api.set("k%d" % i, i)
        api.batch(False)
        for i in range(5):
            self.assertTrue(os.path.isfile(self.path("k%d" % i)))
        r = api.set("after", 1)
        self.assertEqual(r.status, "written")  # 已回写透

    def test_dirty_count(self):
        api = self.api
        api.batch(True)
        api.set("a", 1)
        api.set("b", 2)
        self.assertEqual(api.dirty_count(), 2)
        api.flush()
        self.assertEqual(api.dirty_count(), 0)

    def test_flush_failure_keeps_dirty(self):
        api = self.api
        api.batch(True)
        api.set("good", 1)
        api.set("bad", 2)
        os.makedirs(self.path("bad"))  # bad 写必失败
        r = api.flush()
        self.assertEqual(r.status, "partial")
        self.assertTrue(os.path.isfile(self.path("good")))
        self.assertEqual(api.dirty_count(), 1)  # bad 留队
        r2 = api.flush()  # 重试
        self.assertEqual(r2.status, "partial")
        self.assertEqual(api.dirty_count(), 1)  # 仍在


class TestMigrate(ApiCase):
    """migrate: 一次性整槽重编码。"""

    def seed(self, slot="default", n=4):
        s = self.api.get_slot(slot)
        for i in range(n):
            s.set("k%d" % i, {"i": i})
        s.set("blob", os.urandom(32))
        return s

    def test_compress_all_slots_and_defaults(self):
        self.seed()
        self.seed(slot="b")
        r = self.api.migrate.compress()
        self.assertTrue(r.ok)
        self.assertTrue(os.path.isfile(self.path("k0", JSON_Z)))
        self.assertTrue(os.path.isfile(self.path("blob", BIN_Z, slot="b")))
        with open(os.path.join(self.root, ".savegame", "defaults.json")) as f:
            self.assertTrue(json.load(f)["compress"])
        self.assertEqual(r.detail["defaults_updated"], True)
        self.assertEqual(r.detail["slots"]["default"]["converted"], 5)
        self.assertEqual(self.api.get("k2"), {"i": 2})

    def test_idempotent_rerun_all_skipped(self):
        self.seed(n=2)
        self.api.migrate.compress()
        r = self.api.migrate.compress()
        self.assertTrue(r.ok)
        d = r.detail["slots"]["default"]
        self.assertEqual(d["converted"], 0)
        self.assertEqual(d["skipped"], 3)
        self.assertEqual(d["failed"], {})

    def test_interrupted_converges(self):
        s = self.seed(n=4)
        # 模拟中断: 手动把 k0 转成压缩态(残留), 其余仍明文
        raw = open(self.path("k0"), "rb").read()
        with open(self.path("k0", JSON_Z), "wb") as f:
            f.write(zlib.compress(raw, 6))
        os.remove(self.path("k0"))
        r = self.api.migrate.compress()
        self.assertTrue(r.ok)
        d = r.detail["slots"]["default"]
        self.assertEqual(d["converted"], 4)  # blob + k1..k3
        self.assertEqual(d["skipped"], 1)    # k0 已目标态
        for i in range(4):
            self.assertTrue(os.path.isfile(self.path("k%d" % i, JSON_Z)))

    def test_failed_key_does_not_block(self):
        s = self.seed(n=2)
        with open(self.path("k0"), "wb") as f:
            f.write(b"not-json-at-all")  # k0 损坏
        r = self.api.migrate.compress()
        self.assertEqual(r.status, "partial")
        d = r.detail["slots"]["default"]
        self.assertIn("k0", d["failed"])
        self.assertEqual(d["converted"], 2)  # k1 + blob 照转
        self.assertTrue(os.path.isfile(self.path("k1", JSON_Z)))
        self.assertTrue(os.path.isfile(self.path("k0", JSON)))  # 旧态留存可读侧处理

    def test_plain_reverse(self):
        self.seed(n=2)
        self.api.migrate.compress()
        r = self.api.migrate.plain()
        self.assertTrue(r.ok)
        self.assertTrue(os.path.isfile(self.path("k0", JSON)))
        self.assertFalse(os.path.isfile(self.path("k0", JSON_Z)))
        with open(os.path.join(self.root, ".savegame", "defaults.json")) as f:
            self.assertFalse(json.load(f)["compress"])

    def test_single_slot_scope(self):
        self.seed()
        self.seed(slot="b")
        r = self.api.migrate.compress(slot="b", defaults=False)
        self.assertTrue(r.ok)
        self.assertEqual(sorted(r.detail["slots"]), ["b"])
        self.assertTrue(os.path.isfile(self.path("k0", JSON)))       # default 未动
        self.assertTrue(os.path.isfile(self.path("k0", JSON_Z, slot="b")))
        self.assertFalse(os.path.isfile(os.path.join(self.root, ".savegame", "defaults.json")))


class TestSchedulerWorker(ApiCase):
    """_FlushWorker: 隔离线程 + 策略 + 快照交接。"""

    def test_interval_flush(self):
        api = self.api
        worker = sg._FlushWorker(api)
        worker.configure(interval=0.3)
        worker.start()
        try:
            api.batch(True)
            api.set("k", 1)
            deadline = time.monotonic() + 3.0
            while not os.path.isfile(self.path("k")) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(os.path.isfile(self.path("k")))
            # flushed_count 与文件出现不是原子: worker 在 flush_all 返回后才自增,
            # 轮询等待(与文件同期限)消除读早于自增的竞态
            while worker.status()["flushed_count"] < 1 \
                    and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(worker.status()["flushed_count"], 1)
        finally:
            worker.stop()
            api.batch(False)

    def test_flush_now_immediate(self):
        api = self.api
        worker = sg._FlushWorker(api)
        worker.configure(interval=999)  # 定期策略不触发
        worker.start()
        try:
            api.batch(True)
            api.set("k", 1)
            worker.flush_now()
            deadline = time.monotonic() + 2.0
            while not os.path.isfile(self.path("k")) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(os.path.isfile(self.path("k")))
        finally:
            worker.stop()
            api.batch(False)

    def test_threshold_trigger(self):
        api = self.api
        worker = sg._FlushWorker(api)
        worker.configure(interval=0, threshold_keys=3)
        worker.start()
        try:
            api.batch(True)
            for i in range(3):
                api.set("k%d" % i, i)
            deadline = time.monotonic() + 3.0
            while api.dirty_count() > 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(api.dirty_count(), 0)
            for i in range(3):
                p = self.path("k%d" % i)
                stop = time.monotonic() + 3.0
                while not os.path.isfile(p) and time.monotonic() < stop:
                    time.sleep(0.05)  # 快照交接后 IO 仍在途, 等落盘
                self.assertTrue(os.path.isfile(p))
        finally:
            worker.stop()
            api.batch(False)

    def test_status_reports_dead(self):
        api = self.api
        worker = sg._FlushWorker(api)
        worker.configure(interval=0.2)
        worker.start()
        original = api.flush_all

        def boom():
            raise RuntimeError("disk exploded")
        api.flush_all = boom
        try:
            deadline = time.monotonic() + 3.0
            while worker.status()["alive"] and time.monotonic() < deadline:
                time.sleep(0.05)
            st = worker.status()
            self.assertFalse(st["alive"])
            self.assertIn("disk exploded", st["dead_reason"])
        finally:
            api.flush_all = original
            worker.stop()

    def test_concurrent_set_during_flush(self):
        """worker flush 在途(锁外 IO 窗口)期间的并发 set: 快照交接收敛——
        在途旧快照与窗口内排队的新值互不吞没, 尾款后终态=最后意图。
        确定性交错: 慢 IO 门控把 flush 卡在真实写盘上(不是靠 interval 碰运气),
        hammer 期间 _inflight 非空=窗口真实被穿过; 终态从盘上断言。"""
        api = self.api
        slot = api._active
        worker = sg._FlushWorker(api)
        worker.configure(interval=999)   # 定期策略不触发, 全由 flush_now 驱动
        worker.start()
        gate = threading.Event()
        original = slot._write_payload_nolock

        def slow_k(key, suffix, data):
            if key == "k":
                gate.wait(5)
            original(key, suffix, data)

        slot._write_payload_nolock = slow_k
        errors = []
        try:
            api.batch(True)
            api.set("k", "old")          # 快照键: 卡门用
            worker.flush_now()           # worker 线程进入 flush, 卡在 k 的 IO 上
            deadline = time.monotonic() + 2
            while "k" not in slot._inflight and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertIn("k", slot._inflight)   # 窗口前提: flush 真实在途

            def hammer(idx):
                try:
                    for i in range(30):
                        api.set("w%d" % idx, i)  # 窗口内并发排队(真实交错)
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=hammer, args=(t,)) for t in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            gate.set()                   # 放行 k 的旧快照落盘
            worker.flush_now()           # 尾款: 落窗口内排队的 w0-w3
            deadline = time.monotonic() + 3.0
            while api.dirty_count() > 0 and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            slot._write_payload_nolock = original
            worker.stop()
            api.batch(False)
        self.assertEqual(errors, [])
        self.assertEqual(api.dirty_count(), 0)
        api.refresh(flush=False)         # 从盘上事实断言(不走缓存)
        self.assertEqual(api.get("k"), "old")
        for t in range(4):
            self.assertEqual(api.get("w%d" % t), 29)  # 最终值收敛


class TestContractSurvived(ApiCase):
    """契约三线全覆盖。"""

    def test_dual_mode_read(self):
        with self.assertRaises(KeyError):
            self.api.get("nope")
        self.assertEqual(self.api.get("nope", "fb"), "fb")
        self.api.set("n", 5)
        self.assertEqual(self.api.get("n"), 5)

    def test_typed_strict_and_fallback(self):
        self.api.set("s", "text")
        with self.assertRaises(TypeError):
            self.api.get_int("s")
        self.assertEqual(self.api.get_int("s", 7), 7)
        self.assertEqual(self.api.get_str("s"), "text")

    def test_delete_idempotent(self):
        self.api.set("k", 1)
        self.assertEqual(self.api.delete("k").status, "removed")
        self.assertEqual(self.api.delete("k").status, "missing")

    def test_pop_and_mapping(self):
        self.api.set("a", 1)
        self.api.set("b", 2)
        self.assertIn("a", self.api)
        self.assertEqual(len(self.api), 2)
        self.assertEqual(self.api.pop("a"), 1)
        self.assertNotIn("a", self.api)
        self.assertEqual(self.api.pop("zz", 9), 9)
        self.assertEqual(sorted(self.api.items()), [("b", 2)])

    def test_setdefault_and_update(self):
        self.assertEqual(self.api.setdefault("sv", 3), 3)
        self.assertEqual(self.api.setdefault("sv", 4), 3)
        self.assertTrue(self.api.update({"x": 1, "y": 2}).ok)
        self.assertEqual(self.api.get("y"), 2)

    def test_slot_lifecycle(self):
        self.api.set("k", "default-val")
        r = self.api.use_slot("b")
        self.assertTrue(r.ok)
        self.assertEqual(r.detail, {"from": "default", "to": "b"})
        self.api.set("k", "b-val")
        self.api.use_slot("default")
        self.assertEqual(self.api.get("k"), "default-val")
        self.assertEqual(self.api.get_slot("b").get("k"), "b-val")
        self.assertIn("b", self.api.list_slots())
        self.assertEqual(self.api.delete_slot("b").status, "removed")

    def test_delete_active_slot_recreates_default(self):
        """delete_slot 作用于当前活动槽: 删除后 default 槽重建并被指为活动,
        立即可写可读, 旧数据不可见。"""
        api = self.api
        api.set("k", 1)
        r = api.delete_slot("default")
        self.assertEqual(r.status, "removed")
        self.assertEqual(api.current_slot(), "default")
        self.assertEqual(api.get("k", "fb"), "fb")   # 旧数据随目录消失
        r2 = api.set("k2", 2)                        # 新 default 立即可用
        self.assertTrue(r2.ok)
        self.assertEqual(api.get("k2"), 2)

    def test_export_import(self):
        self.api.set("a", 1)
        self.api.set("b", b"x")
        r = self.api.export_slot("default")
        self.assertTrue(r.ok)
        self.assertEqual(r.data, {"a": 1, "b": b"x"})
        self.api.use_slot("copy")
        self.assertTrue(self.api.import_slot("copy", r.data).ok)
        self.assertEqual(self.api.get("a"), 1)

    def test_negative_cache(self):
        self.assertFalse(self.api.has("ghost"))
        path = self.path("ghost")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("1")
        self.assertFalse(self.api.has("ghost"))  # 负缓存仍生效
        self.api.refresh(flush=False)
        self.assertTrue(self.api.has("ghost"))

    def test_stats_shape(self):
        st = self.api.stats()
        self.assertTrue(st["write_through"])
        self.assertFalse(st["batch_mode"])
        self.assertEqual(st["active_slot"], "default")


class TestRootOverride(unittest.TestCase):
    """root: 环境变量覆盖, 永不落盘; Android 平台条件下的引擎持久化层。"""

    def test_android_falls_back_to_engine_persistent_path(self):
        """无 LOCALAPPDATA 平台(Android Player 实测条件):
        root 落引擎 persistent_data_path(app 私有可写目录), 目录结构对称;
        引擎 API 缺失时退回 ~ 兜底分支。Windows(有 LOCALAPPDATA)不受影响。"""
        real_app = getattr(sg.inx, "application", None)
        saved_lad = os.environ.get("LOCALAPPDATA")
        tmp = tempfile.mkdtemp()

        class _FakeEngineApp:
            class Application:
                @staticmethod
                def persistent_data_path():
                    return "/data/data/com.example/files"

        try:
            # 真实平台条件: Android Player(runtime=True, sys.executable 空 → "game" 兜底)
            saved_exe = sys.executable
            sys.executable = ""
            ctx = type("Ctx", (), {"runtime": True, "project_root": ""})()
            os.environ.pop("LOCALAPPDATA", None)
            # 引擎 API 命中: pdp 层 + 结构对称(Infernux/Saves/<game_dir>)
            sg.inx.application = _FakeEngineApp
            root = sg._saves_root(ctx)
            self.assertEqual(root, os.path.join(
                "/data/data/com.example/files",
                "Infernux", "Saves", "game"))
            # 引擎 API 缺席: 退回 ~ 兜底分支(Android 上该路径不可写)
            sg.inx.application = None
            root2 = sg._saves_root(ctx)
            self.assertTrue(
                root2.replace("\\", "/").endswith(
                    "AppData/Local/Infernux/Saves/game"))
            sys.executable = saved_exe
            # Windows 条件(LOCALAPPDATA 在): 现状路径不受影响(零破坏)
            self.assertIn("Infernux", sg._saves_root(ctx))
        finally:
            if saved_lad is not None:
                os.environ["LOCALAPPDATA"] = saved_lad
            sg.inx.application = real_app
            shutil.rmtree(tmp, ignore_errors=True)

    def test_env_override(self):
        """MGS_SAVES_ROOT 是 preload 域 root 解析(_saves_root)的最高优先覆盖:
        env 命中即返回, 永不落盘; 显式传参的 SaveGameApi 不受 env 劫持。
        (机制唯一生效点在 preload 的 _saves_root——构造期 root_dir 不读 env。)"""
        tmp = tempfile.mkdtemp()
        api = None
        try:
            custom = os.path.join(tmp, "cloud")
            os.environ["MGS_SAVES_ROOT"] = custom
            # 机制本体: env 命中分支不触碰 context, 纯解析直返
            self.assertEqual(sg._saves_root(None), custom)
            # 显式传参生效(与 env 值无关, 不被劫持)
            api = sg.SaveGameApi(custom)
            api.set("k", 1)
            self.assertTrue(os.path.isfile(
                os.path.join(custom, "default", "k.json")))
            # 永不落盘: root 路径(两种分隔符形态)不出现在任何盘上文件里
            needles = {custom.encode("utf-8"),
                       custom.replace("\\", "/").encode("utf-8")}
            for walk_root, _, names in os.walk(custom):
                for n in names:
                    with open(os.path.join(walk_root, n), "rb") as f:
                        blob = f.read()
                    for needle in needles:
                        self.assertNotIn(needle, blob, n)
        finally:
            os.environ.pop("MGS_SAVES_ROOT", None)
            if api is not None:
                api.shutdown()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
