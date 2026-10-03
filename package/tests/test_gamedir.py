# -*- coding: utf-8 -*-
"""目录名与收养测试: _game_dir 跟随 BuildSettings.game_name(产品名权威,
编辑器/Player 同源; Player 兜底 exe 名, 编辑器兜底工程文件夹名)。
真实路径: 临时工程真实落盘 BuildSettings.json, 函数真实读盘。"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_SG = os.path.join(_HERE, "..", "runtime", "savegame.py")

sg_spec = importlib.util.spec_from_file_location(
    "sg_gamedir_tests", os.path.abspath(_MOD_SG))
sg = importlib.util.module_from_spec(sg_spec)
sg_spec.loader.exec_module(sg)


def _make_project(game_name):
    root = tempfile.mkdtemp(prefix="mgs_sg_")
    if game_name is not None:
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(ps)
        with open(os.path.join(ps, "BuildSettings.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"game_name": game_name}, f)
    return root


class GameDirTests(unittest.TestCase):

    def setUp(self):
        self._roots = []

    def tearDown(self):
        for r in self._roots:
            shutil.rmtree(r, ignore_errors=True)

    def _ctx(self, root, runtime):
        self._roots.append(root)
        return types.SimpleNamespace(project_root=root, runtime=runtime)

    def test_editor_domain_follows_game_name(self):
        root = _make_project("MyGame")
        self.assertEqual(sg._game_dir(self._ctx(root, False)), "MyGame.editor")

    def test_player_domain_follows_game_name(self):
        root = _make_project("MyGame")
        self.assertEqual(sg._game_dir(self._ctx(root, True)), "MyGame")

    def test_player_falls_back_to_exe_when_no_settings(self):
        root = _make_project(None)
        expected = sg._safe_dir(
            os.path.splitext(os.path.basename(sys.executable or ""))[0] or "game")
        self.assertEqual(sg._game_dir(self._ctx(root, True)), expected)

    def test_editor_falls_back_to_project_folder(self):
        root = _make_project(None)
        base = os.path.join(os.path.dirname(root), "FallbackProj")
        os.rename(root, base)
        self._roots.append(base)
        self.assertEqual(sg._game_dir(self._ctx(base, False)), "FallbackProj.editor")

    def test_empty_game_name_engages_fallback(self):
        root = _make_project("")
        base = os.path.join(os.path.dirname(root), "EmptyProj")
        os.rename(root, base)
        self._roots.append(base)
        self.assertEqual(sg._game_dir(self._ctx(base, False)), "EmptyProj.editor")


class LegacyPrivateDirAdoptionTests(unittest.TestCase):
    """目录名收养: 存在 .inx 且无 .savegame 时整体改名(真实路径:
    临时盘上造 .savegame 缺席的布局, api 构造触发收养, 断言改名且事实保留)。"""

    def setUp(self):
        self._root = tempfile.mkdtemp(prefix="mgs_sg_")

    def tearDown(self):
        shutil.rmtree(self._root, ignore_errors=True)

    def _make_legacy(self, game_state=None, slot_state=None):
        game_legacy = os.path.join(self._root, ".inx")
        os.makedirs(game_legacy)
        with open(os.path.join(game_legacy, "defaults.json"), "w",
                  encoding="utf-8") as f:
            json.dump(game_state or {"compress": False}, f)
        slot_legacy = os.path.join(self._root, "default", ".inx")
        os.makedirs(slot_legacy)
        with open(os.path.join(slot_legacy, "state.json"), "w",
                  encoding="utf-8") as f:
            json.dump(slot_state or {"compress": True, "aggregate": False}, f)
        with open(os.path.join(self._root, "default", "score.json"), "w",
                  encoding="utf-8") as f:
            f.write("700")

    def test_api_init_adopts_game_and_active_slot(self):
        self._make_legacy()
        api = sg.SaveGameApi(self._root)
        self.assertTrue(os.path.isdir(os.path.join(self._root, ".savegame")))
        self.assertTrue(os.path.isdir(
            os.path.join(self._root, "default", ".savegame")))
        self.assertFalse(os.path.exists(os.path.join(self._root, ".inx")))
        self.assertFalse(os.path.exists(
            os.path.join(self._root, "default", ".inx")))
        # 事实随目录迁移: 槽级 compress=True 生效(裸 .json 不在, .json.z 在)
        self.assertTrue(api.get_slot("default").compress)
        self.assertEqual(api.get("score"), 700)

    def test_adopt_skips_when_modern_exists(self):
        self._make_legacy()
        # 人工已建新目录: 收养让路, 旧目录原地保留
        os.makedirs(os.path.join(self._root, ".savegame"))
        sg.SaveGameApi(self._root)
        self.assertTrue(os.path.isdir(os.path.join(self._root, ".inx")))
        self.assertTrue(os.path.isdir(os.path.join(self._root, ".savegame")))

    def test_fresh_root_untouched(self):
        api = sg.SaveGameApi(self._root)
        self.assertFalse(os.path.exists(os.path.join(self._root, ".inx")))
        api.set("k", 1)
        self.assertTrue(os.path.isdir(
            os.path.join(self._root, "default", ".savegame")))


if __name__ == "__main__":
    unittest.main()
