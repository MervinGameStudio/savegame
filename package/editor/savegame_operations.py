# -*- coding: utf-8 -*-
"""savegame 的 MCP 操作集——agent 调试友好通道。

架构铁律(与面板同款): 每个操作都是 ``inx.savegame`` 公开 API 的薄投影,
经独立 Slot 对象(``get_slot``)工作, 不动游戏全局光标, 零直接文件 IO。

只读面(capability: runtime.read): slots / keys / get / stats / diagnose
写面  (capability: runtime.write): set / delete / flush

bytes 值的 op 投影: MCP 参数是 JSON——bytes 以 ``{"type": "bytes",
"hex": ...}`` 表示(set 入参, 前 4MB 上限), 读出以 size + 前 64 字节 hex
预览返回。
"""
from __future__ import annotations

import infernux as inx

from Infernux.host.operation_support import on_editor, operation
from Infernux.host.operations import OperationKind, OperationRegistry

OWNER = "mervingamestudio/savegame"
_HEX_LIMIT = 4 * 1024 * 1024          # set 入参 bytes 上限
_HEAD_BYTES = 64                      # get 返回的 hex 预览长度
# keys 投影哨兵: 回退读到它 = 文件损坏(键已由 keys() 列出, 缺失不可能)
_UNREADABLE = object()


def _key_row(key, value):
    """keys 投影: null → "NoneType"(与 get op 一致); 损坏 → "corrupt"。"""
    if value is _UNREADABLE:
        return {"key": key, "type": "corrupt", "size": None}
    size = (len(value)
            if isinstance(value, (bytes, bytearray, str)) else None)
    return {"key": key, "type": type(value).__name__, "size": size}


def _sg():
    api = getattr(inx, "savegame", None)
    if api is None:
        raise RuntimeError("savegame plugin not loaded (preload missing)")
    return api


def _slot(api, name):
    slot = api.get_slot(str(name or "default"))
    if slot is None:
        raise RuntimeError("slot %r unavailable" % name)
    return slot


def _value_view(value):
    """值 -> JSON 投影(bytes -> size + hex 头)。"""
    if isinstance(value, (bytes, bytearray)):
        head = bytes(value[:_HEAD_BYTES]).hex()
        return {"type": "bytes", "size": len(value), "head_hex": head}
    return {"type": type(value).__name__, "value": value}


def _value_from_arg(raw):
    """op 入参 -> 库值。形如 {"type":"bytes","hex":...} 还原 bytes, 其余取 value。"""
    if isinstance(raw, dict) and raw.get("type") == "bytes":
        data = bytes.fromhex(str(raw.get("hex", "")))
        if len(data) > _HEX_LIMIT:
            raise RuntimeError("bytes payload exceeds 4MB op limit")
        return data
    if isinstance(raw, dict) and set(raw) == {"type", "value"}:
        return raw.get("value")
    return raw


def _result_view(result):
    return {"ok": bool(result.ok), "status": str(result.status),
            "detail": dict(result.detail) if result.detail else {}}


