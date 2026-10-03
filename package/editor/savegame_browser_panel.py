# -*- coding: utf-8 -*-
"""Savegame Browser — editor panel for browsing / editing savegame slots.

Architecture rule: this panel is a pure projection + staging layer.  Every
read and write goes through the public ``inx.savegame`` API
(``list_slots / get_slot / get / set / delete / keys / stats``); the panel
never touches files directly.

Edit semantics (by design):
- Edits are staged in a local ``pending`` buffer; nothing reaches disk until
  the user clicks Save and confirms the modal warning (saving writes the
  editor save pool immediately — a running Play session reads the changes
  at once).
- Slot browsing uses an independent Slot object (``get_slot``); the game's
  global slot cursor is never moved by this panel.
- bytes values are read-only here (preview + delete); edit them as files.

Layout: pinned toolbar → master/detail two-column body (keys table
left, editor + staged list right; stacks vertically when narrow) → pinned
footer with Discard/Save always visible.  Theme colors map row states
(staged/deleted/corrupt); notices are colored and auto-fade; large key sets
render through a virtualization window.  UI text follows the Editor locale
via ``editor/translations.json``.
"""
from __future__ import annotations

import json
import time

import infernux as inx

from Infernux.engine.i18n import t
from Infernux.engine.interaction import PanelInteractionDescriptor
from Infernux.engine.ui.editor_panel import EditorPanel
from Infernux.engine.ui.panel_registry import editor_panel
from Infernux.engine.ui.theme import ImGuiCol, Theme

# ---------------------------------------------------------------------------
# Pure logic (unit-testable, no GUI dependency)
# ---------------------------------------------------------------------------

CORRUPT = "<corrupt>"          # sentinel value; display text goes through t()
PREVIEW_LIMIT = 48
SIDE_BY_SIDE_MIN_WIDTH = 620.0   # 窄于此值上下堆叠, 宽于此值主从双栏
FOOTER_HEIGHT = 46.0
SEARCHABLE_SLOT_THRESHOLD = 8    # 槽位数超过则切换可搜索下拉
VIRTUALIZE_MIN_ROWS = 200        # 行数超过才启用滚动窗口切片
ROW_HEIGHT_ESTIMATE = 26.0       # 行高估算(按钮+间距), 切片仅用它
NOTICE_FADE_SECONDS = 4.0
SORT_BY_KEYS = ("key", "type", "size")
TYPE_FILTERS = ("all", "json", "bytes")


def initial_slot_name(sg):
    """打开面板时落在游戏当前活动槽(纯浏览跟随, 不回写); 取不到回 default。"""
    try:
        return str(sg.current_slot() or "default")
    except Exception:
        return "default"


def perform_refresh(panel, sg):
    """Refresh 的可测内核: 库级 refresh(保留活动槽光标) + 清面板选中态
    (选中的键可能已被外部删除)。pending 守卫在按钮层。"""
    result = sg.refresh()
    panel._selected = None
    panel._edit_buf = ""
    return result


def format_preview(value, limit=PREVIEW_LIMIT):
    """Value -> short preview text.  bytes -> size note; unserializable -> repr."""
    if isinstance(value, (bytes, bytearray)):
        return t("savegame.ui.bin_bytes").format(len(value))
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = repr(value)
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def parse_value_text(text):
    """Edit-box text -> (value, error).  JSON first; plain text falls back to
    a raw string so typing `hello` does not explode."""
    text = text.strip()
    if not text:
        return None, t("savegame.ui.empty_value")
    try:
        return json.loads(text), ""
    except ValueError:
        return text, ""


def overlay_rows(keys, getter, pending):
    """Compose the view: library truth overlaid with pending changes.

    Returns a sorted list of (key, value_or_None, tag) where tag is
    '' unchanged / '*' modified-or-new / 'DEL' pending delete.
    ``getter`` must be the fallback-mode read (never raises)."""
    keyset = set(keys)
    rows = {}
    for key in keys:
        op = pending.get(key)
        if op is not None and op[0] == "del":
            rows[key] = (None, "DEL")
            continue
        if op is not None and op[0] == "set":
            rows[key] = (op[1], "*")
        else:
            rows[key] = (getter(key), "")
    for key, op in pending.items():          # staged new keys
        if key not in keyset and op[0] == "set":
            rows[key] = (op[1], "*")
    return [(k, rows[k][0], rows[k][1]) for k in sorted(rows)]


