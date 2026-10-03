# -*- coding: utf-8 -*-
"""聚合布局测试: 类型分片/溢开/索引可再生/两轴正交迁移。"""
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import zlib

_MOD_PATH = os.path.join(os.path.dirname(__file__), "..", "runtime", "savegame.py")
spec = importlib.util.spec_from_file_location("savegame_aggregate", os.path.abspath(_MOD_PATH))
sg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sg)


class AggCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "game")
        self.api = sg.SaveGameApi(self.root)

    def tearDown(self):
        try:
            self.api.shutdown()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def slot_dir(self, slot="default"):
        return os.path.join(self.root, slot)

    def index(self, slot="default"):
        with open(os.path.join(self.root, slot, ".savegame", "index.json"),
                  encoding="utf-8") as f:
            return json.load(f)["keys"]

    def state(self, slot="default"):
        with open(os.path.join(self.root, slot, ".savegame", "state.json"),
                  encoding="utf-8") as f:
            return json.load(f)


class TestPackRouting(AggCase):
    """类型分片路由 + jsonl 可读。"""

    def test_type_routing_and_readable_jsonl(self):
        api = self.api
        r = api.migrate.aggregate(defaults=False)
        self.assertTrue(r.ok)
        api.set("score", 700)
        api.set("name", "alice")
        api.set("pos", {"x": 1})
        api.set("flag", True)
        api.set("nothing", None)
        api.set("blob", b"\x00\x01")
        names = set(os.listdir(self.slot_dir()))
        self.assertIn("int-0.pack", names)
        self.assertIn("str-0.pack", names)
        self.assertIn("dict-0.pack", names)
        self.assertIn("bool-0.pack", names)
        self.assertIn("null-0.pack", names)
        self.assertIn("blob.bin", names)          # bytes 恒散放
        with open(os.path.join(self.slot_dir(), "int-0.pack"),
                  encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(lines, [{"k": "score", "v": 700}])  # 人可读 jsonl
        idx = self.index()
        self.assertEqual(idx["score"], "int-0.pack")
        self.assertEqual(idx["blob"], "blob.bin")
        # 读回全类型
        self.assertEqual(api.get("score"), 700)
        self.assertEqual(api.get("name"), "alice")
        self.assertEqual(api.get("pos"), {"x": 1})
        self.assertIs(api.get("flag"), True)
        self.assertIsNone(api.get("nothing"))
        self.assertEqual(api.get("blob"), b"\x00\x01")

    def test_write_through_updates_pack_immediately(self):
        self.api.migrate.aggregate(defaults=False)
        self.api.set("hp", 100)
        text = open(os.path.join(self.slot_dir(), "int-0.pack"),
                    encoding="utf-8").read()
        self.assertIn('"hp"', text)
        self.assertIn("100", text)

    def test_type_change_moves_between_packs(self):
        api = self.api
        api.migrate.aggregate(defaults=False)
        api.set("k", 1)
        self.assertEqual(self.index()["k"], "int-0.pack")
        api.set("k", {"lv": 2})                   # int -> dict 搬家
        self.assertEqual(self.index()["k"], "dict-0.pack")
        self.assertFalse(os.path.isfile(          # 空片已回收
            os.path.join(self.slot_dir(), "int-0.pack")))
        self.assertEqual(api.get("k"), {"lv": 2})

    def test_empty_pack_recycled(self):
        api = self.api
        api.migrate.aggregate(defaults=False)
        api.set("solo", 1)
        self.assertTrue(os.path.isfile(os.path.join(self.slot_dir(), "int-0.pack")))
        api.delete("solo")
        self.assertFalse(os.path.isfile(os.path.join(self.slot_dir(), "int-0.pack")))
        self.assertNotIn("solo", self.index())
        self.assertNotIn("solo", api.keys())

    def test_delete_from_multi_entry_pack(self):
        api = self.api
        api.migrate.aggregate(defaults=False)
        api.set("a", 1)
        api.set("b", 2)
        api.delete("a")
        self.assertEqual(api.get("b"), 2)
        self.assertNotIn("a", self.index())

    def test_bytes_always_scatter_even_aggregate(self):
        self.api.migrate.aggregate(defaults=False)
        self.api.set("img", b"\xff" * 10)
        self.assertTrue(os.path.isfile(os.path.join(self.slot_dir(), "img.bin")))
        self.assertEqual(self.index()["img"], "img.bin")

    def test_nan_rejected_in_aggregate(self):
        self.api.migrate.aggregate(defaults=False)
        r = self.api.set("k", float("nan"))
        self.assertFalse(r.ok)
        self.assertEqual(r.status, "rejected")

    def test_none_value_survives_cold_read(self):
        """回归锁死: None 是合法存储值, 冷读(cache 清空)不得误判缺失。"""
        api = self.api
        api.migrate.aggregate(defaults=False)
        api.set("nothing", None)
        api.set("n", 1)
        api.refresh(flush=False)          # cache 冷却, 强制走片读取路径
        self.assertIsNone(api.get("nothing"))
        self.assertTrue(api.has("nothing"))
        self.assertIn("nothing", api.keys())
        self.assertEqual(api.get("n"), 1)

    def test_keys_from_index_and_scatter_blindness(self):
        api = self.api
        api.set("old", 1)
        api.migrate.aggregate(defaults=False)
        api.set("new", 2)
        self.assertEqual(set(api.keys()), {"old", "new"})
        # 散态盲区: 孤儿 .pack 在散态 keys() 被无视(外部残像不可见)
        api.migrate.scatter(defaults=False)
        self.assertIn("old", api.keys())
        sg._write_pack(os.path.join(self.slot_dir(), "orphan-0.pack"),
                       {"ghost": 1})
        self.assertNotIn("ghost", api.keys())


class TestPackOverflow(AggCase):
    """溢出开新片 + 护栏。"""

    def test_overflow_opens_next_pack(self):
        api = self.api
        api.migrate.aggregate(defaults=False)
        big = "x" * 600000                          # 单值 600KB
        api.set("a", big)                           # str-0 超 1MB? 600KB<1MB
        api.set("b", big)
        api.set("c", big)                           # str-0 约 1.8MB -> 已封顶
        api.set("d", big)                           # -> str-1
        names = sorted(n for n in os.listdir(self.slot_dir())
                       if n.startswith("str-"))
        self.assertEqual(names, ["str-0.pack", "str-1.pack"])
        idx = self.index()
        self.assertEqual(idx["a"], "str-0.pack")
        self.assertEqual(idx["d"], "str-1.pack")
        for k in ("a", "b", "c", "d"):
            self.assertEqual(api.get(k), big)
        # 封顶片仍可更新(不迁移旧键)
        api.set("a", "small")
        self.assertEqual(api.get("a"), "small")
        self.assertEqual(self.index()["a"], "str-0.pack")

    def test_pack_guard_count_seals(self):
        """条目数护栏(size 轴的对称轴): 小值条目数到顶即封顶开新片。
        护栏常量临时调小 = 等价于足够多的条目, 同一代码路径。"""
        api = self.api
        original = sg._PACK_GUARD_COUNT
        sg._PACK_GUARD_COUNT = 3
        try:
            api.migrate.aggregate(defaults=False)
            for i in range(4):                 # 第 4 键溢到 int-1
                api.set("n%d" % i, i)
            names = sorted(n for n in os.listdir(self.slot_dir())
                           if n.startswith("int-"))
            self.assertEqual(names, ["int-0.pack", "int-1.pack"])
            idx = self.index()
            self.assertEqual(idx["n0"], "int-0.pack")
            self.assertEqual(idx["n3"], "int-1.pack")
        finally:
            sg._PACK_GUARD_COUNT = original
        api.refresh(flush=False)
        for i in range(4):
            self.assertEqual(api.get("n%d" % i), i)


class TestIndexResilience(AggCase):
    """索引可再生: 删除/过期自愈。"""

    def test_index_deleted_self_heals(self):
        api = self.api
        api.migrate.aggregate(defaults=False)
        api.set("k1", 1)
        api.set("k2", "v2")
        os.remove(os.path.join(self.slot_dir(), ".savegame", "index.json"))
        api.refresh(flush=False)                    # 清内存
        self.assertEqual(api.get("k1"), 1)          # 读 miss 兜底重建
        self.assertEqual(api.get("k2"), "v2")
        self.assertEqual(api.keys(), ["k1", "k2"])
        self.assertTrue(os.path.isfile(
            os.path.join(self.slot_dir(), ".savegame", "index.json")))  # 重建落盘

    def test_stale_index_entry_rebuilt(self):
        api = self.api
        api.migrate.aggregate(defaults=False)
        api.set("k", 1)
        # 索引造假: k 指向不存在的片 -> 读兜底重建后正确路由
        with open(os.path.join(self.slot_dir(), ".savegame", "index.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"keys": {"k": "int-9.pack"}}, f)
        api.refresh(flush=False)
        self.assertEqual(api.get("k"), 1)
        self.assertEqual(self.index()["k"], "int-0.pack")

    def test_corrupt_pack_read(self):
        api = self.api
        api.migrate.aggregate(defaults=False)
        api.set("k", "v")
        p = os.path.join(self.slot_dir(), "str-0.pack")
        with open(p, "a", encoding="utf-8") as f:
            f.write("garbage-not-json\n")
        api.refresh(flush=False)
        self.assertEqual(api.get("k", "fb"), "fb")  # 回退
        with self.assertRaises(sg.SavegameError):
            api.get("k")                            # 严格


class TestLayoutMigration(AggCase):
    """aggregate/scatter: 四步写序/幂等/中断收敛/组合正交。"""

    def seed(self, n=5, slot="default"):
        s = self.api.get_slot(slot)
        for i in range(n):
            s.set("k%d" % i, {"i": i})
        s.set("s", "text")
        s.set("blob", b"\x00\x02")
        return s

    def test_aggregate_scatter_roundtrip(self):
        api = self.api
        self.seed()
        r = api.migrate.aggregate()
        self.assertTrue(r.ok)
        self.assertEqual(r.detail["defaults_updated"], True)
        names = set(os.listdir(self.slot_dir()))
        self.assertTrue(any(n.startswith("dict-") for n in names))
        self.assertFalse(any(n.endswith(".json") for n in names
                              if not n.startswith(".")))  # 散残留已清
        self.assertEqual(api.get("k2"), {"i": 2})
        self.assertEqual(api.get("blob"), b"\x00\x02")
        with open(os.path.join(self.root, ".savegame", "defaults.json")) as f:
            self.assertTrue(json.load(f)["aggregate"])
        r2 = api.migrate.scatter()
        self.assertTrue(r2.ok)
        self.assertTrue(os.path.isfile(os.path.join(self.slot_dir(), "k2.json")))
        self.assertFalse(os.path.exists(
            os.path.join(self.slot_dir(), ".savegame", "index.json")))  # 索引已清
        self.assertEqual(api.get("s"), "text")

    def test_layout_idempotent(self):
        self.seed(n=2)
        self.api.migrate.aggregate(defaults=False)
        r = self.api.migrate.aggregate(defaults=False)
        self.assertTrue(r.ok)
        d = r.detail["slots"]["default"]
        self.assertEqual(d["converted"], 0)
        self.assertEqual(d["failed"], {})

    def test_interrupted_aggregate_converges(self):
        api = self.api
        self.seed(n=3)
        # 模拟中断: 手动把 k0 收进片(孤儿片), 散文件仍在
        entries = {"k0": api.get("k0")}
        sg._write_pack(os.path.join(self.slot_dir(), "dict-0.pack"), entries)
        r = api.migrate.aggregate(defaults=False)
        self.assertTrue(r.ok)
        d = r.detail["slots"]["default"]
        self.assertEqual(d["failed"], {})
        for i in range(3):
            self.assertEqual(api.get("k%d" % i), {"i": i})

    def test_single_slot_scope(self):
        self.seed()
        self.seed(slot="b")
        r = self.api.migrate.aggregate(slot="b", defaults=False)
        self.assertTrue(r.ok)
        self.assertEqual(sorted(r.detail["slots"]), ["b"])
        self.assertTrue(os.path.isfile(os.path.join(self.slot_dir(), "k0.json")))
        self.assertTrue(any(n.startswith("dict-") for n in os.listdir(
            os.path.join(self.root, "b"))))

    def test_two_axis_all_four_combinations(self):
        """散明->聚明->聚压->散压->散明 全程值不丢(两轴正交)。"""
        api = self.api
        self.seed(n=3)
        self.assertTrue(api.migrate.aggregate(defaults=False).ok)   # 聚明
        self.assertEqual(api.get("k1"), {"i": 1})
        self.assertTrue(any(n.endswith(".pack") and not n.endswith(".z")
                            for n in os.listdir(self.slot_dir())))
        self.assertTrue(api.migrate.compress(defaults=False).ok)    # 聚压
        self.assertEqual(api.get("k1"), {"i": 1})
        self.assertTrue(any(n.endswith(".pack.z")
                            for n in os.listdir(self.slot_dir())))
        self.assertTrue(api.migrate.scatter(defaults=False).ok)     # 散压
        self.assertEqual(api.get("k1"), {"i": 1})
        self.assertTrue(os.path.isfile(
            os.path.join(self.slot_dir(), "k1.json.z")))
        self.assertTrue(api.migrate.plain(defaults=False).ok)       # 散明
        self.assertEqual(api.get("k1"), {"i": 1})
        self.assertEqual(api.get("blob"), b"\x00\x02")
        self.assertEqual(api.get("s"), "text")

    def test_compress_on_aggregated_slot(self):
        api = self.api
        self.seed(n=2)
        api.migrate.aggregate(defaults=False)
        r = api.migrate.compress(defaults=False)
        self.assertTrue(r.ok)
        names = os.listdir(self.slot_dir())
        self.assertTrue(any(n.endswith(".pack.z") for n in names))
        self.assertFalse(any(n.endswith(".pack") and not n.endswith(".z")
                             for n in names))
        idx = self.index()
        self.assertTrue(idx["k0"].endswith(".pack.z"))
        self.assertTrue(idx["blob"].endswith(".bin.z"))
        self.assertEqual(api.get("k0"), {"i": 0})

    def test_aggregate_read_failure_aborts_before_write(self):
        """散->聚合读阶段有失败键(损坏) → 读不齐不动手——
        partial + state 不翻 + 损坏键不失明(路径仍在) + 修好后重跑完整重做。"""
        api = self.api
        api.set("a", 1)
        api.set("b", 2)
        with open(os.path.join(self.slot_dir(), "b.json"), "wb") as f:
            f.write(b"<<<corrupted>>>")
        api.refresh(flush=False)
        r = api.migrate.aggregate(defaults=False)
        self.assertFalse(r.ok)
        self.assertEqual(r.status, "partial")
        detail = r.detail["slots"]["default"]
        self.assertEqual(detail["converted"], 0)
        self.assertIn("b", detail["failed"])
        self.assertFalse(self.state()["aggregate"])      # state 未翻
        self.assertEqual(sorted(api.keys()), ["a", "b"])  # b 未失明
        names = os.listdir(self.slot_dir())
        self.assertFalse(any(n.endswith(".pack") for n in names))  # 零残像
        # 修好 b 后重跑: 完整重做成功
        api.set("b", 2)
        r2 = api.migrate.aggregate(defaults=False)
        self.assertTrue(r2.ok)
        self.assertEqual(api.get("a"), 1)
        self.assertEqual(api.get("b"), 2)


class TestAggregateMode(AggCase):
    """聚合态的写透/批量/槽位语义。"""

    def test_batch_mode_on_aggregated(self):
        api = self.api
        api.migrate.aggregate(defaults=False)
        api.batch(True)
        r = api.set("queued_k", 42)
        self.assertEqual(r.status, "queued")
        self.assertEqual(api.dirty_count(), 1)     # 未落盘
        api.flush()
        self.assertIn("queued_k", self.index())     # 片+索引已写
        self.assertEqual(api.get("queued_k"), 42)
        api.batch(False)

    def test_use_slot_and_export(self):
        api = self.api
        api.set("a", 1)
        api.migrate.aggregate(defaults=False)
        r = api.export_slot("default")
        self.assertEqual(r.data, {"a": 1})
        api.use_slot("b")
        api.migrate.aggregate(slot="b", defaults=False)
        api.set("b_key", "v")
        self.assertEqual(api.get_slot("default").get("a"), 1)
        self.assertIn("b", api.list_slots())

    def test_new_slot_inherits_aggregate_default(self):
        sg._update_defaults(self.root, {"aggregate": True})
        api2 = sg.SaveGameApi(self.root)
        api2.set("fresh", 1)
        names = os.listdir(self.slot_dir())
        self.assertTrue(any(n.endswith(".pack") for n in names))  # 出生即聚合
        self.assertEqual(api2.get("fresh"), 1)


class TestTombstone(AggCase):
    """delete 与 flush 在途并发的丢删除竞态: 墓碑机制锁死。"""

    def test_scatter_delete_during_flush(self):
        api = self.api
        slot = api._active
        api.batch(True)
        api.set("k", 1)
        entered, release = threading.Event(), threading.Event()
        original = slot._write_payload_nolock

        def slow_write(key, suffix, data):
            if key == "k":
                entered.set()
                self.assertTrue(release.wait(5))
            original(key, suffix, data)
        slot._write_payload_nolock = slow_write
        try:
            flusher = threading.Thread(target=slot.flush)
            flusher.start()
            self.assertTrue(entered.wait(5))       # flush 卡在 k 的写盘上
            r = api.delete("k")                    # 在途 delete -> 墓碑
            self.assertEqual(r.status, "removed")
            release.set()
            flusher.join(5)
        finally:
            slot._write_payload_nolock = original
        api.batch(False)
        self.assertFalse(os.path.exists(
            os.path.join(self.slot_dir(), "k.json")))    # 未复活
        self.assertFalse(os.path.exists(
            os.path.join(self.slot_dir(), "k.json.z")))
        api.refresh(flush=False)
        self.assertEqual(api.get("k", "fb"), "fb")             # 冷读缺失

    def test_concurrent_flushes_keep_inflight(self):
        """双 flush 并发: flush#2 的新键不得冲掉 flush#1 的在途保护。"""
        api = self.api
        slot = api._active
        api.batch(True)
        api.set("a", 1)
        gate = threading.Event()
        original = slot._write_payload_nolock

        def slow_a(key, suffix, data):
            if key == "a":
                gate.wait(5)
            original(key, suffix, data)
        slot._write_payload_nolock = slow_a
        try:
            f1 = threading.Thread(target=slot.flush)
            f1.start()
            deadline = __import__("time").monotonic() + 2
            while "a" not in slot._inflight and __import__("time").monotonic() < deadline:
                __import__("time").sleep(0.005)
            self.assertIn("a", slot._inflight)          # flush#1 在途
            api.set("c", 3)                              # 新键入队
            r2 = slot.flush()                            # flush#2 拿到 c(同步, 不碰 a)
            self.assertTrue(r2.ok)
            self.assertIn("a", slot._inflight)          # 未被 flush#2 覆盖
            api.delete("a")                              # 仍应打墓碑
            self.assertIn("a", slot._tombstones)
            gate.set()
            f1.join(5)
        finally:
            slot._write_payload_nolock = original
        api.batch(False)
        self.assertFalse(os.path.exists(os.path.join(self.slot_dir(), "a.json")))
        api.refresh(flush=False)
        self.assertEqual(api.get("a", "fb"), "fb")       # 未复活
        self.assertEqual(api.get("c"), 3)                # flush#2 的键正常落盘

    def test_delete_then_set_respects_last_intent(self):
        api = self.api
        slot = api._active
        api.batch(True)
        api.set("k", 1)
        entered, release = threading.Event(), threading.Event()
        original = slot._write_payload_nolock

        def slow_write(key, suffix, data):
            if key == "k":
                entered.set()
                self.assertTrue(release.wait(5))
            original(key, suffix, data)
        slot._write_payload_nolock = slow_write
        try:
            flusher = threading.Thread(target=slot.flush)
            flusher.start()
            self.assertTrue(entered.wait(5))
            api.delete("k")                          # 墓碑
            api.set("k", 99)                         # delete 后又 set: 最后意图
            release.set()
            flusher.join(5)
        finally:
            slot._write_payload_nolock = original
        r = api.flush()                              # 99 落盘
        self.assertTrue(r.ok)
        api.batch(False)
        self.assertTrue(os.path.isfile(
            os.path.join(self.slot_dir(), "k.json")))
        self.assertEqual(api.get("k"), 99)


class TestBatchWritePath(AggCase):
    """批量写路径: 同片 N 键 = 1 次片写 + 1 次索引写(O(N) 锁死)。"""

    def count_writes(self, slot):
        counts = {"pack": 0, "index": 0}
        original_pack = sg._write_pack
        original_atomic = sg._atomic_write

        def counting_pack(path, entries):
            counts["pack"] += 1
            return original_pack(path, entries)

        def counting_atomic(path, data):
            if path.endswith("index.json"):
                counts["index"] += 1
            return original_atomic(path, data)
        sg._write_pack = counting_pack
        sg._atomic_write = counting_atomic

        def restore():
            sg._write_pack = original_pack
            sg._atomic_write = original_atomic
        return counts, restore

    def test_flush_writes_pack_once(self):
        api = self.api
        slot = api._active
        api.migrate.aggregate(defaults=False)
        api.batch(True)
        for i in range(50):
            api.set("k%02d" % i, i)
        counts, restore = self.count_writes(slot)
        try:
            r = api.flush()
        finally:
            restore()
        self.assertTrue(r.ok)
        self.assertEqual(counts["pack"], 1)      # 50 键同片 = 1 次片写
        self.assertEqual(counts["index"], 1)     # 索引一次持久化
        self.assertEqual(api.get("k49"), 49)

    def test_aggregate_flush_partial_requeues(self):
        """聚合批量 flush 部分失败: 该片键整体进 failed 回 dirty 重试
        (不覆盖更新的排队值), 恢复后一次 flush 收敛。"""
        api = self.api
        slot = api._active
        api.migrate.aggregate(defaults=False)
        api.batch(True)
        api.set("a", 1)
        api.set("b", 2)                        # 同片 int-0
        original = sg._write_pack
        state = {"failed": False}

        def fail_once(path, entries):
            if not state["failed"]:
                state["failed"] = True
                raise OSError("disk full")
            return original(path, entries)

        sg._write_pack = fail_once
        try:
            r = slot.flush()
        finally:
            sg._write_pack = original
        self.assertFalse(r.ok)
        self.assertEqual(r.status, "partial")
        self.assertEqual(sorted(r.detail["failed"]), ["a", "b"])
        self.assertIn("a", slot._dirty)        # 回队(只读断言)
        self.assertIn("b", slot._dirty)
        r2 = slot.flush()                      # IO 已恢复
        self.assertTrue(r2.ok)
        api.refresh(flush=False)
        self.assertEqual(api.get("a"), 1)
        self.assertEqual(api.get("b"), 2)

    def test_flush_compressed_pack_once(self):
        api = self.api
        api.migrate.aggregate(defaults=False)
        api.migrate.compress(defaults=False)     # 压缩聚合(重写模型的受益态)
        api.batch(True)
        for i in range(50):
            api.set("s%02d" % i, "v")
        counts, restore = self.count_writes(api._active)
        try:
            r = api.flush()
        finally:
            restore()
        self.assertTrue(r.ok)
        self.assertEqual(counts["pack"], 1)
        self.assertEqual(counts["index"], 1)
        self.assertEqual(api.get("s49"), "v")

    def test_migrate_aggregate_writes_pack_once(self):
        api = self.api
        for i in range(50):
            api.set("m%02d" % i, i)              # 散态写透
        counts, restore = self.count_writes(api._active)
        try:
            r = api.migrate.aggregate(defaults=False)
        finally:
            restore()
        self.assertTrue(r.ok)
        self.assertEqual(counts["pack"], 1)      # 迁移 50 键 = 1 次片写
        self.assertEqual(api.get("m49"), 49)


class TestBatchBenchmark(AggCase):
    """宽松上限基准(防 CI 抖动): 500 键批量 flush < 200ms。
    回归 O(N^2) 时 500 键实测约 1s, 该断言必爆——性能形状的常驻守卫。"""

    def test_batch_flush_500keys_under_budget(self):
        import time as _time
        api = self.api
        api.migrate.aggregate(defaults=False)
        api.batch(True)
        for i in range(500):
            api.set("k%04d" % i, {"i": i})
        t0 = _time.perf_counter()
        r = api.flush()
        elapsed = _time.perf_counter() - t0
        self.assertTrue(r.ok)
        self.assertEqual(api.get("k0499"), {"i": 499})
        self.assertLess(elapsed, 0.2,
                        "500 键批量 flush %.0fms 超预算(疑似 O(N^2) 回归)"
                        % (elapsed * 1000))


class TestMigrateDiagnostics(AggCase):
    """迁移明细以存档键为单位(四路统一) + diagnose 四类异常。"""

    def test_compress_failed_unit_is_key(self):
        api = self.api
        api.set("good1", 1)
        api.set("bad_a", 2)
        api.set("bad_b", 3)
        api.migrate.aggregate(defaults=False)     # 3 键同进 int-0.pack
        # 损坏该槽再压: 先 plain 状态? 已是明文——直接把 int-0.pack 写坏
        with open(os.path.join(self.slot_dir(), "int-0.pack"), "wb") as f:
            f.write(b"broken-not-jsonl")
        api.refresh(flush=False)
        r = api.migrate.compress(defaults=False)
        self.assertEqual(r.status, "partial")
        d = r.detail["slots"]["default"]
        self.assertEqual(sorted(d["failed"]), ["bad_a", "bad_b", "good1"])
        for key, reason in d["failed"].items():
            self.assertIn("int-0.pack", reason)   # 原因保留片名线索

    def test_orphan_failure_silent(self):
        api = self.api
        api.set("k", 1)
        api.migrate.aggregate(defaults=False)
        # 孤儿片: 索引无主且内容损坏(转码必失败)
        with open(os.path.join(self.slot_dir(), "str-9.pack"), "wb") as f:
            f.write(b"garbage")
        r = api.migrate.compress(defaults=False)
        self.assertTrue(r.ok)                     # 孤儿失败不进报告
        self.assertEqual(r.detail["slots"]["default"]["failed"], {})
        self.assertTrue(r.ok)
        d = api.migrate.diagnose()                # 可见性由诊断承担
        orphans = [i for i in d.data["default"] if i["issue"] == "orphan_pack"]
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0]["file"], "str-9.pack")

    def test_diagnose_healthy_empty(self):
        api = self.api
        api.set("a", 1)
        api.set("b", b"x")
        r = api.migrate.diagnose()
        self.assertTrue(r.ok)
        self.assertEqual(r.data, {"default": []})

    def test_diagnose_orphan_reports_recoverable_keys(self):
        api = self.api
        api.set("k", 1)
        api.migrate.aggregate(defaults=False)
        # 孤儿片含 2 键可恢复数据
        sg._write_pack(os.path.join(self.slot_dir(), "int-7.pack"),
                       {"lost1": 10, "lost2": 20})
        r = api.migrate.diagnose()
        orphans = [i for i in r.data["default"] if i["issue"] == "orphan_pack"]
        self.assertEqual(orphans[0]["keys"], 2)   # 告知可恢复数据量

    def test_diagnose_dangling_index(self):
        api = self.api
        api.set("k", 1)
        api.migrate.aggregate(defaults=False)
        with open(os.path.join(self.slot_dir(), ".savegame", "index.json"),
                  "w", encoding="utf-8") as f:
            json.dump({"keys": {"k": "int-5.pack"}}, f)
        api.refresh(flush=False)
        r = api.migrate.diagnose()
        issues = r.data["default"]
        self.assertTrue(any(i["issue"] == "dangling_index" and
                            i["key"] == "k" for i in issues))


class TestCaseInsensitive(AggCase):
    """键空间大小写不敏感(显式契约): 归一为小写, 任意拼写互通, 跨平台一致。"""

    def test_any_spelling_interops(self):
        api = self.api
        api.set("Score", 100)
        self.assertTrue(os.path.isfile(os.path.join(self.slot_dir(), "score.json")))
        self.assertEqual(api.get("SCORE"), 100)
        self.assertEqual(api.get("score"), 100)
        self.assertEqual(api.get("ScOrE"), 100)

    def test_variant_spelling_updates_not_creates(self):
        api = self.api
        api.set("PlayerHp", 50)
        api.set("playerhp", 80)              # 变体拼写 = 更新, 不是新键
        self.assertEqual(api.get("PLAYERHP"), 80)
        self.assertEqual(api.keys(), ["playerhp"])
        files = [n for n in os.listdir(self.slot_dir()) if not n.startswith(".")]
        self.assertEqual(files, ["playerhp.json"])  # 无第二个数据文件

    def test_slot_name_normalized(self):
        api = self.api
        r = api.use_slot("SaveA")
        self.assertTrue(r.ok)
        self.assertEqual(r.detail["to"], "savea")   # detail 用归一名
        self.assertEqual(api.current_slot(), "savea")
        api.set("k", 1)                      # 触发目录创建(惰性)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "savea")))
        api.use_slot("SAVEA")                # 变体拼写切回同一槽
        self.assertEqual(api.current_slot(), "savea")
        self.assertIn("savea", api.list_slots())

    def test_aggregate_pack_entries_lowercase(self):
        api = self.api
        api.migrate.aggregate(defaults=False)
        api.set("HighScore", 999)
        with open(os.path.join(self.slot_dir(), "int-0.pack"),
                  encoding="utf-8") as f:
            content = f.read()
        self.assertIn("highscore", content)
        self.assertNotIn("HighScore", content)
        self.assertEqual(api.get("HIGHSCORE"), 999)
        idx = self.index()
        self.assertIn("highscore", idx)


class TestMigrationAndCleanupContracts(AggCase):
    """契约用例: 失败≡中断 / 清理谓词 / 风险期结算。
    铁律: 迁移断言一律 refresh 后从盘验证。"""

    def test_scatter_keeps_bytes_on_disk(self):
        """scatter 不得删 bytes 新家(新旧家同名, 清理谓词恒假)。"""
        api = self.api
        api.set("blob", bytes([0, 2]))
        api.set("k", 1)
        api.migrate.aggregate(defaults=False)
        r = api.migrate.scatter(defaults=False)
        self.assertTrue(r.ok)
        api.refresh(flush=False)                       # 从盘验证, 不走 cache
        self.assertTrue(os.path.isfile(os.path.join(self.slot_dir(), "blob.bin")))
        self.assertEqual(api.get("blob"), bytes([0, 2]))

    def test_aggregate_failure_leaves_old_world_readable(self):
        """聚合迁移部分失败 → 失败≡中断(state 不翻), 全键旧世界可读,
        重跑完整重做收敛。"""
        api = self.api
        for i in range(3):
            api.set("k%d" % i, i)
        orig_pack = sg._write_pack
        def boom(path, entries):
            raise OSError("disk full")
        sg._write_pack = boom
        try:
            r = api.migrate.aggregate(defaults=False)
        finally:
            sg._write_pack = orig_pack
        self.assertEqual(r.status, "partial")
        state_doc = json.load(open(                     # state 未翻(仍是散态)
            os.path.join(self.slot_dir(), ".savegame", "state.json"), encoding="utf-8"))
        self.assertIs(state_doc.get("aggregate"), False)
        api.refresh(flush=False)
        for i in range(3):                             # 全键旧世界可读
            self.assertEqual(api.get("k%d" % i), i)
        r2 = api.migrate.aggregate(defaults=False)     # 重跑完整重做
        self.assertTrue(r2.ok)
        api.refresh(flush=False)
        for i in range(3):
            self.assertEqual(api.get("k%d" % i), i)    # 收敛

    def test_scatter_failure_leaves_old_world_readable(self):
        """scatter 部分失败 → state 留聚合, 全键可读, 重跑收敛。"""
        api = self.api
        api.set("k1", 1)
        api.set("k2", 2)
        api.migrate.aggregate(defaults=False)
        orig_atomic = sg._atomic_write
        def selective(path, data):
            if path.endswith("k1.json"):
                raise OSError("locked")
            return orig_atomic(path, data)
        sg._atomic_write = selective
        try:
            r = api.migrate.scatter(defaults=False)
        finally:
            sg._atomic_write = orig_atomic
        self.assertEqual(r.status, "partial")
        self.assertTrue(os.path.isfile(                # 片未被清理
            os.path.join(self.slot_dir(), "int-0.pack")))
        api.refresh(flush=False)
        self.assertEqual(api.get("k1"), 1)             # 旧世界(聚合)可读
        self.assertEqual(api.get("k2"), 2)
        r2 = api.migrate.scatter(defaults=False)       # 重跑收敛
        self.assertTrue(r2.ok)
        api.refresh(flush=False)
        self.assertEqual(api.get("k1"), 1)
        self.assertEqual(api.get("k2"), 2)

    def test_tombstone_settles_at_risk_window_end(self):
        """delete 落在 flush#2 完成前(真正的交错)→ 墓碑保留,
        最后在途者(flush#1)离开时结算, a 不复活。"""
        api = self.api
        slot = api._active
        api.batch(True)
        api.set("a", 1)
        gate = threading.Event()
        original = slot._write_payload_nolock
        def slow_a(key, suffix, data):
            if key == "a":
                gate.wait(5)
            original(key, suffix, data)
        slot._write_payload_nolock = slow_a
        try:
            f1 = threading.Thread(target=slot.flush)
            f1.start()
            import time as _t
            deadline = _t.monotonic() + 2
            while "a" not in slot._inflight and _t.monotonic() < deadline:
                time.sleep(0.005)
            self.assertIn("a", slot._inflight)
            api.set("c", 3)
            api.delete("a")                            # ← 墓碑, 在 flush#2 完成前
            r2 = slot.flush()                          # flush#2: 写 c 并 try 结算
            self.assertTrue(r2.ok)
            self.assertIn("a", slot._tombstones)       # 墓碑未被 flush#2 提前清
            gate.set()
            f1.join(5)
        finally:
            slot._write_payload_nolock = original
        self.assertEqual(slot._tombstones, set())      # flush#1 结算完成
        api.batch(False)
        self.assertFalse(os.path.exists(              # a 未复活
            os.path.join(self.slot_dir(), "a.json")))
        api.refresh(flush=False)
        self.assertEqual(api.get("a", "fb"), "fb")
        self.assertEqual(api.get("c"), 3)


class TestRoutePrimitive(AggCase):
    """路由原语: "索引可再生"的统一投影——五处共用, 自愈无死角。"""

    def test_dangling_scatter_entry_self_heals(self):
        """散悬空(索引指向已删 .bin): 与 pack 悬空同构自愈, 不永 corrupt。"""
        api = self.api
        api.migrate.aggregate(defaults=False)
        api.set("img", bytes([255, 254]))
        api.set("n", 1)
        os.remove(os.path.join(self.slot_dir(), "img.bin"))
        api.refresh(flush=False)
        self.assertEqual(api.get("img", "fb"), "fb")    # 回退(自愈后发现真缺)
        with self.assertRaises(sg.SavegameError):
            api.get("img")                              # 严格=损坏语义(文件丢失)

    def test_stale_index_delete_works(self):
        """陈旧索引上的 delete: 经原语自愈后真删(不静默无效、不复活)。"""
        api = self.api
        api.migrate.aggregate(defaults=False)
        api.set("k", 1)
        # 伪造索引: 不含 k(模拟崩溃窗口: 片已写但索引未持久化)
        with open(os.path.join(self.slot_dir(), ".savegame", "index.json"),
                  "w", encoding="utf-8") as f:
            json.dump({"keys": {}}, f)
        api.refresh(flush=False)
        r = api.delete("k")                             # 原语重建→找到→真删
        self.assertEqual(r.status, "removed")
        api.refresh(flush=False)
        self.assertEqual(api.get("k", "fb"), "fb")      # 不复活

    def test_keys_self_heals_missing_index(self):
        """keys(): 索引文件缺失 → 经原语重建后正确枚举。"""
        api = self.api
        api.migrate.aggregate(defaults=False)
        api.set("a", 1)
        api.set("b", 2)
        os.remove(os.path.join(self.slot_dir(), ".savegame", "index.json"))
        api.refresh(flush=False)
        self.assertEqual(api.keys(), ["a", "b"])


class TestSupersedeProtection(AggCase):
    """新值保护(墓碑的对称投影): 在途快照不得覆灭更新意图。
    可达入口 = batch(False) 锁内开写透 -> 锁外 flush_all 尾款窗口内的并发写透 set。"""

    def test_batch_close_tail_flush_not_clobbered(self):
        """真实路径: batch(False) 尾款 flush 卡在 k 的旧快照上,
        主线程写透 set(k,new) → 旧快照被跳过 + 尾部结算重写 → 盘上=new。
        本用例把 set 压进 flush 在途窗口内。"""
        api = self.api
        slot = api._active
        api.batch(True)
        api.set("k", "old")
        gate = threading.Event()
        original = slot._write_payload_nolock

        def slow(key, suffix, data):
            if key == "k":
                gate.wait(5)
            original(key, suffix, data)

        slot._write_payload_nolock = slow
        try:
            closer = threading.Thread(target=api.batch, args=(False,))
            closer.start()
            deadline = time.monotonic() + 2
            while "k" not in slot._inflight and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertIn("k", slot._inflight)
            self.assertTrue(slot._write_through)   # 写透已开(真实窗口前提)
            r = api.set("k", "new")                # 写透 set 撞在途键
            self.assertTrue(r.ok)
            self.assertEqual(r.status, "written")
            self.assertIn("k", slot._superseded)   # 打标生效
            gate.set()
            closer.join(5)
        finally:
            slot._write_payload_nolock = original
        self.assertEqual(slot._superseded, set())  # 结算清标, 不泄漏
        api.refresh(flush=False)
        self.assertEqual(api.get("k"), "new")      # 盘上终态=最后意图

    def test_settle_rewrite_failure_enters_dirty(self):
        """结算重写失败(尾部那笔 IO 抛错) → 新值转 dirty 重试, 标记清空,
        恢复后一次 flush 落盘新值。窗口: 写循环卡在 j 上, k 的旧快照被跳过,
        结算重写 k 时注入 IO 失败。"""
        api = self.api
        slot = api._active
        api.batch(True)
        api.set("j", 0)                        # 卡门键(写循环先到)
        api.set("k", "old")
        gate = threading.Event()
        original = slot._write_payload_nolock
        k_calls = []

        def gate_j_fail_second_k(key, suffix, data):
            if key == "j":
                gate.wait(5)
            if key == "k":
                k_calls.append(1)
                if len(k_calls) >= 2:          # k 的第 2 次调用 = 结算重写
                    raise OSError("disk full on settle")
            original(key, suffix, data)

        slot._write_payload_nolock = gate_j_fail_second_k
        try:
            closer = threading.Thread(target=api.batch, args=(False,))
            closer.start()
            deadline = time.monotonic() + 2
            while "k" not in slot._inflight and time.monotonic() < deadline:
                time.sleep(0.005)
            api.set("k", "new")                # 写透: 第 1 次 k IO, 成功
            gate.set()                         # 放行 j, 写循环到 k 时跳过
            closer.join(5)
        finally:
            slot._write_payload_nolock = original
        self.assertEqual(len(k_calls), 2)                # set 写透 + 结算重写
        self.assertEqual(slot._superseded, set())        # 标记已清
        self.assertIn("k", slot._dirty)                  # 转入重试
        r = api.flush()                                  # IO 已恢复
        self.assertTrue(r.ok)
        api.refresh(flush=False)
        self.assertEqual(api.get("k"), "new")            # 重试后终态=新值


class TestPersistIOFailure(AggCase):
    """persist 失败统一走 IO 路径(函数内 try+log), 不越契约线。"""

    def test_persist_failure_does_not_break_contract(self):
        """.savegame 元数据 IO 失败(真实故障经 _atomic_write 抛出, 注入点在文件层
        ——不替换被测函数本体)→ 函数内 try 吞掉: 变更照常成功、数据已落盘、
        元数据保持旧值; IO 恢复后下一次变更补齐; migrate 不抛裸异常。"""
        api = self.api
        api.migrate.aggregate(defaults=False)
        api.set("a", 1)                       # 基线: state/index 已在盘
        self.assertIn("a", self.index())
        original = sg._atomic_write

        def fail_meta(path, data):
            if os.sep + ".savegame" + os.sep in path:
                raise OSError("disk full (.savegame)")
            original(path, data)

        sg._atomic_write = fail_meta
        try:
            r = api.set("b", 2)               # 数据文件落盘成功, persist 被吞
            self.assertTrue(r.ok)
            self.assertEqual(r.status, "written")
            self.assertTrue(os.path.isfile(
                os.path.join(self.slot_dir(), "int-0.pack")))  # 片已写
            idx = self.index()                # 元数据保持旧值(只读断言)
            self.assertIn("a", idx)
            self.assertNotIn("b", idx)
            rd = api.delete("a")              # delete 同样不被 persist 失败打断
            self.assertTrue(rd.ok)
            rm = api.migrate.scatter(defaults=False)   # migrate 不抛裸异常
            self.assertIsInstance(rm, sg.SaveResult)
        finally:
            sg._atomic_write = original
        rm2 = api.migrate.aggregate(defaults=False)  # 恢复到聚合(补写元数据)
        self.assertTrue(rm2.ok)
        api.set("c", 3)                        # IO 恢复: 下一次变更补齐全量账本
        idx2 = self.index()
        self.assertNotIn("a", idx2)
        self.assertIn("c", idx2)
        api.refresh(flush=False)
        self.assertEqual(api.get("b"), 2)
        self.assertEqual(api.get("c"), 3)


if __name__ == "__main__":
    unittest.main()
