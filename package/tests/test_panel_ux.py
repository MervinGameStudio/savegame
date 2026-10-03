# -*- coding: utf-8 -*-
"""面板 UX 内核测试: 布局判定/过滤/排序/虚拟窗口等纯函数。渲染层由人工验收覆盖。"""
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PANEL = os.path.join(_HERE, "..", "editor", "savegame_browser_panel.py")

panel_spec = importlib.util.spec_from_file_location(
    "panel_ux_tests", os.path.abspath(_MOD_PANEL))
panel = importlib.util.module_from_spec(panel_spec)
panel_spec.loader.exec_module(panel)


class LayoutColumnsTests(unittest.TestCase):

    def test_wide_window_side_by_side(self):
        self.assertTrue(panel.layout_columns(800.0))
        self.assertTrue(panel.layout_columns(panel.SIDE_BY_SIDE_MIN_WIDTH))

    def test_narrow_window_stacks(self):
        self.assertFalse(panel.layout_columns(400.0))
        self.assertFalse(panel.layout_columns(panel.SIDE_BY_SIDE_MIN_WIDTH - 0.1))


class ViewKernelsTests(unittest.TestCase):
    """视图内核: 过滤/排序/虚拟窗口/大小度量。"""

    ROWS = [
        ("alpha", 1, ""),
        ("beta", b"", ""),
        ("gamma", {"k": 2}, "*"),
        ("delta", None, "DEL"),
    ]

    def test_filter_staged_only(self):
        out = panel.filter_rows(self.ROWS, "", True, "all")
        self.assertEqual([r[0] for r in out], ["gamma", "delta"])

    def test_filter_type_bytes(self):
        out = panel.filter_rows(self.ROWS, "", False, "bytes")
        self.assertEqual([r[0] for r in out], ["beta"])

    def test_filter_type_json_excludes_bytes_none_del(self):
        out = panel.filter_rows(self.ROWS, "", False, "json")
        self.assertEqual([r[0] for r in out], ["alpha", "gamma"])

    def test_filter_needle(self):
        out = panel.filter_rows(self.ROWS, "am", False, "all")
        self.assertEqual([r[0] for r in out], ["gamma"])

    def test_sort_by_size_desc(self):
        out = panel.sort_rows(self.ROWS, "size", False)
        # 降序: gamma(8字符) > delta("null"=4) > beta(2字节) > alpha(1字符)
        self.assertEqual([r[0] for r in out], ["gamma", "delta", "beta", "alpha"])

    def test_sort_by_type_groups(self):
        out = panel.sort_rows(self.ROWS, "type", True)
        types = [type(r[1]).__name__ for r in out]
        self.assertEqual(types, sorted(types))

    def test_visible_slice_bounds(self):
        start, end = panel.visible_slice(5000, 0.0, 300.0)
        self.assertEqual(start, 0)
        self.assertLessEqual(end, 30)
        start, end = panel.visible_slice(5000, 2600.0, 300.0)
        self.assertGreaterEqual(start, 98)
        self.assertLessEqual(end - start, 30)
        self.assertLessEqual(end, 5000)

    def test_visible_slice_empty(self):
        self.assertEqual(panel.visible_slice(0, 0.0, 300.0), (0, 0))

    def test_value_size_bytes_vs_text(self):
        self.assertEqual(panel.value_size(b"ab"), 2)
        self.assertEqual(panel.value_size(700), 3)


if __name__ == "__main__":
    unittest.main()