def _build_operations():
    string = {"type": "string"}

    def make(op_id, kind, summary, handler, capability, inputs=None,
             required=(), side_effects=(), tags=()):
        return operation(
            op_id, kind, summary, handler,
            capability=capability,
            input_properties=inputs or {},
            required=required,
            side_effects=side_effects,
            reversible=False,
            tags=tags,
            owner=OWNER,
            thread="owner",
        )

    def slots():
        api = _sg()
        return on_editor("mervingamestudio.savegame.slots", lambda: {
            "slots": sorted(api.list_slots()),
        })

    def keys(slot="default"):
        api = _sg()
        name = str(slot or "default")

        def run():
            slot_obj = _slot(api, name)
            out = [_key_row(key, slot_obj.get(key, _UNREADABLE))
                   for key in slot_obj.keys()]
            return {"slot": name, "count": len(out), "keys": out}
        return on_editor("mervingamestudio.savegame.keys", run)

    def get(key, slot="default"):
        api = _sg()
        key = str(key)
        name = str(slot or "default")

        def run():
            value = _slot(api, name).get(key)
            view = _value_view(value)
            view["key"] = key
            return view
        return on_editor("mervingamestudio.savegame.get", run)

    def stats():
        api = _sg()

        def run():
            return {"stats": dict(api.stats()),
                    "slots": sorted(api.list_slots())}
        return on_editor("mervingamestudio.savegame.stats", run)

    def diagnose(slot="default"):
        api = _sg()
        name = str(slot or "default")

        def run():
            r = api.migrate.diagnose(slot=name)
            return {"ok": bool(r.ok), "detail": dict(r.detail or {})}
        return on_editor("mervingamestudio.savegame.diagnose", run)

    def set_value(key, value, slot="default"):
        api = _sg()
        key = str(key)
        name = str(slot or "default")
        value = _value_from_arg(value)

        def run():
            return _result_view(_slot(api, name).set(key, value))
        return on_editor("mervingamestudio.savegame.set", run)

    def delete(key, slot="default"):
        api = _sg()
        key = str(key)
        name = str(slot or "default")

        def run():
            return _result_view(_slot(api, name).delete(key))
        return on_editor("mervingamestudio.savegame.delete", run)

    def flush(slot=None):
        api = _sg()

        def run():
            if slot:
                return _result_view(_slot(api, str(slot)).flush())
            return _result_view(api.flush_all())
        return on_editor("mervingamestudio.savegame.flush", run)

    return [
        make("mervingamestudio.savegame.slots", OperationKind.QUERY,
             "List savegame slots.", slots, "runtime.read",
             tags=("savegame", "slots")),
        make("mervingamestudio.savegame.keys", OperationKind.QUERY,
             "List keys of one savegame slot (type/size annotated).", keys,
             "runtime.read",
             inputs={"slot": {"type": "string"}},
             tags=("savegame", "keys")),
        make("mervingamestudio.savegame.get", OperationKind.QUERY,
             "Read one savegame value (bytes -> size + head hex).", get,
             "runtime.read",
             inputs={"slot": string, "key": string},
             required=("key",),
             tags=("savegame", "get")),
        make("mervingamestudio.savegame.stats", OperationKind.QUERY,
             "Savegame status projection (mode/compress/dirty per slot).",
             stats, "runtime.read",
             tags=("savegame", "stats")),
        make("mervingamestudio.savegame.diagnose", OperationKind.QUERY,
             "Run savegame diagnose (orphan packs / dangling index / "
             "layout residue / corrupt packs).", diagnose, "runtime.read",
             inputs={"slot": string},
             tags=("savegame", "diagnose")),
        make("mervingamestudio.savegame.set", OperationKind.COMMAND,
             "Write one savegame value through the public API "
             "(bytes via {type:'bytes', hex:...}, <=4MB).", set_value,
             "runtime.write",
             inputs={"slot": string, "key": string, "value": {}},
             required=("key", "value"),
             side_effects=("Writes one savegame value.",),
             tags=("savegame", "set")),
        make("mervingamestudio.savegame.delete", OperationKind.COMMAND,
             "Delete one savegame key.", delete, "runtime.write",
             inputs={"slot": string, "key": string},
             required=("key",),
             side_effects=("Deletes one savegame key.",),
             tags=("savegame", "delete")),
        make("mervingamestudio.savegame.flush", OperationKind.COMMAND,
             "Flush one slot (omit slot = all slots).", flush,
             "runtime.write",
             inputs={"slot": string},
             side_effects=("Flushes staged savegame writes.",),
             tags=("savegame", "flush")),
    ]


def register_savegame_operations() -> int:
    registry = OperationRegistry.instance()
    count = 0
    for op in _build_operations():
        registry.register(op)
        count += 1
    return count


def unregister_savegame_operations() -> int:
    return OperationRegistry.instance().unregister_owner(OWNER)