def pending_set(pending, key, value):
    pending[key] = ("set", value)


def pending_delete(pending, key):
    pending[key] = ("del",)


def pending_apply(slot, pending):
    """Flush staged changes through the library API.  Returns (applied, error)
    where applied counts writes+deletes and error carries the first failure."""
    applied = 0
    first_error = ""
    for key in sorted(pending):
        op = pending[key]
        try:
            if op[0] == "set":
                result = slot.set(key, op[1])
                if not result.ok:
                    reason = (result.detail.get("reason", result.status)
                              if result.detail else result.status)
                    first_error = first_error or "%s: %s" % (key, reason)
                    continue
            else:
                slot.delete(key)
            applied += 1
        except Exception as exc:              # IO failure = IO failure
            first_error = first_error or "%s: %s" % (key, exc)
    pending.clear()
    return applied, first_error


def layout_columns(avail_width):
    """True=主从双栏, False=上下堆叠(窄窗口)。纯函数可测。"""
    return avail_width >= SIDE_BY_SIDE_MIN_WIDTH


def value_size(value):
    """排序用大小度量: bytes 按字节, 其他按序列化文本长。"""
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return len(repr(value))


def filter_rows(rows, needle, staged_only, type_filter):
    """rows -> 过滤视图。type_filter: 'all'|'json'|'bytes'; needle 子串。"""
    out = []
    for key, value, tag in rows:
        if staged_only and not tag:
            continue
        if type_filter == "json" and (isinstance(value, (bytes, bytearray))
                                      or value is None or value == CORRUPT):
            continue
        if type_filter == "bytes" and not isinstance(value, (bytes, bytearray)):
            continue
        if needle and needle not in key.lower():
            continue
        out.append((key, value, tag))
    return out


def sort_rows(rows, by, ascending):
    """rows -> 排序副本。by: 'key'|'type'|'size'。"""
    if by == "type":
        keyfn = lambda r: (type(r[1]).__name__, r[0])
    elif by == "size":
        keyfn = lambda r: value_size(r[1])
    else:
        keyfn = lambda r: r[0]
    return sorted(rows, key=keyfn, reverse=not ascending)


