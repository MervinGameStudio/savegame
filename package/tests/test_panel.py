# -*- coding: utf-8 -*-
"""面板纯逻辑测试: 预览/解析/叠合视图/pending 落库(真实槽, 盘上验证) + i18n 词条。
模块 import 副作用检查: @editor_panel 在无 WindowManager 环境只收集不炸。"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_SG = os.path.join(_HERE, "..", "runtime", "savegame.py")
_MOD_PANEL = os.path.join(_HERE, "..", "editor", "savegame_browser_panel.py")
_TRANSLATIONS = os.path.join(_HERE, "..", "editor", "translations.json")

sg_spec = importlib.util.spec_from_file_location(
    "sg_panel_tests", os.path.abspath(_MOD_SG))
sg = importlib.util.module_from_spec(sg_spec)
sg_spec.loader.exec_module(sg)

panel_spec = importlib.util.spec_from_file_location(
    "sg_browser_panel_tests", os.path.abspath(_MOD_PANEL))
panel = importlib.util.module_from_spec(panel_spec)
panel_spec.loader.exec_module(panel)

from Infernux.engine.i18n import (  # noqa: E402
    get_locale, register_translation_catalog, t,
    unregister_translation_catalog, validate_translation_catalog)


class PanelCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.api = sg.SaveGameApi(os.path.join(self.tmp, "game"))

    def tearDown(self):
        try:
            self.api.shutdown()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def slot(self):
        return self.api.get_slot("default")

    def slot_dir(self):
        return os.path.join(self.tmp, "game", "default")


class TestPreview(PanelCase):

    def test_type_previews(self):
        self.assertEqual(panel.format_preview(700), "700")
        self.assertEqual(panel.format_preview("hi"), '"hi"')
        self.assertEqual(panel.format_preview({"a": 1}), '{"a": 1}')
        self.assertEqual(panel.format_preview(True), "true")
        self.assertEqual(panel.format_preview(None), "null")
        # bytes 预览走词条(随引擎 locale)
        self.assertEqual(panel.format_preview(b"\x00\x01"),
                         t("savegame.ui.bin_bytes").format(2))

    def test_long_value_truncated(self):
        text = "x" * 100
        out = panel.format_preview(text)
        self.assertTrue(out.endswith("..."))
        self.assertEqual(len(out), panel.PREVIEW_LIMIT)


class TestParse(PanelCase):

    def test_json_types_restored(self):
        for raw, want in [("700", 700), ('"s"', "s"), ("true", True),
                          ("null", None), ("[1,2]", [1, 2]),
                          ('{"k": 1}', {"k": 1}), ("1.5", 1.5)]:
            value, error = panel.parse_value_text(raw)
            self.assertEqual(error, "")
            self.assertEqual(value, want)
            self.assertIs(type(value), type(want))

    def test_plain_text_falls_back_to_string(self):
        value, error = panel.parse_value_text("hello world")
        self.assertEqual((value, error), ("hello world", ""))

    def test_empty_rejected(self):
        value, error = panel.parse_value_text("   ")
        self.assertNotEqual(error, "")


class TestOverlay(PanelCase):

    def test_unchanged_tag_empty(self):
        self.api.set("a", 1)
        rows = panel.overlay_rows(
            self.slot().keys(), self._getter(), {})
        self.assertEqual(rows, [("a", 1, "")])

    def test_pending_marks(self):
        self.api.set("a", 1)
        self.api.set("b", 2)
        pending = {}
        panel.pending_set(pending, "a", 99)       # modified
        panel.pending_delete(pending, "b")        # delete staged
        panel.pending_set(pending, "c", "new")    # new key
        rows = panel.overlay_rows(
            self.slot().keys(), self._getter(), pending)
        self.assertEqual(
            rows, [("a", 99, "*"), ("b", None, "DEL"), ("c", "new", "*")])

    def test_corrupt_key_shows_marker(self):
        self.api.set("k", 1)
        path = os.path.join(self.slot_dir(), "k.json")
        with open(path, "wb") as f:
            f.write(b"<<<broken>>>")
        self.api.refresh(flush=False)
        rows = panel.overlay_rows(
            self.slot().keys(), self._getter(), {})
        self.assertEqual(rows, [("k", panel.CORRUPT, "")])

    def _getter(self):
        slot = self.slot()
        def read(key):
            return slot.get(key, panel.CORRUPT)
        return read


class TestPendingApply(PanelCase):

    def test_apply_writes_through_public_api(self):
        self.api.set("old", 1)
        slot = self.slot()
        pending = {}
        panel.pending_set(pending, "old", 42)      # modify
        panel.pending_set(pending, "new", "v")     # add
        applied, error = panel.pending_apply(slot, pending)
        self.assertEqual((applied, error), (2, ""))
        self.assertEqual(pending, {})              # flushed
        # 盘上事实(文件即值): 修改与新增都真实落盘
        with open(os.path.join(self.slot_dir(), "old.json")) as f:
            self.assertEqual(f.read().strip(), "42")
        self.assertTrue(os.path.isfile(
            os.path.join(self.slot_dir(), "new.json")))
        self.assertEqual(self.api.get("old"), 42)

    def test_apply_delete_removes_file(self):
        self.api.set("gone", 5)
        slot = self.slot()
        pending = {}
        panel.pending_delete(pending, "gone")
        applied, error = panel.pending_apply(slot, pending)
        self.assertEqual((applied, error), (1, ""))
        self.assertFalse(os.path.exists(
            os.path.join(self.slot_dir(), "gone.json")))
        self.assertEqual(self.api.get("gone", "fb"), "fb")

    def test_apply_rejects_invalid_key_via_library(self):
        slot = self.slot()
        pending = {}
        panel.pending_set(pending, ".hidden", 1)   # 点开头 = 库拒绝
        applied, error = panel.pending_apply(slot, pending)
        self.assertEqual(applied, 0)
        self.assertIn(".hidden", error)
        self.assertEqual(pending, {})              # 拒绝键也被清出缓冲


class TestTranslations(PanelCase):
    """i18n 词条: 引擎校验器验合法性 + register/t/unregister 对称(真实 API)。"""

    CATALOG_OWNER = "savegame-tests"

    def _document(self):
        with open(_TRANSLATIONS, encoding="utf-8") as f:
            return json.load(f)

    def test_catalog_passes_engine_validator(self):
        """引擎 validate_translation_catalog: 键集一致/无引擎键冲突/非空值。"""
        doc = self._document()
        identity, candidate = validate_translation_catalog(
            "mervingamestudio/savegame", doc)          # 只验证, 不发布
        self.assertEqual(identity, "mervingamestudio/savegame")
        self.assertEqual(set(candidate), set(doc["locales"]))

    def test_register_translates_and_unregister_reverts(self):
        doc = self._document()
        # 用独立 owner 注册同一份词条(避免与真实 owner 冲突)
        probe_doc = {"$schema": doc["$schema"],
                     "locales": doc["locales"]}
        register_translation_catalog(self.CATALOG_OWNER, probe_doc)
        try:
            locale = get_locale()
            want = probe_doc["locales"][locale]["panel.savegame_browser"]
            self.assertEqual(t("panel.savegame_browser"), want)
        finally:
            unregister_translation_catalog(self.CATALOG_OWNER)
        self.assertNotEqual(t("panel.savegame_browser"),
                            probe_doc["locales"][get_locale()]
                            ["panel.savegame_browser"])


if __name__ == "__main__":
    unittest.main()
