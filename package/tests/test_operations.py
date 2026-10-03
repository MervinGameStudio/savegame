# -*- coding: utf-8 -*-
"""MCP 操作集测试: 注册/注销对称(真实 OperationRegistry) + 值投影/入参还原 +
handler 经真实 api 落盘验证。

on_editor 依赖编辑器主线程队列, 单测进程不可用——测试直接调 handler 内的
投影辅助与 _slot 路径(get_slot 真实调用), 注册表走真实 API。
"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_SG = os.path.join(_HERE, "..", "runtime", "savegame.py")
_MOD_OPS = os.path.join(_HERE, "..", "editor", "savegame_operations.py")

sg_spec = importlib.util.spec_from_file_location(
    "sg_ops_tests", os.path.abspath(_MOD_SG))
sg = importlib.util.module_from_spec(sg_spec)
sg_spec.loader.exec_module(sg)

ops_spec = importlib.util.spec_from_file_location(
    "sg_ops_mod_tests", os.path.abspath(_MOD_OPS))
ops = importlib.util.module_from_spec(ops_spec)
ops_spec.loader.exec_module(ops)

from Infernux.host.operations import (  # noqa: E402
    OperationError, OperationKind, OperationRegistry)


class OpsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.api = sg.SaveGameApi(os.path.join(self.tmp, "game"))
        self._saved = getattr(sg.inx, "savegame", None)
        sg.inx.savegame = self.api      # 模拟 preload 注入(平台条件)

    def tearDown(self):
        try:
            if self._saved is not None:
                sg.inx.savegame = self._saved
            else:
                try:
                    delattr(sg.inx, "savegame")
                except AttributeError:
                    pass
            self.api.shutdown()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def slot_dir(self):
        return os.path.join(self.tmp, "game", "default")


class TestValueProjection(OpsCase):

    def test_value_view_json(self):
        self.assertEqual(ops._value_view(700),
                         {"type": "int", "value": 700})
        self.assertEqual(ops._value_view(None),
                         {"type": "NoneType", "value": None})

    def test_value_view_bytes_head(self):
        view = ops._value_view(bytes(range(200)))
        self.assertEqual(view["type"], "bytes")
        self.assertEqual(view["size"], 200)
        self.assertEqual(len(view["head_hex"]), ops._HEAD_BYTES * 2)

    def test_value_from_arg_roundtrip(self):
        raw = {"type": "bytes", "hex": "00ff10"}
        self.assertEqual(ops._value_from_arg(raw), b"\x00\xff\x10")
        self.assertEqual(ops._value_from_arg({"type": "int", "value": 5}), 5)
        self.assertEqual(ops._value_from_arg(42), 42)
        self.assertEqual(ops._value_from_arg("plain"), "plain")


class TestHandlers(OpsCase):

    def test_slot_write_path_lands_on_disk(self):
        slot = ops._slot(self.api, "default")     # handler 的真实写路径
        r = slot.set("agent_k", {"n": 1})
        self.assertTrue(r.ok)
        with open(os.path.join(self.slot_dir(), "agent_k.json"),
                  encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"n": 1})

    def test_sg_guard_raises_without_inject(self):
        saved = sg.inx.savegame
        delattr(sg.inx, "savegame")
        try:
            with self.assertRaises(RuntimeError):
                ops._sg()
        finally:
            sg.inx.savegame = saved


class TestRegistry(OpsCase):

    OP_IDS = (
        "mervingamestudio.savegame.slots", "mervingamestudio.savegame.keys",
        "mervingamestudio.savegame.get", "mervingamestudio.savegame.stats",
        "mervingamestudio.savegame.diagnose", "mervingamestudio.savegame.set",
        "mervingamestudio.savegame.delete", "mervingamestudio.savegame.flush")

    def test_register_unregister_symmetric(self):
        registry = OperationRegistry.instance()
        self.assertEqual(ops.register_savegame_operations(), 8)
        try:
            for op_id in self.OP_IDS:
                op = registry.get(op_id)
                self.assertEqual(op.schema.id, op_id)
                # kind 必须是枚举而非字符串——字符串会在 MCP adapter 投影时
                # 炸掉('str' has no .value)并拖垮 MCP 服务启动(实测教训)
                self.assertIsInstance(op.schema.kind, OperationKind)
        finally:
            removed = ops.unregister_savegame_operations()
        self.assertEqual(removed, 8)
        with self.assertRaises(OperationError):
            registry.get("mervingamestudio.savegame.get")


if __name__ == "__main__":
    unittest.main()