def visible_slice(total, scroll_y, view_height,
                  row_h=ROW_HEIGHT_ESTIMATE, pad=2):
    """虚拟化窗口: 返回 [start, end) 行区间, 覆盖可视区上下各 pad 行。"""
    if total <= 0 or view_height <= 0:
        return 0, 0
    start = max(0, int(scroll_y // row_h) - pad)
    visible = int(view_height // row_h) + pad * 2 + 1
    end = min(total, start + visible)
    return start, end


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

@editor_panel(
    "Savegame Browser",
    title_key="panel.savegame_browser",
    menu_path="Window",
    menu_path_keys=("menu.window",),
    interaction=PanelInteractionDescriptor(),
)
class SavegameBrowserPanel(EditorPanel):
    """Browse / stage edits / save — the butler's shop window."""

    def __init__(self):
        # WindowManager 的默认工厂无参构造——title 必须自带
        super().__init__("Savegame Browser")
        sg = getattr(inx, "savegame", None)
        self._slot_name = initial_slot_name(sg) if sg is not None \
            else "default"
        self._pending = {}
        self._search = ""
        self._selected = None
        self._edit_buf = ""
        self._new_key = ""
        self._new_val = ""
        # 通知: (kind, text), kind in info/ok/warn/error; 超时自动消隐
        self._notice_kind = ""
        self._notice_text = ""
        self._notice_at = 0.0
        # 视图控制
        self._staged_only = False
        self._type_filter = 0
        self._sort_by = 0
        self._sort_asc = True

    # -- hooks ----------------------------------------------------------------

    @staticmethod
    def _initial_size():
        return (760, 560)

    # -- helpers ---------------------------------------------------------------

    def _say(self, kind, text):
        self._notice_kind = kind
        self._notice_text = text
        self._notice_at = time.time()

    def _clear_say(self):
        self._notice_kind = ""
        self._notice_text = ""

    def _slot(self, sg):
        try:
            return sg.get_slot(self._slot_name)
        except Exception:
            return None

    def _getter(self, slot):
        def read(key):
            try:
                return slot.get(key, CORRUPT)
            except Exception:
                return CORRUPT
        return read

    @staticmethod
    def _type_name(value):
        if value == CORRUPT:
            return "?"
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, (bytes, bytearray)):
            return "bytes"
        return type(value).__name__

    @staticmethod
    def _colored(ctx, text, rgba):
        ctx.push_style_color(ImGuiCol.Text, *rgba)
        ctx.label(text)
        ctx.pop_style_color(1)

    @staticmethod
    def _tooltip(ctx, key):
        ctx.set_tooltip(t(key))

    # -- render -----------------------------------------------------------------

    def on_render_content(self, ctx):
        sg = getattr(inx, "savegame", None)
        if sg is None:
            self._render_empty_state(ctx, t("savegame.ui.not_loaded"))
            return
        slot = self._slot(sg)
        if slot is None:
            self._render_empty_state(
                ctx, t("savegame.ui.slot_unavailable").format(self._slot_name))
            return

        self._render_toolbar(ctx, sg, slot)
        body_h = ctx.get_content_region_avail_height() - FOOTER_HEIGHT
        avail_w = ctx.get_content_region_avail_width()
        if layout_columns(avail_w):
            left_w = avail_w * 0.62 - 4.0
            right_w = avail_w - left_w - 8.0
            self._render_table_child(ctx, slot, left_w, body_h)
            ctx.same_line()
            self._render_detail_child(ctx, slot, right_w, body_h)
        else:
            self._render_table_child(ctx, slot, 0.0, body_h * 0.55)
            self._render_detail_child(ctx, slot, 0.0, body_h * 0.45)
        self._render_footer(ctx, slot)

    def _render_empty_state(self, ctx, hint):
        # 引擎空态基座(居中虚线框); 不可用时退化为换行文本
        try:
            EditorPanel._render_empty_state(self, ctx, hint)
        except Exception:
            ctx.text_wrapped(hint)

    def _render_toolbar(self, ctx, sg, slot):
        slots = []
        try:
            slots = list(sg.list_slots())
        except Exception:
            pass
        if self._slot_name not in slots:
            slots = sorted(set(slots) | {self._slot_name})
        picked = self._pick_slot(ctx, slots)
        if ctx.is_item_hovered():
            self._tooltip(ctx, "savegame.ui.tt_slot")
        if slots[picked] != self._slot_name:
            if self._pending:
                self._say("warn", t("savegame.ui.unsaved_switch"))
            else:
                self._slot_name = slots[picked]
                self._selected = None
                self._clear_say()
        ctx.same_line()
        if ctx.button(t("savegame.ui.refresh")):
            if self._pending:
                self._say("warn", t("savegame.ui.unsaved_refresh"))
            else:
                perform_refresh(self, sg)
                self._clear_say()
        ctx.same_line()
        self._staged_only = ctx.checkbox(
            t("savegame.ui.filter_staged_only"), self._staged_only)
        # 第二行: 键名过滤 + 类型筛选 + 排序 同排。可设宽控件全部显式
        # 定宽(引擎惯例: 该绑定的默认宽度不可依赖), 杜绝宽度漂移
        ctx.set_next_item_width(240.0)
        self._search = ctx.input_text_with_hint(
            "##search", t("savegame.ui.filter_hint"), self._search, 128)
        ctx.same_line()
        ctx.set_next_item_width(140.0)
        type_labels = [t("savegame.ui.filter_type_all"),
                       t("savegame.ui.filter_type_json"),
                       t("savegame.ui.filter_type_bytes")]
        self._type_filter = ctx.combo("##typef", self._type_filter, type_labels)
        ctx.same_line()
        ctx.set_next_item_width(100.0)
        sort_labels = [t("savegame.ui.sort_key"), t("savegame.ui.col_type"),
                       t("savegame.ui.sort_size")]
        self._sort_by = ctx.combo("##sortby", self._sort_by, sort_labels)
        ctx.same_line()
        dir_label = (t("savegame.ui.sort_asc") if self._sort_asc
                     else t("savegame.ui.sort_desc"))
        if ctx.button(dir_label):
            self._sort_asc = not self._sort_asc
        self._render_meta_line(ctx, sg, slot)

    def _pick_slot(self, ctx, slots):
        """槽位选择: 少于阈值用普通 combo, 超过切可搜索下拉。"""
        current = slots.index(self._slot_name) if self._slot_name in slots else 0
        if len(slots) <= SEARCHABLE_SLOT_THRESHOLD:
            ctx.set_next_item_width(150.0)
            return ctx.combo("##slot", current, slots)
        try:
            return ctx.searchable_combo(
                "##slot_search", current, slots, 0, 8,
                t("savegame.ui.slot_search_hint"), "")
        except Exception:
            return ctx.combo("##slot", current, slots)

    def _render_meta_line(self, ctx, sg, slot):
        # 槽名(正常) / 三枚状态徽标(暗色) / 键数(暗色)
        try:
            st = sg.stats()
            mode = (t("savegame.ui.mode_batch") if st.get("batch_mode")
                    else t("savegame.ui.mode_write_through"))
            badges = [
                (t("savegame.ui.mode_compressed") if slot.compress
                 else t("savegame.ui.mode_plain")),
                (t("savegame.ui.mode_aggregated") if slot.aggregate
                 else t("savegame.ui.mode_scattered")),
                mode,
            ]
        except Exception:
            badges = ["?"]
        ctx.label("%s: %s" % (t("savegame.ui.slot"), self._slot_name))
        for badge in badges:
            ctx.same_line()
            self._colored(ctx, "[%s]" % badge, Theme.TEXT_DIM)
        try:
            count = len(slot.keys())
        except Exception:
            count = -1
        if count >= 0:
            ctx.same_line()
            self._colored(ctx, t("savegame.ui.badge_keys").format(count),
                          Theme.TEXT_DIM)

    def _table_rows(self, slot):
        keys = list(slot.keys())
        rows = overlay_rows(keys, self._getter(slot), self._pending)
        rows = filter_rows(rows, self._search.strip().lower(),
                           self._staged_only, TYPE_FILTERS[self._type_filter])
        rows = sort_rows(rows, SORT_BY_KEYS[self._sort_by], self._sort_asc)
        return rows

    def _render_table_child(self, ctx, slot, width, height):
        if not ctx.begin_child("##keys", width, height, True):
            ctx.end_child()
            return
        try:
            try:
                rows = self._table_rows(slot)
            except Exception:
                ctx.text_wrapped(t("savegame.ui.keys_failed"))
                return
            if not rows:
                self._render_empty_state(ctx, t("savegame.ui.keys_empty"))
                return
            # 虚拟化: 大键集只渲染可视窗口
            view_h = ctx.get_content_region_avail_height()
            start, end = 0, len(rows)
            if len(rows) > VIRTUALIZE_MIN_ROWS:
                start, end = visible_slice(
                    len(rows), ctx.get_scroll_y(), view_h)
            if ctx.begin_table("##keys_table", 3, 0, 0.0):
                ctx.table_setup_column(t("savegame.ui.col_key"), 0, 0.0, 0)
                ctx.table_setup_column(t("savegame.ui.col_type"), 0, 0.0, 0)
                ctx.table_setup_column(t("savegame.ui.col_value"), 0, 0.0, 0)
                ctx.table_headers_row()
                for key, value, tag in rows[start:end]:
                    self._render_table_row(ctx, key, value, tag)
                if start > 0 or end < len(rows):
                    ctx.table_next_column()
                    self._colored(
                        ctx, "... %d-%d / %d ..." % (start + 1, end, len(rows)),
                        Theme.TEXT_DIM)
                ctx.end_table()
        finally:
            ctx.end_child()

    def _render_table_row(self, ctx, key, value, tag):
        ctx.table_next_column()
        label = (t("savegame.ui.mark_modified").format(key) if tag == "*"
                 else key)
        if tag == "DEL":
            self._colored(ctx,
                          t("savegame.ui.delete_staged").format(key),
                          Theme.TEXT_DISABLED)
        else:
            if tag == "*":
                ctx.push_style_color(ImGuiCol.Text, *Theme.WARNING_TEXT)
            clicked = ctx.button(label)
            if tag == "*":
                ctx.pop_style_color(1)
            if clicked:
                self._selected = key
                self._edit_buf = _edit_seed(value)
                self._clear_say()
            self._render_row_menu(ctx, key, value, tag)
        ctx.table_next_column()
        if isinstance(value, (bytes, bytearray)):
            self._colored(ctx, self._type_name(value), Theme.TEXT_DIM)
        else:
            ctx.label(self._type_name(value))
        ctx.table_next_column()
        if value == CORRUPT:
            self._colored(ctx, _preview_text(value), Theme.ERROR_TEXT)
        elif tag == "DEL":
            self._colored(ctx, "—", Theme.TEXT_DISABLED)
        else:
            ctx.label(_preview_text(value))

    def _render_row_menu(self, ctx, key, value, tag):
        """行右键菜单: 复制键 / 复制值 / 暂存删除。"""
        try:
            if not ctx.begin_popup_context_item("##row_menu"):
                return
        except Exception:
            return
        try:
            if ctx.menu_item(t("savegame.ui.ctx_copy_key")):
                ctx.set_clipboard_text(key)
                self._say("ok", t("savegame.ui.ctx_copy_key") + ": " + key)
            if ctx.menu_item(t("savegame.ui.ctx_copy_value")):
                ctx.set_clipboard_text(_copy_text(value))
                self._say("ok", t("savegame.ui.ctx_copy_value") + ": " + key)
            if tag != "DEL" and ctx.menu_item(t("savegame.ui.ctx_stage_delete")):
                pending_delete(self._pending, key)
                self._say("warn",
                          t("savegame.ui.delete_staged_key").format(key))
        finally:
            ctx.end_popup()

    def _render_detail_child(self, ctx, slot, width, height):
        if not ctx.begin_child("##detail", width, height, True):
            ctx.end_child()
            return
        try:
            self._render_editor(ctx, slot)
            ctx.separator()
            self._render_staged_list(ctx)
            ctx.separator()
            self._render_add_row(ctx)
        finally:
            ctx.end_child()

    def _render_editor(self, ctx, slot):
        if self._selected is None:
            ctx.text_wrapped(t("savegame.ui.detail_none"))
            return
        key = self._selected
        op = self._pending.get(key)
        staged_value = op[1] if op is not None and op[0] == "set" else None
        current = staged_value if staged_value is not None else \
            self._getter(slot)(key)
        ctx.label(
            t("savegame.ui.selected_staged" if op
              else "savegame.ui.selected").format(key))
        if isinstance(current, (bytes, bytearray)) and staged_value is None:
            ctx.label(t("savegame.ui.bin_bytes").format(len(current)))
            self._colored(ctx, t("savegame.ui.bin_readonly"),
                          Theme.TEXT_DIM)
            if ctx.is_item_hovered():
                self._tooltip(ctx, "savegame.ui.tt_bytes_ro")
        else:
            self._edit_buf = ctx.input_text_with_hint(
                "##value", t("savegame.ui.hint_json"), self._edit_buf, 4096)
            if ctx.button(t("savegame.ui.stage_change")):
                self._stage_edit(key)
        if ctx.button(t("savegame.ui.stage_delete")):
            pending_delete(self._pending, key)
            self._say("warn", t("savegame.ui.delete_staged_key").format(key))

    def _stage_edit(self, key):
        value, error = parse_value_text(self._edit_buf)
        if error:
            self._say("error", error)
            return
        pending_set(self._pending, key, value)
        self._say("ok", t("savegame.ui.staged_key").format(key))

    def _render_staged_list(self, ctx):
        ctx.label(t("savegame.ui.staged_title").format(len(self._pending)))
        if ctx.is_item_hovered():
            self._tooltip(ctx, "savegame.ui.tt_staged")
        if not self._pending:
            self._colored(ctx, t("savegame.ui.staged_empty"), Theme.TEXT_DIM)
            return
        for key in sorted(self._pending):
            op = self._pending[key]
            if op[0] == "del":
                self._colored(
                    ctx, t("savegame.ui.delete_staged").format(key),
                    Theme.TEXT_DISABLED)
            else:
                ctx.push_style_color(ImGuiCol.Text, *Theme.WARNING_TEXT)
                ctx.label("* %s = %s" % (key, format_preview(op[1], 36)))
                ctx.pop_style_color(1)
            ctx.same_line()
            if ctx.button("x##unstage_%s" % key):
                del self._pending[key]
                self._say("info", t("savegame.ui.unstage").format(key))

    def _render_add_row(self, ctx):
        ctx.label(t("savegame.ui.add_key"))
        self._new_key = ctx.input_text_with_hint(
            "##new_key", t("savegame.ui.hint_key"), self._new_key, 128)
        self._new_val = ctx.input_text_with_hint(
            "##new_val", t("savegame.ui.hint_json"), self._new_val, 4096)
        if ctx.button(t("savegame.ui.stage_add")):
            key = self._new_key.strip()
            if not key:
                self._say("error", t("savegame.ui.key_required"))
                return
            value, error = parse_value_text(self._new_val)
            if error:
                self._say("error", error)
                return
            pending_set(self._pending, key, value)
            self._say("ok", t("savegame.ui.staged_new").format(key))
            self._new_key = ""
            self._new_val = ""

    def _notice_rgba(self):
        return {"ok": Theme.SUCCESS_TEXT, "warn": Theme.WARNING_TEXT,
                "error": Theme.ERROR_TEXT}.get(self._notice_kind, Theme.TEXT)

    def _render_footer(self, ctx, slot):
        pending_n = len(self._pending)
        notice = self._notice_text or ""
        faded = (time.time() - self._notice_at) > NOTICE_FADE_SECONDS
        if faded:
            notice = ""
        if len(notice) > 60:
            notice = notice[:57] + "..."
        try:
            notice_w = ctx.calc_text_width(notice) + 8.0
        except Exception:
            notice_w = 200.0
        discard_label = t("savegame.ui.discard")
        save_label = t("savegame.ui.save_n").format(pending_n)
        try:
            buttons_w = (ctx.calc_text_width(discard_label)
                         + ctx.calc_text_width(save_label)) + 76.0
        except Exception:
            buttons_w = 240.0
        ctx.set_next_item_width(notice_w)
        if notice:
            self._colored(ctx, notice, self._notice_rgba())
        else:
            ctx.label("")
        offset = max(0.0, ctx.get_content_region_avail_width() - buttons_w)
        ctx.same_line(offset)
        if pending_n:
            if ctx.button(discard_label):
                self._pending.clear()
                self._say("info", t("savegame.ui.discarded"))
        else:
            ctx.begin_disabled(True)
            ctx.button(discard_label)
            ctx.end_disabled()
        ctx.same_line()
        if pending_n:
            if ctx.button(save_label):
                ctx.open_popup("##save_confirm")
        else:
            ctx.begin_disabled(True)
            ctx.button(save_label)
            ctx.end_disabled()
        if ctx.is_item_hovered():
            self._tooltip(ctx, "savegame.ui.tt_save")
        self._render_save_modal(ctx, slot)

    def _render_save_modal(self, ctx, slot):
        if ctx.begin_popup_modal("##save_confirm"):
            ctx.label(t("savegame.ui.confirm_l1"))
            ctx.label(t("savegame.ui.confirm_l2"))
            ctx.label(t("savegame.ui.confirm_l3"))
            if ctx.button(t("savegame.ui.save")):
                applied, error = pending_apply(slot, self._pending)
                note = t("savegame.ui.saved_n").format(applied)
                if error:
                    self._say("error", note + " | " +
                              t("savegame.ui.error_prefix").format(error))
                else:
                    self._say("ok", note)
                ctx.close_current_popup()
            ctx.same_line()
            if ctx.button(t("savegame.ui.cancel")):
                self._say("info", t("savegame.ui.save_cancelled"))
                ctx.close_current_popup()
            ctx.end_popup()


def _preview_text(value):
    """Display text for one cell — corrupt sentinel localizes here."""
    if value == CORRUPT:
        return t("savegame.ui.corrupt")
    return format_preview(value)


def _copy_text(value):
    """复制用文本: bytes 转 hex, 其他走 JSON。"""
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return repr(value)


def _edit_seed(value):
    """Seed the edit box with the current value in editable JSON form."""
    if isinstance(value, (bytes, bytearray)) or value == CORRUPT:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return repr(value)
