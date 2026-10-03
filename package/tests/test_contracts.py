# -*- coding: utf-8 -*-
"""语义契约测试: refresh 保光标 / stats aggregate / import 镜像 / typed
getter 三件 / diagnose 散态损坏键 / keys 投影损坏标记。全部真实路径。"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_SG = os.path.join(_HERE, "..", "runtime", "savegame.py")
_MOD_PANEL = os.path.join(_HERE, "..", "editor", "savegame_browser_panel.py")
_MOD_OPS = os.path.join(_HERE, "..", "editor", "savegame_operations.py")

sg_spec = importlib.util.spec_from_file_location(
    "sg_contracts_tests", os.path.abspath(_MOD_SG))
sg = importlib.util.module_from_spec(sg_spec)
sg_spec.loader.exec_module(sg)

panel_spec = importlib.util.spec_from_file_location(
    "panel_contracts_tests", os.path.abspath(_MOD_PANEL))
panel = importlib.util.module_from_spec(panel_spec)
panel_spec.loader.exec_module(panel)

ops_spec = importlib.util.spec_from_file_location(
    "ops_contracts_tests", os.path.abspath(_MOD_OPS))
ops = importlib.util.module_from_spec(ops_spec)
ops_spec.loader.exec_module(ops)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.api = sg.SaveGameApi(os.path.join(self.tmp, "game"))

    def tearDown(self):
        try:
            self.api.shutdown()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def slot_dir(self, slot="default"):
        return os.path.join(self.tmp, "game", slot)


class TestRefreshKeepsSlot(Case):

    def test_refresh_preserves_active_slot(self):
        self.api.use_slot("savea")
        self.api.set("k", "v")
        r = self.api.refresh()
        self.assertTrue(r.ok)
        self.assertEqual(r.detail, {})             # 无回退
        self.assertEqual(self.api.current_slot(), "savea")
        self.assertEqual(self.api.get("k"), "v")   # 值仍在

    def test_refresh_falls_back_when_slot_dir_gone(self):
        self.api.use_slot("savea")
        self.api.set("k", 1)                        # 写透: 文件已落盘
        shutil.rmtree(self.slot_dir("savea"), ignore_errors=True)
        r = self.api.refresh()
        self.assertEqual(self.api.current_slot(), "default")
        self.assertEqual(r.detail.get("fallback_from"), "savea")  # 回退可见


class TestStatsAggregate(Case):

    def test_stats_reports_aggregate_field(self):
        self.api.set("k", 1)
        self.assertFalse(self.api.stats()["aggregate"])
        self.api.migrate.aggregate(defaults=False)
        self.assertTrue(self.api.stats()["aggregate"])
        # 槽级 property
        self.assertTrue(self.api.get_slot("default").aggregate)
        self.assertTrue(self.api.get_slot("default").compress is False)


class TestImportMirror(Case):

    def test_roundtrip_removes_keys_outside_snapshot(self):
        self.api.set("keep", 1)
        self.api.set("old", 2)
        snap = self.api.export_slot("default").data   # {"keep":1,"old":2}
        # 目标槽演化: 加新键 / 删快照内键 / 改值
        self.api.set("extra", 3)
        self.api.delete("old")
        self.api.set("keep", 99)
        r = self.api.import_slot("default", snap)
        self.assertTrue(r.ok)
        self.assertEqual(r.detail["removed"], 1)      # extra 被删
        self.assertEqual(sorted(self.api.keys()), ["keep", "old"])
        self.assertEqual(self.api.get("keep"), 1)     # 值回快照
        # 盘上事实(refresh 后): extra 文件消失
        self.api.refresh(flush=False)
        self.assertFalse(os.path.exists(
            os.path.join(self.slot_dir(), "extra.json")))

    def test_roundtrip_mirror_on_aggregated_slot(self):
        self.api.set("a", 1)
        self.api.set("b", b"\x00\x02")
        self.api.migrate.aggregate(defaults=False)
        snap = self.api.export_slot("default").data
        self.api.set("c", 3)                          # 快照外新键(进片)
        r = self.api.import_slot("default", snap)
        self.assertTrue(r.ok)
        self.assertEqual(r.detail["removed"], 1)
        self.assertEqual(sorted(self.api.keys()), ["a", "b"])
        self.api.refresh(flush=False)
        self.assertNotIn("c", self.api.keys())        # 片内条目消失

    def test_precheck_rejects_without_touching(self):
        self.api.set("safe", 1)
        r = self.api.import_slot("default", {".hidden": 1, "ok": 2})
        self.assertFalse(r.ok)
        self.assertEqual(r.status, "rejected")
        self.assertEqual(self.api.keys(), ["safe"])   # 零改动
        self.assertFalse(os.path.exists(
            os.path.join(self.slot_dir(), "ok.json")))

    def test_key_case_normalized(self):
        snap = {"Score": 700}                          # 大写变体拼写
        r = self.api.import_slot("default", snap)
        self.assertTrue(r.ok)
        self.assertEqual(self.api.get("score"), 700)   # 归一为同一键
        self.assertEqual(sorted(self.api.keys()), ["score"])
        self.assertFalse(os.path.exists(
            os.path.join(self.slot_dir(), "score.json.__dup__")))

    def test_rerun_idempotent(self):
        self.api.set("x", 1)
        snap = self.api.export_slot("default").data
        self.api.set("y", 2)
        self.api.import_slot("default", snap)
        r2 = self.api.import_slot("default", snap)     # 重跑
        self.assertTrue(r2.ok)
        self.assertEqual(r2.detail["removed"], 0)
        self.assertEqual(sorted(self.api.keys()), ["x"])


class TestTypedGetters(Case):

    def test_get_list(self):
        self.api.set("inv", [1, 2])
        self.assertEqual(self.api.get_list("inv"), [1, 2])
        self.assertEqual(self.api.get_list("missing", []), [])
        self.api.set("num", 5)
        self.assertEqual(self.api.get_list("num", ["fb"]), ["fb"])  # 回退

    def test_get_dict(self):
        self.api.set("player", {"hp": 100})
        self.assertEqual(self.api.get_dict("player"), {"hp": 100})
        self.assertEqual(self.api.get_dict("missing", {}), {})
        self.api.set("num", 5)
        self.assertEqual(self.api.get_dict("num", {"d": 1}), {"d": 1})  # 回退

    def test_get_bytes(self):
        self.api.set("blob", b"\x00\xff")
        self.assertEqual(self.api.get_bytes("blob"), b"\x00\xff")
        self.api.set("num", 5)
        self.assertEqual(self.api.get_bytes("num", b"fb"), b"fb")

    def test_corrupt_falls_back(self):
        self.api.set("d", {"hp": 1})
        with open(os.path.join(self.slot_dir(), "d.json"), "wb") as f:
            f.write(b"<<<broken>>>")
        self.api.refresh(flush=False)
        self.assertEqual(self.api.get_dict("d", {"fb": 1}), {"fb": 1})


class TestDiagnoseScatter(Case):

    def test_scatter_corrupt_key_reported(self):
        self.api.set("good", 1)
        self.api.set("bad", 2)
        with open(os.path.join(self.slot_dir(), "bad.json"), "wb") as f:
            f.write(b"<<<broken>>>")
        r = self.api.migrate.diagnose()
        report = r.data["default"]
        kinds = [(i["issue"], i.get("key")) for i in report]
        self.assertIn(("corrupt_key", "bad"), kinds)
        self.assertNotIn(("corrupt_key", "good"), kinds)

    def test_aggregated_scatter_binz_reported(self):
        self.api.migrate.aggregate(defaults=False)
        self.api.set("img", b"\x00\x01")               # 散放 .bin(明文, 不检)
        self.api.set("zdata", b"\x00\x02")
        # 手工构造散放 .bin.z 损坏(索引认领的合法散文件)
        self.api.migrate.compress(defaults=False)      # 全槽压缩
        self.api.refresh(flush=False)
        found = [f for f in os.listdir(self.slot_dir())
                 if f.endswith(".bin.z")]
        self.assertTrue(found)
        with open(os.path.join(self.slot_dir(), found[0]), "wb") as f:
            f.write(b"broken-zlib")
        r = self.api.migrate.diagnose()
        report = r.data["default"]
        self.assertTrue(any(i["issue"] == "corrupt_key" for i in report))


class TestPanelHelpers(Case):

    def test_initial_slot_follows_game(self):
        self.assertEqual(panel.initial_slot_name(self.api), "default")
        self.api.use_slot("ingame7")
        self.assertEqual(panel.initial_slot_name(self.api), "ingame7")
        self.assertEqual(panel.initial_slot_name(None), "default")

    def test_perform_refresh_clears_selection(self):
        sg_mod = panel.inx
        sg_mod.savegame = self.api
        try:
            self.api.set("k", 1)
            p = panel.SavegameBrowserPanel()
            p._selected = "k"
            p._edit_buf = "seed"
            panel.perform_refresh(p, self.api)
            self.assertIsNone(p._selected)
            self.assertEqual(p._edit_buf, "")
        finally:
            delattr(sg_mod, "savegame")


class TestKeysProjection(Case):

    def test_corrupt_marked_distinctly(self):
        self.api.set("num", 3)
        self.api.set("bad", 4)
        with open(os.path.join(self.slot_dir(), "bad.json"), "wb") as f:
            f.write(b"<<<broken>>>")
        self.api.refresh(flush=False)
        slot = self.api.get_slot("default")
        rows = {key: ops._key_row(key, slot.get(key, ops._UNREADABLE))
                for key in slot.keys()}
        self.assertEqual(rows["num"]["type"], "int")
        self.assertEqual(rows["bad"]["type"], "corrupt")


if __name__ == "__main__":
    unittest.main()
