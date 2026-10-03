# -*- coding: utf-8 -*-
"""mervingamestudio/savegame v0.9.1 — 槽位式分片键值存档库（管家定位）。

设计要点:
  定位 = 辅助玩家管理存档的管家, 不是户主: 位置/内容/结构皆用户决策。
  存储 = 文件即值, 扩展名即事实(.json|.bin|.json.z|.bin.z);
         点开头 = 库私域(.savegame/), 平铺 = 用户数据;
         槽级 .savegame/state.json(compress+aggregate 事实) + 游戏级 defaults(出生默认)。
  聚合 = 可选布局: 类型分片 jsonl(int-0.pack 等, 人可读) + bytes 恒散放
         + .savegame/index.json 可再生索引(容器自描述=数据权威, 读时自愈)
         + 溢出开新片(1MB 护栏) + migrate.aggregate/scatter 与 compress/plain 正交;
         布局迁移失败≡中断(state 不翻不清理, 重跑完整重做);
         diagnose() 识别孤儿片/索引悬空/布局残留/损坏片四类静默异常。
  业务 = 写透默认(变化即落盘, type is type and ==); 缓存三投影
         (读缓存/负缓存/同值短路)同属一原则: 能缓存就缓存, 确认变化才 IO。
  批量 = sg.batch(True/False) 原语公开; MgsSavegameScheduler 官方组件只是
         参考壳, 用户自定义调度与其平权(同一个底座的两个消费者)。
  迁移 = sg.migrate.compress/plain(slot=None, defaults=True):
         一次性整槽重编码, 逐键原子(写新->验->删旧), 幂等重跑收敛。

契约(三条线):
  读   = 值本体。get(key) 严格: 缺失 KeyError / 损坏 SavegameError;
         get(key, default) 回退到底: 缺失与损坏均返回 default。
  变更 = SaveResult(ok/status/detail/data; 真值即 ok)。
  异常 = 编程错误(键名非法 ValueError / __setitem__ 拒绝)。

键空间显式契约: **大小写不敏感**(统一归一为小写)——任意拼写互通同一键,
不要通过大小写来区别 key; 跨平台(Windows/Android/Linux)行为绝对一致。

沙箱(candidate import)注意: 顶层仅 os + infernux 属性访问与纯定义;
json/zlib/threading 惰性导入; 哨兵用类对象(顶层禁止函数调用)。
"""
import os

import infernux as inx

Debug = inx.Debug
InxPreload = inx.lifecycle.InxPreload
PreloadContext = inx.lifecycle.PreloadContext
InxComponent = inx.components.component.InxComponent

# ---- 存储常量 ----
_SUFFIX_JSON = ".json"          # 明文 JSON 值
_SUFFIX_BIN = ".bin"            # 明文 bytes 值
_SUFFIX_JSON_Z = ".json.z"      # zlib(json 序列化)
_SUFFIX_BIN_Z = ".bin.z"        # zlib(bytes)
_SUFFIXES = (_SUFFIX_JSON_Z, _SUFFIX_BIN_Z, _SUFFIX_JSON, _SUFFIX_BIN)  # 长前短后
_PRIVATE_DIR = ".savegame"      # 库私域: key 校验禁止 . 开头 => 天然正交
_LEGACY_PRIVATE_DIR = ".inx"     # 兼容目录名(启动收养, 见 _adopt_legacy_private)
_STATE_FILE = "state.json"
_DEFAULTS_FILE = "defaults.json"
_INDEX_FILE = "index.json"      # 聚合态路由账本(可再生: 扫片/散 bin 可重建)
_ROOT_ENV = "MGS_SAVES_ROOT"    # root 早期覆盖(优先级最高, 永不落盘)


def _adopt_legacy_private(target_dir):
    """目录名收养: 存在 .inx 且无 .savegame 时整体改名(内容全为库私有
    元数据, 原子 rename 即完成)。收养失败不阻断——新路径按默认态继续,
    旧目录原地保留可人工处理。"""
    legacy = os.path.join(target_dir, _LEGACY_PRIVATE_DIR)
    modern = os.path.join(target_dir, _PRIVATE_DIR)
    try:
        if os.path.isdir(legacy) and not os.path.exists(modern):
            os.rename(legacy, modern)
    except OSError:
        pass

# ---- 聚合布局 ----
_PACK_SUFFIX = ".pack"          # 类型分片(jsonl 文本, 每行 {"k":..,"v":..})
_PACK_SUFFIX_Z = ".pack.z"      # 整片 zlib(压缩=最小独立重写单元)
_PACK_GUARD_BYTES = 1 << 20     # 片护栏(写死的防御性保险丝, 非调优参数)
_PACK_GUARD_COUNT = 100000      # 条目数护栏(同上)
_PACK_FILE_SUFFIXES = (_PACK_SUFFIX, _PACK_SUFFIX_Z)


def _pack_type_of(value):
    """值类型 -> 分片名; bytes/未知返回 None(恒散放, 不进片)。"""
    if value is None:
        return "null"
    if isinstance(value, bool):  # bool 是 int 子类, 先判
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, dict):
        return "dict"
    if isinstance(value, list):
        return "list"
    return None


def _stem_of_scatter(fname):
    """散放 bytes 文件名 -> key(供批量写失败反查)。"""
    if fname.endswith(_SUFFIX_BIN_Z):
        return fname[:-len(_SUFFIX_BIN_Z)]
    return fname[:-len(_SUFFIX_BIN)]

_json_mod = None
_zlib_mod = None
_threading_mod = None


def _json():
    global _json_mod
    if _json_mod is None:
        import json as _m
        _json_mod = _m
    return _json_mod


def _zlib():
    global _zlib_mod
    if _zlib_mod is None:
        import zlib as _m
        _zlib_mod = _m
    return _zlib_mod


def _threading():
    global _threading_mod
    if _threading_mod is None:
        import threading as _m
        _threading_mod = _m
    return _threading_mod


class _NoDefaultSentinel(object):
    """get 系列"未提供 default"哨兵 = 严格模式开关(类对象作标识)。"""
    __slots__ = ()


_NO_DEFAULT = _NoDefaultSentinel


class SavegameError(Exception):
    """严格模式读取时, 键存在但内容损坏/无法解码。原因见 args 与日志。"""


class SaveResult(object):
    """变更/IO 操作的统一返回。真值即 ok; detail/data 按需深挖。"""
    __slots__ = ("ok", "status", "detail", "data")

    def __init__(self, ok, status, detail=None, data=None):
        self.ok = bool(ok)
        self.status = status
        self.detail = detail
        self.data = data

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return "SaveResult(ok=%s, status=%r, detail=%r)" % (
            self.ok, self.status, self.detail)


_RESULTS = None  # 惰性单例表: 常用无明细状态零分配


def _ok(status):
    global _RESULTS
    if _RESULTS is None:
        _RESULTS = {
            "written": SaveResult(True, "written"),
            "queued": SaveResult(True, "queued"),
            "removed": SaveResult(True, "removed"),
            "missing": SaveResult(True, "missing"),
            "done": SaveResult(True, "done"),
        }
    return _RESULTS[status]


# ============ 键名校验与值归一化 ============

def _name_ok(text):
    if not text or len(text) > 64:
        return False
    for ch in text:
        if not (("a" <= ch <= "z") or ("A" <= ch <= "Z")
                or ("0" <= ch <= "9") or ch in "_.-"):
            return False
    return text[0] not in ".-"


def _check_name(value, kind):
    """名字必经之路: 归一为小写后校验——键空间大小写不敏感是显式 API 契约
    (任意拼写互通同一键; 不要通过大小写来区别 key)。"""
    text = str(value or "").lower()
    if not _name_ok(text):
        raise ValueError(
            "invalid %s name %r (allowed: a-z 0-9 _ . - , max 64 chars,"
            " must not start with . or -; keys are case-insensitive"
            " (normalized to lowercase))" % (kind, value))
    return text


def _normalize_value(value):
    """tuple->list(JSON 数组语义, 跨会话类型一致); bytearray->bytes。递归。"""
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _normalize_value(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_normalize_value(item) for item in value]
    if isinstance(value, bytearray):
        return bytes(value)
    return value


def _coerce(value, kinds, default):
    if isinstance(value, bool) and bool not in kinds:
        return default
    if isinstance(value, kinds):
        return value
    return default


def _safe_dir(text):
    out = []
    for ch in str(text):
        if ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ("0" <= ch <= "9") or ch in "_.-":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out) or "game"


# ============ 文件层(存储) ============

def _atomic_write(path, data):
    """tmp + os.replace; data: bytes。失败抛 OSError。"""
    tmp = path + ".tmp"
    with open(tmp, "wb") as stream:
        stream.write(data)
    os.replace(tmp, path)


def _read_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            doc = _json().load(stream)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def _read_state(slot_dir):
    """槽级事实读取; 缺失/损坏返回 None(由 defaults/全默认接管)。"""
    return _read_json_file(os.path.join(slot_dir, _PRIVATE_DIR, _STATE_FILE))


def _write_state(slot_dir, state):
    private = os.path.join(slot_dir, _PRIVATE_DIR)
    os.makedirs(private, exist_ok=True)
    _atomic_write(os.path.join(private, _STATE_FILE),
                  _json().dumps(state, indent=2, sort_keys=True).encode("utf-8"))


def _read_defaults(game_dir):
    return _read_json_file(os.path.join(game_dir, _PRIVATE_DIR, _DEFAULTS_FILE))


def _update_defaults(game_dir, changes):
    """合并更新游戏级默认; 返回是否发生变化(含首次创建)。"""
    doc = _read_defaults(game_dir) or {}
    changed = False
    for key, value in changes.items():
        if doc.get(key) != value:
            doc[key] = value
            changed = True
    if changed:
        private = os.path.join(game_dir, _PRIVATE_DIR)
        os.makedirs(private, exist_ok=True)
        _atomic_write(os.path.join(private, _DEFAULTS_FILE),
                      _json().dumps(doc, indent=2, sort_keys=True).encode("utf-8"))
    return changed


def _encode_value(value, compress):
    """值 -> (suffix, bytes)。bytes 族走 .bin; 其余 JSON; compress 决定 .z。
    NaN/Infinity 在此被拒(allow_nan=False, 官方文案 reason)。"""
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
        base = _SUFFIX_BIN
    else:
        data = _json().dumps(value, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
        base = _SUFFIX_JSON
    if compress:
        return (base + ".z", _zlib().compress(data, 6))
    return (base, data)


def _decode_value(suffix, raw):
    """(suffix, bytes) -> value。损坏抛异常(由调用方定性)。"""
    if suffix == _SUFFIX_JSON_Z or suffix == _SUFFIX_BIN_Z:
        raw = _zlib().decompress(raw)
    if suffix == _SUFFIX_BIN or suffix == _SUFFIX_BIN_Z:
        return raw
    return _json().loads(raw.decode("utf-8"))


def _suffix_is_z(suffix):
    return suffix == _SUFFIX_JSON_Z or suffix == _SUFFIX_BIN_Z


def _report_corrupt_scatter(slot, filename, issues):
    """散放文件解码检查: .json/.z 可判坏(解码抛错); 明文 .bin 是原始
    bytes 恒有效, 不检测。文件名即键名(大小写已归一)。"""
    suffix = None
    for sfx in _SUFFIXES:
        if filename.endswith(sfx):
            suffix = sfx
            break
    if suffix == _SUFFIX_BIN:
        return
    key = filename[:-len(suffix)]
    try:
        with open(os.path.join(slot._dir, filename), "rb") as stream:
            raw = stream.read()
        _decode_value(suffix, raw)
    except Exception:
        if not any(i["issue"] == "corrupt_key" and i["key"] == key
                   for i in issues):
            issues.append({
                "issue": "corrupt_key", "key": key, "file": filename,
                "advice": "restore from backup or rewrite the key"})


def _read_pack(path):
    """读片: jsonl(或整片 zlib 后的 jsonl) -> {key: value}。损坏抛异常。"""
    with open(path, "rb") as stream:
        raw = stream.read()
    if path.endswith(_PACK_SUFFIX_Z):
        raw = _zlib().decompress(raw)
    entries = {}
    for line in raw.decode("utf-8").splitlines():
        if not line:
            continue
        record = _json().loads(line)
        entries[str(record["k"])] = record["v"]
    return entries


def _write_pack(path, entries):
    """写片: entries dict -> jsonl(压缩态整体 zlib)。原子写, 键序稳定。"""
    text = "".join(_json().dumps({"k": k, "v": entries[k]},
                                 ensure_ascii=False, separators=(",", ":"),
                                 allow_nan=False) + "\n" for k in sorted(entries))
    data = text.encode("utf-8")
    if path.endswith(_PACK_SUFFIX_Z):
        data = _zlib().compress(data, 6)
    _atomic_write(path, data)
    return len(data)


# ============ 业务层 Slot ============

class Slot(object):
    """单槽: 缓存三投影 + 写透/batch 双模式。文件操作全走文件层。"""

    def __init__(self, name, slot_dir, lock, write_through=True, game_dir=None):
        self.name = name
        _adopt_legacy_private(slot_dir)
        self._dir = slot_dir
        self._game_dir = game_dir
        self._lock = lock
        self._cache = {}
        self._missing = set()
        self._corrupt = set()
        self._dirty = {}          # 散态: key->(suffix,data); 聚合态: key->value
        self._write_through = write_through
        self._state_persisted = False
        self._aggregate = False
        self._index = None        # {key: filename} 聚合态路由账本(内存镜像)
        self._packs = {}          # {filename: {key: value}} 片级惰性缓存
        self._sealed = set()      # 已达护栏封顶的片(不再接新键)
        self._rebuilt = False     # 本进程已做过一次 miss 重建(防反复全扫)
        self._inflight = set()    # flush 在途键集(快照取走~落盘完成)
        self._tombstones = set()  # 墓碑: 在途期间被 delete 的键, flush 跳过防复活
        self._superseded = set()  # 在途期间被写透 set 覆写的键(flush 跳过旧快照)
        state = _read_state(slot_dir)
        if state is not None:
            self._compress = bool(state.get("compress", False))
            self._aggregate = bool(state.get("aggregate", False))
            self._state_persisted = True
        else:
            defaults = _read_defaults(game_dir) if game_dir else None
            defaults = defaults or {}
            self._compress = bool(defaults.get("compress", False))
            self._aggregate = bool(defaults.get("aggregate", False))

    @property
    def compress(self):
        """槽级布局事实(只读投影)。"""
        return bool(self._compress)

    @property
    def aggregate(self):
        """槽级布局事实(只读投影)。"""
        return bool(self._aggregate)

    def _persist_state_nolock(self):
        """IO 失败就是 IO 失败——与主数据同域, 不需要特殊语义。
        state 是可再生元数据(defaults 兜底/互为盲区兜底), 写不进就 log,
        最坏后果=下次启动回到旧布局, 架构已处理(中断安全)。"""
        try:
            _write_state(self._dir, {"compress": self._compress,
                                     "aggregate": self._aggregate})
            self._state_persisted = True
        except OSError as exc:
            Debug.log("[SaveGame] state persist failed (IO): %s" % exc)

    # ---- 聚合布局内部 ----
    def _pack_path(self, filename):
        return os.path.join(self._dir, filename)

    def _is_pack_name(self, filename):
        return filename.endswith(_PACK_SUFFIX) or filename.endswith(_PACK_SUFFIX_Z)

    def _ensure_index_nolock(self):
        """索引内存镜像(急切常驻); 缺失/损坏则扫片重建(可再生账本)。"""
        if self._index is not None:
            return self._index
        doc = _read_json_file(os.path.join(self._dir, _PRIVATE_DIR, _INDEX_FILE))
        if doc and isinstance(doc.get("keys"), dict):
            self._index = {str(k): str(v) for k, v in doc["keys"].items()}
        else:
            self._rebuild_index_nolock()
            self._persist_index_nolock()
        return self._index

    def _rebuild_index_nolock(self):
        """重建器: 扫全部片与散 .bin 重建索引(容器自描述=数据权威)。"""
        index = {}
        try:
            names = os.listdir(self._dir)
        except FileNotFoundError:
            names = []
        for name in names:
            if self._is_pack_name(name):
                try:
                    for key in _read_pack(self._pack_path(name)):
                        index[key] = name
                except Exception as exc:
                    Debug.log("[SaveGame] pack %s unreadable (%s)" % (name, exc))
            elif name.endswith(_SUFFIX_BIN) or name.endswith(_SUFFIX_BIN_Z):
                suffix = _SUFFIX_BIN_Z if name.endswith(_SUFFIX_BIN_Z) else _SUFFIX_BIN
                stem = name[:-len(suffix)]
                if stem and not stem.startswith("."):
                    index[stem] = name
        self._index = index
        self._packs = {}  # 片缓存随索引一起失效重读
        return index

    def _persist_index_nolock(self):
        """IO 失败就是 IO 失败——索引是可再生缓存, 写不进就 log,
        最坏后果=下次读 miss 触发重建(架构已有的自愈)。"""
        try:
            private = os.path.join(self._dir, _PRIVATE_DIR)
            os.makedirs(private, exist_ok=True)
            _atomic_write(os.path.join(private, _INDEX_FILE),
                          _json().dumps({"keys": self._index},
                                        sort_keys=True).encode("utf-8"))
        except OSError as exc:
            Debug.log("[SaveGame] index persist failed (IO): %s" % exc)

    def _pack_entries_nolock(self, filename):
        """片级惰性缓存; 新片/不存在=空 dict; 损坏抛 SavegameError。"""
        entries = self._packs.get(filename)
        if entries is None:
            path = self._pack_path(filename)
            if os.path.isfile(path):
                try:
                    entries = _read_pack(path)
                except Exception as exc:
                    raise SavegameError("pack %s unreadable (%s)"
                                        % (filename, exc))
            else:
                entries = {}
            self._packs[filename] = entries
        return entries

    def _active_pack_name_nolock(self, pack_type):
        """溢出开新片: 未封顶的最大序号片为活跃; 全封顶则开新片。"""
        try:
            names = os.listdir(self._dir)
        except FileNotFoundError:
            names = []
        prefix = pack_type + "-"
        active, max_no, act_no = None, -1, -1
        for name in names:
            stem = None
            for suffix in _PACK_FILE_SUFFIXES:
                if name.startswith(prefix) and name.endswith(suffix):
                    stem = name[:-len(suffix)]
                    break
            if stem is None:
                continue
            digits = stem[len(prefix):]
            if not digits.isdigit():
                continue
            no = int(digits)
            if no > max_no:
                max_no = no
            if name not in self._sealed and no > act_no:
                active, act_no = name, no
        if active is None:
            active = "%s-%d%s" % (pack_type, max_no + 1,
                                  _PACK_SUFFIX_Z if self._compress else _PACK_SUFFIX)
        return active

    def _store_aggregate_nolock(self, key, value):
        """聚合态统一写(先数据后索引): bytes 散放+登记; JSON 进类型片+登记;
        已有键同类型原地更新(封顶片亦然, 不搬家); 换类型才路由活跃片;
        溢开护栏; 空片回收。"""
        index = self._ensure_index_nolock()
        old_file = index.get(key)
        if isinstance(value, (bytes, bytearray)):
            data = bytes(value)
            suffix = _SUFFIX_BIN_Z if self._compress else _SUFFIX_BIN
            filename = key + suffix
            _atomic_write(self._pack_path(filename), data)
        else:
            pack_type = _pack_type_of(value)
            if pack_type is None:
                raise ValueError("value type not storable in aggregate mode")
            if old_file is not None and self._is_pack_name(old_file) \
                    and old_file.startswith(pack_type + "-"):
                filename = old_file  # 同类型原地更新: 封顶片不接新键但接受旧键
            else:
                filename = self._active_pack_name_nolock(pack_type)
            entries = self._pack_entries_nolock(filename)
            entries[key] = value
            size = _write_pack(self._pack_path(filename), entries)
            if size >= _PACK_GUARD_BYTES or len(entries) >= _PACK_GUARD_COUNT:
                self._sealed.add(filename)  # 封顶: 新键路由下一片
        if old_file is not None and old_file != filename:
            self._remove_from_location_nolock(key, old_file)
        index[key] = filename
        self._persist_index_nolock()
        if not self._state_persisted:
            self._persist_state_nolock()

    def _store_batch_nolock(self, items):
        """批量写路径(锁内, 唯一): 同片键一次重写, 索引一次持久化。
        供 flush / migrate.aggregate / 批量导入共用——单键写透仍走 _store。
        失败语义片级: 某片写失败 -> 该片的键整体进返回的 failed。"""
        index = self._ensure_index_nolock()
        routes = {}
        scatter = {}
        groups = {}
        for key, value in items.items():
            old_file = index.get(key)
            if isinstance(value, (bytes, bytearray)):
                suffix = _SUFFIX_BIN_Z if self._compress else _SUFFIX_BIN
                fname = key + suffix
                scatter[fname] = bytes(value)
                routes[key] = fname
                continue
            pack_type = _pack_type_of(value)
            if pack_type is None:
                raise ValueError("value type not storable in aggregate mode")
            if old_file is not None and self._is_pack_name(old_file) \
                    and old_file.startswith(pack_type + "-"):
                fname = old_file  # 同类型原地: 封顶片不接新键但接受旧键
            else:
                fname = self._active_pack_name_nolock(pack_type)
            groups.setdefault(fname, {})[key] = value
            routes[key] = fname
        failed = {}
        # 分组进片缓存(内存) -> 每涉及片一次重写
        for fname, keys in groups.items():
            try:
                entries = self._pack_entries_nolock(fname)
                entries.update(keys)
                size = _write_pack(self._pack_path(fname), entries)
                if size >= _PACK_GUARD_BYTES or len(entries) >= _PACK_GUARD_COUNT:
                    self._sealed.add(fname)
            except Exception as exc:
                for key in keys:
                    failed[key] = str(exc)
        for fname, data in scatter.items():
            try:
                _atomic_write(self._pack_path(fname), data)
            except Exception as exc:
                failed[_stem_of_scatter(fname)] = str(exc)
        # 换类型/换位置搬家 + 索引路由
        for key, fname in routes.items():
            if key in failed:
                continue
            old_file = index.get(key)
            if old_file is not None and old_file != fname \
                    and old_file not in scatter:
                self._remove_from_location_nolock(key, old_file)
                moved = True
            index[key] = fname
        self._persist_index_nolock()
        if not self._state_persisted:
            self._persist_state_nolock()
        return failed

    def _remove_from_location_nolock(self, key, filename):
        """清除旧位置: 片内删除(空片回收)或散 .bin 删除。"""
        if self._is_pack_name(filename):
            try:
                entries = self._pack_entries_nolock(filename)
            except SavegameError:
                return
            entries.pop(key, None)
            if not entries:
                self._packs.pop(filename, None)
                self._sealed.discard(filename)
                try:
                    os.remove(self._pack_path(filename))  # 空片惰性回收
                except OSError:
                    pass
            else:
                _write_pack(self._pack_path(filename), entries)
        else:
            try:
                os.remove(self._pack_path(filename))
            except OSError:
                pass

    def _enter_layout(self, target):
        """迁移前向原语: 切换聚合态内存视图(不落盘 state——成功路径才 persist)。"""
        self._aggregate = target
        self._index = {} if target else None
        self._packs = {}
        self._sealed = set()
        self._rebuilt = target  # 聚合向: 新索引从零构建; 散向: 无需重建

    def _exit_layout(self, target):
        """迁移回滚原语: _enter_layout 的逆(失败≡中断时恢复旧世界内存视图)。"""
        self._enter_layout(target)  # 逆操作恰好=再进一次旧布局

    def _route_nolock(self, key):
        """路由原语(锁内): 查索引 → miss → 一次性重建 → 重查。
        "索引可再生"不变式的唯一投影点——所有 API 共用, 不再各自手写兜底。
        返回文件名 or None(真缺失)。"""
        index = self._ensure_index_nolock()
        filename = index.get(key)
        if filename is None and not self._rebuilt:
            self._rebuild_index_nolock()
            self._rebuilt = True
            self._persist_index_nolock()
            filename = self._index.get(key)
        return filename

    def _route_pack_entry_nolock(self, key, filename):
        """片路由原语(锁内): 片内无此键且未重建过 → 重建重查。
        返回 (最终文件名, 值 or _NO_DEFAULT)。"""
        entries = self._pack_entries_nolock(filename)
        if key not in entries and not self._rebuilt:
            self._rebuild_index_nolock()
            self._rebuilt = True
            self._persist_index_nolock()
            filename = self._index.get(key)
            if filename is None:
                return (None, _NO_DEFAULT)
            entries = self._pack_entries_nolock(filename)
        if key not in entries:
            return (filename, _NO_DEFAULT)
        return (filename, entries[key])

    def _route_all_nolock(self):
        """全量路由原语(keys 用): 索引未加载时一次性加载或重建。"""
        index = self._ensure_index_nolock()
        if not self._rebuilt and not os.path.exists(
                os.path.join(self._dir, _PRIVATE_DIR, _INDEX_FILE)):
            self._rebuild_index_nolock()
            self._rebuilt = True
            self._persist_index_nolock()
            return self._index
        return index

    def _load_nolock(self, key):
        """读侧统一入口(锁内): 返回值或 _NO_DEFAULT; 损坏记 corrupt。"""
        if self._aggregate:
            filename = self._route_nolock(key)
            if filename is None:
                self._missing.add(key)
                return _NO_DEFAULT
            try:
                if self._is_pack_name(filename):
                    filename, value = self._route_pack_entry_nolock(key, filename)
                    if value is _NO_DEFAULT:
                        self._missing.add(key)
                        return _NO_DEFAULT
                else:
                    with open(self._pack_path(filename), "rb") as stream:
                        raw = stream.read()
                    if filename.endswith(_SUFFIX_BIN_Z):
                        raw = _zlib().decompress(raw)
                    value = raw
            except FileNotFoundError:
                # 散悬空: 与 pack 悬空同构的自愈(路由原语的对称投影)
                if not self._rebuilt:
                    self._rebuild_index_nolock()
                    self._rebuilt = True
                    self._persist_index_nolock()
                    filename = self._index.get(key)
                    if filename is not None:
                        return self._load_nolock(key)
                Debug.log("[SaveGame] key %r file missing (%s)" % (key, filename))
                self._corrupt.add(key)
                return _NO_DEFAULT
            except Exception as exc:
                Debug.log("[SaveGame] key %r undecodable (%s)" % (key, exc))
                self._corrupt.add(key)
                return _NO_DEFAULT
            self._cache[key] = value
            self._missing.discard(key)
            return value
        # 散文件态: 四态探测
        found = self._find_file(key)
        if found is None:
            self._missing.add(key)
            return _NO_DEFAULT
        path, suffix = found
        try:
            with open(path, "rb") as stream:
                raw = stream.read()
            value = _decode_value(suffix, raw)
        except Exception as exc:
            Debug.log("[SaveGame] key %r undecodable (%s)" % (key, exc))
            self._corrupt.add(key)
            return _NO_DEFAULT
        self._cache[key] = value
        self._missing.discard(key)
        return value

    # ---- 内部: 文件操作(不持锁, 由调用方决定锁协议) ----
    def _find_file(self, key):
        """四态探测: (path, suffix) or None。当前 state 形态优先(迁移期兼容)。"""
        if self._compress:
            order = (_SUFFIX_JSON_Z, _SUFFIX_BIN_Z, _SUFFIX_JSON, _SUFFIX_BIN)
        else:
            order = (_SUFFIX_JSON, _SUFFIX_BIN, _SUFFIX_JSON_Z, _SUFFIX_BIN_Z)
        for suffix in order:
            path = os.path.join(self._dir, key + suffix)
            if os.path.isfile(path):
                return (path, suffix)
        return None

    def _write_payload_nolock(self, key, suffix, data):
        """单键原子写 + 清异形态残留 + 惰性立户口。抛异常=失败。"""
        os.makedirs(self._dir, exist_ok=True)
        _atomic_write(os.path.join(self._dir, key + suffix), data)
        for other in _SUFFIXES:
            if other != suffix:
                try:
                    os.remove(os.path.join(self._dir, key + other))
                except OSError:
                    pass
        if not self._state_persisted:
            self._persist_state_nolock()

    def _flush_aggregate(self, snapshot):
        """聚合态批量落盘: 经唯一批量路径(同片一次重写, O(N)), 全程持锁。
        墓碑/新值保护均不需要——聚合态无新旧快照并存窗口(写透 set 串行于
        flush 之后, delete 无打墓碑窗口)。"""
        with self._lock:
            failed = self._store_batch_nolock(snapshot) if snapshot else {}
            written = len(snapshot) - len(failed)
            if failed:
                for key in failed:
                    if key not in self._dirty:  # 不覆盖更新的排队值
                        self._dirty[key] = snapshot[key]
                return SaveResult(False, "partial",
                                  {"written": written, "failed": failed})
            return SaveResult(True, "done", {"written": written})

    def _settle_tombstones_nolock(self):
        """flush 尾部结算墓碑: 对在途期间被 delete 的键执行真正清理。
        delete 之后又有新 set 入队的键跳过(尊重最后意图)。
        仅散态可达——聚合态 flush 全程持锁, delete 无打墓碑窗口。"""
        if not self._tombstones:
            return
        for key in self._tombstones:
            if key in self._dirty:
                continue
            self._cache.pop(key, None)
            self._missing.add(key)
            for suffix in _SUFFIXES:
                try:
                    os.remove(os.path.join(self._dir, key + suffix))
                except OSError:
                    pass
        self._tombstones.clear()

    def _settle_superseded_nolock(self):
        """flush 尾部结算新值保护: 对在途期间被写透 set 覆写的键,
        以 cache 最后意图重写一次(风险期结束时盘上=最后意图)。
        重写失败转入 dirty 重试。
        仅散态可达——聚合态 flush 全程持锁(_flush_aggregate), 写透 set
        串行于其后, 无新旧快照并存窗口。"""
        for key in list(self._superseded):
            if key not in self._cache:
                self._superseded.discard(key)  # cache 已无此键: 标记失效
                continue
            value = self._cache[key]
            suffix, data = _encode_value(value, self._compress)
            try:
                self._write_payload_nolock(key, suffix, data)
            except Exception:
                if key not in self._dirty:  # 不覆盖更新的排队值
                    self._dirty[key] = (suffix, data)
            self._superseded.discard(key)

    def flush(self):
        """batch 模式落盘 dirty(写透模式恒 done)。失败键保留重试。
        在途期间被 delete 的键经墓碑跳过, 尾部结算真正删除。"""
        with self._lock:
            if not self._dirty:
                return _ok("done")
            snapshot = self._dirty
            self._dirty = {}
            self._inflight.update(snapshot)  # 并集: 并发双 flush 各自在途, 不互冲
        try:
            if self._aggregate:
                return self._flush_aggregate(snapshot)
            written, failed = 0, {}
            for key, payload in snapshot.items():
                if key in self._tombstones:
                    continue  # 墓碑: 不让在途写复活已删除的键
                if key in self._superseded:
                    continue  # 新值保护: 在途旧快照不得覆写已落盘的写透新值
                try:
                    self._write_payload_nolock(key, payload[0], payload[1])
                    written += 1
                except Exception as exc:
                    failed[key] = str(exc)
            if failed:
                with self._lock:
                    for key, value in failed.items():
                        if key not in self._dirty:  # 不覆盖更新的排队值
                            self._dirty[key] = snapshot[key]
                return SaveResult(False, "partial",
                                  {"written": written, "failed": failed})
            return SaveResult(True, "done", {"written": written})
        finally:
            with self._lock:
                self._inflight.difference_update(snapshot)  # 只清自己的在途键
                if not self._inflight:
                    # 结算 ≡ 风险期结束: 最后一个在途写者离开时结算
                    if self._tombstones:
                        self._settle_tombstones_nolock()
                    if self._superseded:
                        self._settle_superseded_nolock()

    # ---- 写 ----
    def set(self, key, value):
        """写透模式: 变化即落盘(status=written); batch 模式: 排队(queued)。
        散态 dirty 存编码后 payload(两模式拒绝语义一致, flush 纯 IO);
        聚合态 dirty 存 value(flush 时按片重写)。"""
        try:
            key = _check_name(key, "key")
        except ValueError as exc:
            Debug.log("[SaveGame] set(%r) rejected: %s" % (key, exc))
            return SaveResult(False, "rejected", {"reason": str(exc)})
        value = _normalize_value(value)
        with self._lock:
            supersede = key in self._inflight  # 新值保护: 在途快照不得覆灭更新意图
            if key not in self._dirty and key in self._cache:
                old = self._cache[key]
                if type(old) is type(value) and old == value:
                    return _ok("written")  # 同值短路: 确认无变化, 零 IO
            if self._aggregate:
                if not isinstance(value, (bytes, bytearray)):
                    try:
                        _json().dumps(value, allow_nan=False)
                    except Exception as exc:
                        Debug.log("[SaveGame] set(%r) rejected: %s" % (key, exc))
                        return SaveResult(False, "rejected",
                                          {"reason": str(exc)})
                try:
                    if self._write_through:
                        self._store_aggregate_nolock(key, value)
                        status = "written"
                    else:
                        self._dirty[key] = value
                        status = "queued"
                except Exception as exc:
                    Debug.log("[SaveGame] set(%r) failed: %s" % (key, exc))
                    return SaveResult(False, "failed", {"reason": str(exc)})
                self._cache[key] = value
                self._missing.discard(key)
                self._corrupt.discard(key)
                return _ok(status)
            try:
                suffix, data = _encode_value(value, self._compress)
            except Exception as exc:
                Debug.log("[SaveGame] set(%r) rejected: %s" % (key, exc))
                return SaveResult(False, "rejected", {"reason": str(exc)})
            if self._write_through:
                if supersede:
                    self._superseded.add(key)  # flush 逐键跳过其旧快照
                try:
                    self._write_payload_nolock(key, suffix, data)
                except Exception as exc:
                    self._superseded.discard(key)
                    Debug.log("[SaveGame] set(%r) failed: %s" % (key, exc))
                    return SaveResult(False, "failed", {"reason": str(exc)})
                status = "written"
            else:
                self._dirty[key] = (suffix, data)
                status = "queued"
            self._cache[key] = value
            self._missing.discard(key)
            self._corrupt.discard(key)
            self._tombstones.discard(key)  # 最后意图: 新值使墓碑使命终结
            return _ok(status)

    def __setitem__(self, key, value):
        r = self.set(key, value)
        if not r.ok:
            raise ValueError(
                "savegame set(%r) failed: %s"
                % (key, r.detail.get("reason", "rejected") if r.detail else ""))

    # ---- 读 ----
    def get(self, key, default=_NO_DEFAULT):
        key = _check_name(key, "key")
        with self._lock:
            if key in self._cache:
                value = self._cache[key]
            elif key in self._missing:
                value = _NO_DEFAULT
            else:
                value = self._load_nolock(key)  # 路由分派(散态四态探测/聚合索引)
        if value is _NO_DEFAULT:
            if default is _NO_DEFAULT:
                if key in self._corrupt:
                    raise SavegameError(
                        "key %r exists but failed to decode (corrupt or"
                        " truncated)" % (key,))
                raise KeyError(key)
            return default
        return value

    def __getitem__(self, key):
        return self.get(key)

    def _typed(self, key, kinds, default, kind_name, converter=None):
        strict = default is _NO_DEFAULT
        if strict:
            value = self.get(key)  # 缺失/损坏向上抛
            if isinstance(value, bool) and bool not in kinds \
                    or not isinstance(value, kinds):
                raise TypeError("key %r is %s, not %s"
                                % (key, type(value).__name__, kind_name))
            return converter(value) if converter else value
        return _coerce(self.get(key, default), kinds, default)

    def get_int(self, key, default=_NO_DEFAULT):
        return self._typed(key, (int,), default, "int")

    def get_float(self, key, default=_NO_DEFAULT):
        return self._typed(key, (int, float), default, "float", float)

    def get_str(self, key, default=_NO_DEFAULT):
        return self._typed(key, (str,), default, "str")

    def get_bool(self, key, default=_NO_DEFAULT):
        return self._typed(key, (bool,), default, "bool")

    def get_list(self, key, default=_NO_DEFAULT):
        return self._typed(key, (list,), default, "list")

    def get_dict(self, key, default=_NO_DEFAULT):
        return self._typed(key, (dict,), default, "dict")

    def get_bytes(self, key, default=_NO_DEFAULT):
        return self._typed(key, (bytes,), default, "bytes")

    def has(self, key):
        key = _check_name(key, "key")
        with self._lock:
            if key in self._cache or key in self._dirty:
                return True
            if key in self._missing:
                return False
            if self._aggregate:
                if self._route_nolock(key) is not None:
                    return True
                self._missing.add(key)
                return False
        found = self._find_file(key)
        if found is not None:
            return True
        with self._lock:
            self._missing.add(key)
        return False

    def keys(self):
        if self._aggregate:
            # keys() 从索引免费(纯元数据, 不触片); 索引缺失时经路由原语自愈
            with self._lock:
                index = self._ensure_index_nolock() if self._index is not None                     else self._route_all_nolock()
                found = set(index)
                found.update(self._dirty)
                found.update(self._cache)
            return sorted(found)
        try:
            names = os.listdir(self._dir)
        except FileNotFoundError:
            names = []
        found = set()
        for name in names:
            for suffix in _SUFFIXES:
                if name.endswith(suffix):
                    stem = name[:-len(suffix)]
                    if stem and not stem.startswith("."):
                        found.add(stem)
                    break
        with self._lock:
            found.update(self._dirty)
            found.update(self._cache)
        return sorted(found)

    def delete(self, key):
        key = _check_name(key, "key")
        with self._lock:
            self._cache.pop(key, None)
            self._dirty.pop(key, None)
            self._missing.add(key)
            self._corrupt.discard(key)
            if key in self._inflight:
                # flush 在途: 此刻删文件会被在途写复活(白删)——打墓碑,
                # 由 flush 跳过该键并在尾部结算真正的删除
                self._tombstones.add(key)
                return _ok("removed")
            if self._aggregate:
                filename = self._route_nolock(key)  # 陈旧索引经原语自愈
                if filename is None:
                    return _ok("missing")
                self._index.pop(key, None)
                try:
                    self._remove_from_location_nolock(key, filename)
                    self._persist_index_nolock()
                except Exception as exc:
                    Debug.log("[SaveGame] delete(%r) failed: %s" % (key, exc))
                    return SaveResult(False, "failed", {"reason": str(exc)})
                return _ok("removed")
        removed = False
        for suffix in _SUFFIXES:
            try:
                os.remove(os.path.join(self._dir, key + suffix))
                removed = True
            except OSError:
                pass
        return _ok("removed" if removed else "missing")

    def set_write_through(self, flag):
        with self._lock:
            self._write_through = flag

    def snapshot_dirty(self):
        """scheduler 快照交接: 锁内取走全部 dirty(微秒级)。"""
        with self._lock:
            snapshot = self._dirty
            self._dirty = {}
            return snapshot

    def return_dirty(self, items):
        """IO 失败的键放回队列(不覆盖更新的排队值)。"""
        with self._lock:
            for key, value in items.items():
                if key not in self._dirty:
                    self._dirty[key] = value

    def dirty_count(self):
        with self._lock:
            return len(self._dirty)

    # ---- Mapping 协议与甜点 ----
    def __contains__(self, key):
        return self.has(key)

    def __len__(self):
        return len(self.keys())

    def __iter__(self):
        return iter(self.keys())

    def items(self):
        return [(key, self.get(key)) for key in self.keys()]

    def values(self):
        return [self.get(key) for key in self.keys()]

    def pop(self, key, default=_NO_DEFAULT):
        existed = self.has(key)
        if not existed:
            if default is _NO_DEFAULT:
                raise KeyError(key)
            return default
        value = self.get(key)  # 严格读: 损坏如实抛
        self.delete(key)
        return value

    def setdefault(self, key, default):
        try:
            return self.get(key)
        except (KeyError, SavegameError):
            self.set(key, default)
            return default

    def update(self, mapping):
        rejected = {}
        written = 0
        for key, value in dict(mapping).items():
            r = self.set(key, value)
            if r.ok:
                written += 1
            else:
                rejected[str(key)] = r.detail.get(
                    "reason", "rejected") if r.detail else "rejected"
        if rejected:
            return SaveResult(False, "partial",
                              {"written": written, "rejected": rejected})
        return SaveResult(True, "done", {"written": written})


# ============ 门面 SaveGameApi ============

class SaveGameApi(object):
    """inx.savegame 门面: 委托活动槽, 管理槽位, 暴露 batch/flush 原语。"""

    def __init__(self, root_dir):
        _adopt_legacy_private(root_dir)
        self._root = root_dir
        self._lock = _threading().RLock()
        self._slots = {}
        self._batch = False
        self._migrate = _MigrateApi(self)
        self._active = self.get_slot("default")

    # ---- 槽管理 ----
    def get_slot(self, name):
        name = _check_name(name, "slot")
        with self._lock:
            slot = self._slots.get(name)
            if slot is None:
                slot = Slot(name, os.path.join(self._root, name), self._lock,
                            write_through=not self._batch, game_dir=self._root)
                self._slots[name] = slot
            return slot

    def use_slot(self, name):
        target = self.get_slot(name)  # 名字在 get_slot 内归一
        with self._lock:
            previous = self._active
        if previous is not None and previous is not target:
            previous.flush()  # 切前落盘旧槽——锁外(与 delete_slot 对齐)
        with self._lock:
            if self._active is previous:
                self._active = target
        Debug.log("[SaveGame] active slot -> %s" % target.name)
        return SaveResult(True, "done",
                          {"from": previous.name if previous else None,
                           "to": target.name})

    def current_slot(self):
        return self._active.name if self._active is not None else None

    def list_slots(self):
        try:
            names = os.listdir(self._root)
        except FileNotFoundError:
            names = []
        found = set()
        for name in names:
            if _name_ok(name) and os.path.isdir(os.path.join(self._root, name)):
                found.add(name)
        found.add("default")
        return sorted(found)

    def delete_slot(self, name):
        name = _check_name(name, "slot")
        slot = self.get_slot(name)
        slot.flush()
        with self._lock:
            self._slots.pop(name, None)
            was_active = self._active is slot
        target = slot._dir
        removed = _rmtree(target)
        if was_active:
            with self._lock:
                self._active = Slot("default", os.path.join(self._root, "default"),
                                    self._lock, write_through=not self._batch,
                                    game_dir=self._root)
                self._slots["default"] = self._active
        if not removed:
            return _ok("missing")
        Debug.log("[SaveGame] slot %r deleted" % name)
        return _ok("removed")

    def delete_all_slots(self):
        failed = {}
        removed = 0
        for name in self.list_slots():
            r = self.delete_slot(name)
            if r.ok and r.status == "removed":
                removed += 1
            elif not r.ok:
                failed[name] = r.detail
        if failed:
            return SaveResult(False, "partial",
                              {"removed": removed, "failed": failed})
        return SaveResult(True, "done", {"removed": removed})

    def export_slot(self, name):
        slot = self.get_slot(name)
        data, skipped = {}, {}
        for key in slot.keys():
            try:
                data[key] = slot.get(key)
            except (KeyError, SavegameError) as exc:
                skipped[key] = str(exc)
        detail = {"keys": len(data)}
        if skipped:
            detail["skipped"] = skipped
        return SaveResult(True, "done", detail, data)

    def import_slot(self, name, mapping):
        """镜像导入: 精确装回这份存档——快照外的键删除(回档不完全就是
        完全不回档)。合并注入语义走 slot.update(mapping)。
        预检(键名/值可编码)全过才动手, 任一非法 → rejected 且零改动;
        先写后删(新值先落, 崩溃残留可重跑收敛); 重跑幂等。"""
        slot = self.get_slot(name)
        # 键归一(大小写不敏感契约) + 预检: 纯内存, 全过才动手, 任一非法
        # → rejected 且零改动("回档不完全就是完全不回档")
        normalized = {}
        try:
            for raw_key, value in mapping.items():
                key = _check_name(raw_key, "key")
                normalized[key] = _normalize_value(value)
        except ValueError as exc:
            Debug.log("[SaveGame] import_slot rejected: %s" % exc)
            return SaveResult(False, "rejected", {"reason": str(exc)})
        for key, value in normalized.items():
            try:
                _encode_value(value, slot._compress)
            except Exception as exc:
                Debug.log("[SaveGame] import_slot rejected at %r: %s"
                          % (key, exc))
                return SaveResult(False, "rejected",
                                  {"reason": str(exc), "key": key})
        slot.flush()  # 尾款先落, 快照外键计算基于盘上事实
        snapshot = set(normalized)
        # 先写: 新值先立住
        written = slot.update(normalized)
        if not written.ok:  # 写失败不进入删除(数据安全优先, 可重跑)
            return written
        # 后删: 快照外键
        removed = 0
        for key in list(slot.keys()):
            if key not in snapshot:
                slot.delete(key)
                removed += 1
        return SaveResult(True, "done", {"written": len(normalized),
                                         "removed": removed})

    def refresh(self, flush=True):
        """重新读盘。活动槽光标保持不变(Play 是什么槽位就什么槽位);
        槽目录已不存在时回 default 并在 detail 注明。"""
        if flush:
            self.flush_all()
        with self._lock:
            wanted = self._active.name if self._active is not None else "default"
            self._slots.clear()
            if not os.path.isdir(os.path.join(self._root, wanted)):
                fallback = wanted
                wanted = "default"
            else:
                fallback = ""
            self._active = Slot(wanted, os.path.join(self._root, wanted),
                                self._lock, write_through=not self._batch,
                                game_dir=self._root)
            self._slots[wanted] = self._active
        detail = {"fallback_from": fallback} if fallback else {}
        return SaveResult(True, "done", detail)

    # ---- KV 委托活动槽 ----
    def set(self, key, value):
        return self._active.set(key, value)

    def __setitem__(self, key, value):
        self._active[key] = value

    def __getitem__(self, key):
        return self._active[key]

    def get(self, key, default=_NO_DEFAULT):
        return self._active.get(key, default)

    def get_int(self, key, default=_NO_DEFAULT):
        return self._active.get_int(key, default)

    def get_float(self, key, default=_NO_DEFAULT):
        return self._active.get_float(key, default)

    def get_str(self, key, default=_NO_DEFAULT):
        return self._active.get_str(key, default)

    def get_bool(self, key, default=_NO_DEFAULT):
        return self._active.get_bool(key, default)

    def get_list(self, key, default=_NO_DEFAULT):
        return self._active.get_list(key, default)

    def get_dict(self, key, default=_NO_DEFAULT):
        return self._active.get_dict(key, default)

    def get_bytes(self, key, default=_NO_DEFAULT):
        return self._active.get_bytes(key, default)

    def has(self, key):
        return self._active.has(key)

    def keys(self):
        return self._active.keys()

    def delete(self, key):
        return self._active.delete(key)

    def pop(self, key, default=_NO_DEFAULT):
        return self._active.pop(key, default)

    def setdefault(self, key, default):
        return self._active.setdefault(key, default)

    def update(self, mapping):
        return self._active.update(mapping)

    def __contains__(self, key):
        return self._active.has(key)

    def __len__(self):
        return len(self._active.keys())

    def __iter__(self):
        return iter(self._active.keys())

    def items(self):
        return self._active.items()

    def values(self):
        return self._active.values()

    def flush(self):
        return self._active.flush()

    # ---- batch/flush 原语(scheduler 组件与用户代码平权消费) ----
    def batch(self, flag):
        """True: 批量模式(关写透, dirty 排队); False: 落尾款并恢复写透。"""
        flag = bool(flag)
        with self._lock:
            self._batch = flag
            slots = list(self._slots.values())
            for slot in slots:
                slot.set_write_through(not flag)
        if not flag:
            self.flush_all()
        Debug.log("[SaveGame] batch mode %s" % ("on" if flag else "off"))
        return _ok("done")

    def flush_all(self):
        """全槽落盘(batch 模式下有尾款; 写透模式恒 done)。"""
        with self._lock:
            slots = list(self._slots.values())
        written, failed = 0, {}
        for slot in slots:
            r = slot.flush()
            if r.ok:
                written += r.detail.get("written", 0) if r.detail else 0
            else:
                failed[slot.name] = r.detail.get("failed", {}) if r.detail else {}
        if failed:
            return SaveResult(False, "partial",
                              {"written": written, "failed": failed})
        return SaveResult(True, "done", {"written": written})

    def dirty_count(self):
        with self._lock:
            slots = list(self._slots.values())
        return sum(slot.dirty_count() for slot in slots)

    def create_worker(self):
        """新建调度 worker(未启动)。官方组件与用户自定义调度平权消费:
        configure/start/flush_now/status/stop 全公开。"""
        return _FlushWorker(self)

    def stats(self):
        with self._lock:
            slots = list(self._slots.values())
        active = self._active
        return {
            "root": self._root,
            "batch_mode": self._batch,
            "write_through": not self._batch,
            "slots_on_disk": len(self.list_slots()),
            "slots_instantiated": len(slots),
            "active_slot": self.current_slot(),
            "active_keys": len(active.keys()) if active else 0,
            "dirty": self.dirty_count(),
            "compress": bool(active._compress) if active else False,
            "aggregate": bool(active._aggregate) if active else False,
        }

    @property
    def migrate(self):
        """一次性形态迁移: compress/plain(slot=None, defaults=True)。"""
        return self._migrate

    # ---- 生命周期 ----
    def shutdown(self):
        try:
            self.flush_all()  # unload 兜底(batch 模式尾款)
        except Exception as exc:
            Debug.log("[SaveGame] shutdown flush error: %s" % exc)


def _rmtree(target):
    import shutil as _shutil
    if not os.path.isdir(target):
        return False
    try:
        _shutil.rmtree(target)
        return True
    except OSError as exc:
        Debug.log("[SaveGame] rmtree %s failed: %s" % (target, exc))
        return False


# ============ 迁移(一次性整槽重编码) ============

class _MigrateApi(object):
    """sg.migrate: compress()/plain()。逐键原子(写新->验->删旧),
    幂等重跑收敛; 失败键不阻塞不回滚(旧态可读, 重跑/下次 set 治愈)。"""

    def __init__(self, api):
        self._api = api

    def compress(self, slot=None, defaults=True):
        return self._run(True, slot, defaults)

    def plain(self, slot=None, defaults=True):
        return self._run(False, slot, defaults)

    def aggregate(self, slot=None, defaults=True):
        """散 -> 聚合(四步写序: 数据->索引->state->清理)。幂等重跑收敛。"""
        return self._run_layout(True, slot, defaults)

    def scatter(self, slot=None, defaults=True):
        """聚合 -> 散(对称)。幂等重跑收敛。"""
        return self._run_layout(False, slot, defaults)

    def _run_layout(self, target, slot, defaults_flag):
        api = self._api
        names = [slot] if slot else api.list_slots()
        slots_detail, any_failed = {}, False
        for name in names:
            detail = self._migrate_layout_slot(api.get_slot(name), target)
            slots_detail[name] = detail
            if detail["failed"]:
                any_failed = True
        defaults_updated = False
        if defaults_flag:
            defaults_updated = _update_defaults(
                api._root, {"aggregate": target})
        verb = "aggregate" if target else "scatter"
        total_c = sum(d["converted"] for d in slots_detail.values())
        total_s = sum(d["skipped"] for d in slots_detail.values())
        total_f = sum(len(d["failed"]) for d in slots_detail.values())
        Debug.log("[SaveGame] migrate %s: %d converted, %d skipped, %d failed"
                  " (%d slots)" % (verb, total_c, total_s, total_f, len(names)))
        return SaveResult(not any_failed,
                          "partial" if any_failed else "done",
                          {"slots": slots_detail,
                           "defaults_updated": defaults_updated})

    def _migrate_layout_slot(self, slot, target):
        """布局迁移。失败 ≡ 中断: 任一键失败则 state 不翻、不清理,
        旧世界完整可读、重跑完整重做(与布局迁移同一中断安全契约);
        清理谓词 = 旧家文件 ≠ 该键当前路由(旧新家同名的 bytes 恒不删)。"""
        slot.flush()  # 尾款先落, 从盘上事实出发
        with slot._lock:
            keys = slot.keys()
            if slot._aggregate == target:
                return {"converted": 0, "skipped": len(keys), "failed": {}}
            failed = {}
            if target:
                # 散 -> 聚合: 读值 -> 写片+索引 -> state -> 清散残留
                data = {}
                for key in keys:
                    try:
                        data[key] = slot.get(key)
                    except Exception as exc:
                        failed[key] = str(exc)
                if failed:
                    # 读不齐不动手(零残像): 失败≡中断, state 不翻, 重跑完整重做
                    return {"converted": 0, "skipped": 0, "failed": failed}
                slot._enter_layout(True)
                try:
                    failed = slot._store_batch_nolock(data)  # 唯一批量路径: 每片一次写
                except Exception as exc:
                    failed = {key: str(exc) for key in data}
                if failed:
                    # 失败 ≡ 中断: 回滚内存视图, state 不翻, 旧世界可读, 重跑重做
                    slot._exit_layout(False)
                    return {"converted": 0, "skipped": 0, "failed": failed}
                slot._persist_state_nolock()
                for key in data:  # 清理: 删已被索引覆盖的旧散文件
                    new_file = slot._index.get(key)
                    for suffix in _SUFFIXES:
                        fname = key + suffix
                        if fname == new_file:
                            continue  # bytes 新家即散文件(路由未变), 不删
                        try:
                            os.remove(os.path.join(slot._dir, fname))
                        except OSError:
                            pass
                converted = len(data)
            else:
                # 聚合 -> 散: 读值 -> 写散文件 -> state -> 清片+索引
                index = slot._ensure_index_nolock()
                for key in list(index):
                    try:
                        suffix, payload = _encode_value(
                            slot.get(key), slot._compress)  # 唯一编码器
                        os.makedirs(slot._dir, exist_ok=True)
                        _atomic_write(os.path.join(slot._dir, key + suffix),
                                      payload)
                    except Exception as exc:
                        failed[key] = str(exc)
                if failed:
                    # 失败 ≡ 中断: state(聚合)不翻不清理, 重跑完整重做
                    return {"converted": 0, "skipped": 0, "failed": failed}
                converted = len(index)
                slot._enter_layout(False)
                slot._persist_state_nolock()
                for name in index.values():
                    if not slot._is_pack_name(name):
                        continue  # bytes 新旧家同名(路由未变), 恒不删
                    try:
                        os.remove(os.path.join(slot._dir, name))
                    except OSError:
                        pass
                try:
                    os.remove(os.path.join(
                        slot._dir, _PRIVATE_DIR, _INDEX_FILE))
                except OSError:
                    pass
            return {"converted": converted, "skipped": 0, "failed": failed}

    def _run(self, target_compress, slot, defaults_flag):
        api = self._api
        names = [slot] if slot else api.list_slots()
        slots_detail, any_failed = {}, False
        for name in names:
            detail = self._migrate_slot(api.get_slot(name), target_compress)
            slots_detail[name] = detail
            if detail["failed"]:
                any_failed = True
        defaults_updated = False
        if defaults_flag:
            defaults_updated = _update_defaults(
                api._root, {"compress": target_compress})
        verb = "compress" if target_compress else "plain"
        total_c = sum(d["converted"] for d in slots_detail.values())
        total_s = sum(d["skipped"] for d in slots_detail.values())
        total_f = sum(len(d["failed"]) for d in slots_detail.values())
        Debug.log("[SaveGame] migrate %s: %d converted, %d skipped, %d failed"
                  " (%d slots)" % (verb, total_c, total_s, total_f, len(names)))
        return SaveResult(not any_failed,
                          "partial" if any_failed else "done",
                          {"slots": slots_detail,
                           "defaults_updated": defaults_updated})

    def _migrate_slot(self, slot, target):
        slot.flush()  # batch 尾款先落, 保证从盘上事实出发
        with slot._lock:
            if slot._aggregate:
                return self._migrate_slot_packs(slot, target)
            return self._migrate_slot_files(slot, target)

    def _migrate_slot_packs(self, slot, target):
        """聚合态的 compress/plain: 逐片重编码(.pack<->.pack.z)+索引更新。
        明细以存档键为单位(片失败=该片键展开, 原因带片名); 孤儿片(索引
        无主)转码失败静默——其可见性由 diagnose() 承担。"""
        index = slot._ensure_index_nolock()
        owners = {}  # 文件名 -> [键] (索引反查; 孤儿片无条目)
        for key, fname in index.items():
            owners.setdefault(fname, []).append(key)
        converted, skipped, failed = 0, 0, {}
        new_pack = _PACK_SUFFIX_Z if target else _PACK_SUFFIX
        old_pack = _PACK_SUFFIX if target else _PACK_SUFFIX_Z
        new_bin = _SUFFIX_BIN_Z if target else _SUFFIX_BIN
        old_bin = _SUFFIX_BIN if target else _SUFFIX_BIN_Z
        try:
            names = os.listdir(slot._dir)
        except FileNotFoundError:
            names = []
        moves = {}
        for name in names:
            keys_hit = owners.get(name, [])
            if name.endswith(old_pack):
                new_name = name[:-len(old_pack)] + new_pack
                try:
                    entries = _read_pack(os.path.join(slot._dir, name))
                    _write_pack(os.path.join(slot._dir, new_name), entries)
                    os.remove(os.path.join(slot._dir, name))
                    moves[name] = new_name
                    converted += len(keys_hit)
                except Exception as exc:
                    for key in keys_hit:
                        failed[key] = "pack %s: %s" % (name, exc)
            elif name.endswith(old_bin):
                stem = name[:-len(old_bin)]
                new_name = stem + new_bin
                try:
                    with open(os.path.join(slot._dir, name), "rb") as stream:
                        raw = stream.read()
                    data = raw if target else _zlib().decompress(raw)
                    payload = _zlib().compress(data, 6) if target else data
                    _atomic_write(os.path.join(slot._dir, new_name), payload)
                    os.remove(os.path.join(slot._dir, name))
                    moves[name] = new_name
                    converted += 1
                except Exception as exc:
                    failed[stem] = "scatter %s: %s" % (name, exc)
            elif name.endswith(new_pack) or name.endswith(new_bin):
                skipped += len(keys_hit)
        for key, filename in list(index.items()):
            if filename in moves:
                index[key] = moves[filename]
        slot._packs = {}  # 片缓存失效(文件名已变)
        slot._sealed = set()
        if slot._compress != target or not slot._state_persisted:
            slot._compress = target
            slot._persist_state_nolock()
        slot._persist_index_nolock()
        return {"converted": converted, "skipped": skipped, "failed": failed}

    def diagnose(self, slot=None):
        """迁移诊断(纯读, 无副作用): 识别四类静默异常并给出建议。
        - orphan_pack   索引无主的片(含可恢复键数) / 散态下存在的片
        - dangling_index 索引指向不存在的文件(读时自愈, 可 refresh 立即重建)
        - layout_residue 当前布局不应存在的另一布局残留文件
        - corrupt_pack  解析失败的片
        """
        api = self._api
        names = [slot] if slot else api.list_slots()
        report = {}
        for name in names:
            s = api.get_slot(name)
            with s._lock:
                issues = []
                try:
                    files = os.listdir(s._dir)
                except FileNotFoundError:
                    files = []
                packs = [f for f in files if s._is_pack_name(f)]
                scatters = [f for f in files
                            if f.endswith(_SUFFIX_BIN) or f.endswith(_SUFFIX_BIN_Z)
                            or f.endswith(_SUFFIX_JSON) or f.endswith(_SUFFIX_JSON_Z)]
                if s._aggregate:
                    index = s._ensure_index_nolock()
                    owned = set(index.values())
                    for f in packs:
                        if f not in owned:
                            try:
                                keys_n = len(_read_pack(s._pack_path(f)))
                            except Exception:
                                keys_n = None
                            issues.append({
                                "issue": "orphan_pack", "file": f, "keys": keys_n,
                                "advice": "run aggregate to reclaim, or rerun "
                                          "scatter to clean up"})
                    for key, fname in index.items():
                        if fname not in files:
                            issues.append({
                                "issue": "dangling_index", "key": key,
                                "file": fname,
                                "advice": "self-heals on read; refresh() "
                                          "rebuilds the index now"})
                    for f in scatters:
                        stem = _stem_of_scatter(f)
                        if stem == f:
                            stem = f[:-len(_SUFFIX_JSON_Z)] if f.endswith(_SUFFIX_JSON_Z) \
                                else f[:-len(_SUFFIX_JSON)]
                        if index.get(stem) != f:
                            issues.append({
                                "issue": "layout_residue", "file": f,
                                "advice": "rerun aggregate to collect, or "
                                          "remove manually if unwanted"})
                        _report_corrupt_scatter(s, f, issues)
                else:
                    for f in packs:
                        issues.append({
                            "issue": "orphan_pack", "file": f, "keys": None,
                            "advice": "rerun scatter to clean, or aggregate "
                                      "to reclaim"})
                    for f in scatters:
                        _report_corrupt_scatter(s, f, issues)
                for f in packs:
                    if s._aggregate:
                        try:
                            _read_pack(s._pack_path(f))
                        except Exception:
                            if not any(i["issue"] == "corrupt_pack"
                                       and i["file"] == f for i in issues):
                                issues.append({
                                    "issue": "corrupt_pack", "file": f,
                                    "advice": "keys in it read as corrupt; "
                                              "restore from backup or "
                                              "rewrite the keys"})
                report[name] = issues
        total = sum(len(v) for v in report.values())
        return SaveResult(True, "done",
                          {"slots": report, "issues": total},
                          report)

    def _migrate_slot_files(self, slot, target):
        converted, skipped, failed = 0, 0, {}
        for key in slot.keys():
            found = slot._find_file(key)
            if found is None:
                skipped += 1
                continue
            path, suffix = found
            if _suffix_is_z(suffix) == target:
                skipped += 1
                continue
            try:
                with open(path, "rb") as stream:
                    raw = stream.read()
                value = _decode_value(suffix, raw)
                new_suffix, data = _encode_value(value, target)
                _atomic_write(os.path.join(slot._dir, key + new_suffix), data)
                os.remove(path)  # 写新已验, 删旧
                converted += 1
                slot._cache[key] = value
            except Exception as exc:
                failed[key] = str(exc)
        if slot._compress != target or not slot._state_persisted:
            slot._compress = target
            slot._persist_state_nolock()
        return {"converted": converted, "skipped": skipped, "failed": failed}


# ============ Scheduler(官方组件 + 隔离线程) ============

class _FlushWorker(object):
    """隔离线程: 策略判定 + 快照交接 + 锁外 IO。
    异常全捕获(不越界); 死亡后 status 如实可见, 处置权在用户。
    IO 全部经由 Slot.flush 的快照协议(锁内微秒交接, 锁外读写盘)。"""

    def __init__(self, api):
        self._api = api
        self._interval = 3.0
        self._threshold_keys = 0
        self._stop = _threading().Event()
        self._wake = _threading().Event()
        self._thread = None
        self._dead_reason = None
        self._flushed_count = 0

    def configure(self, interval=None, threshold_keys=None):
        if interval is not None:
            self._interval = max(0.0, float(interval))
        if threshold_keys is not None:
            self._threshold_keys = max(0, int(threshold_keys))
        return self.status()

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self.status()
        self._stop.clear()
        self._dead_reason = None
        self._thread = _threading().Thread(
            target=self._loop, daemon=True, name="mgs-savegame-scheduler")
        self._thread.start()
        return self.status()

    def stop(self):
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        return self.status()

    def flush_now(self):
        self._wake.set()

    def status(self):
        alive = self._thread is not None and self._thread.is_alive()
        return {
            "alive": alive,
            "dead_reason": self._dead_reason,
            "interval": self._interval,
            "threshold_keys": self._threshold_keys,
            "dirty": self._api.dirty_count(),
            "flushed_count": self._flushed_count,
        }

    def _loop(self):
        try:
            last = _monotonic()
            while not self._stop.is_set():
                self._wake.wait(timeout=0.25)
                woken = self._wake.is_set()
                self._wake.clear()
                now = _monotonic()
                due = self._interval > 0 and (now - last) >= self._interval
                over = self._threshold_keys > 0 \
                    and self._api.dirty_count() >= self._threshold_keys
                if woken or due or over:
                    self._api.flush_all()
                    self._flushed_count += 1
                    last = now
        except Exception as exc:  # 异常不越界: 死亡可见, 处置权在用户
            self._dead_reason = str(exc)
            Debug.log("[SaveGame] scheduler thread died: %s" % exc)


_MONOTONIC = None


def _monotonic():
    global _MONOTONIC
    if _MONOTONIC is None:
        import time as _m
        _MONOTONIC = _m.monotonic
    return _MONOTONIC()


# MgsSavegameScheduler 组件(官方参考壳, 非必需钥匙; MGS = MervinGameStudio,
# 前缀防第三方组件类型域撞名)。自定义调度直接消费 sg.batch/flush/
# create_worker 原语, 与本组件完全平权。scheduler 是纯优化件:
# 缺席 = 写透安全默认。要求引擎 >=0.4,<0.5(0.4.1 起插件组件进 Player 构建)。


class MgsSavegameScheduler(InxComponent):
    """挂载 = 激活批量模式 + 策略线程; 销毁 = 落尾款恢复写透。
    字段 Inspector 可调(0 = 禁用该项)。"""

    interval: float = 3.0
    threshold_keys: int = 0

    def awake(self):
        self._worker = None  # 激活延迟到 start(编辑器加载场景也跑 awake)

    def start(self):
        sg = getattr(inx, "savegame", None)
        if sg is None:
            Debug.log("[SaveGame] scheduler: inx.savegame unavailable")
            return
        sg.batch(True)
        self._worker = sg.create_worker()
        self._worker.configure(interval=self.interval,
                               threshold_keys=self.threshold_keys)
        self._worker.start()
        Debug.log("[SaveGame] scheduler on: interval=%s threshold=%s"
                  % (self.interval, self.threshold_keys))

    def on_destroy(self):
        worker = getattr(self, "_worker", None)
        if worker is not None:
            worker.stop()
        sg = getattr(inx, "savegame", None)
        if sg is not None:
            sg.batch(False)  # 尾款落盘 + 恢复写透
        Debug.log("[SaveGame] scheduler off")

    def flush_now(self):
        worker = getattr(self, "_worker", None)
        if worker is not None:
            worker.flush_now()

    def status(self):
        worker = getattr(self, "_worker", None)
        if worker is None:
            return {"alive": False, "dead_reason": None}
        return worker.status()


# ============ root 解析与 preload ============

def _game_dir(context):
    """目录名 = BuildSettings.game_name（产品名，编辑器/Player 同一权威，
    经引擎 runtime-neutral 访问层读取——Player 包内同路径随包分发）。
    兜底：Player 用 exe 名（兜底而已，改名不改变存档目录），编辑器用工程文件夹名。"""
    name = ""
    try:
        from Infernux.engine.build_settings import load_build_settings
        name = str(load_build_settings(
            context.project_root).get("game_name") or "")
    except Exception:
        name = ""
    if name:
        suffix = "" if getattr(context, "runtime", False) else ".editor"
        return _safe_dir(name) + suffix
    if getattr(context, "runtime", False):
        try:
            import sys
            stem = os.path.splitext(os.path.basename(sys.executable or ""))[0]
        except Exception:
            stem = ""
        return _safe_dir(stem or "game")
    folder = ""
    if context.project_root:
        folder = os.path.basename(os.path.normpath(context.project_root))
    return _safe_dir(folder or "game") + ".editor"


def _saves_root(context):
    override = os.environ.get(_ROOT_ENV, "").strip()
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA", "").strip()
    if not base:
        # 无 LOCALAPPDATA 平台(Android Player 等): 引擎持久化路径(app 私有可写目录)。
        # Android 上 ~/AppData/Local 解析为 /data/AppData/... 不可写。
        pdp = ""
        try:
            app = getattr(inx, "application", None)
            if app is not None:
                pdp = str(app.Application.persistent_data_path() or "")
        except Exception:
            pdp = ""
        if pdp:
            base = pdp
        else:
            base = os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "Infernux", "Saves", _game_dir(context))


class SaveGamePreload(InxPreload):
    def preload(self, context: PreloadContext) -> None:
        root = _saves_root(context)
        self._api = SaveGameApi(root)
        inx.savegame = self._api
        Debug.log("[SaveGame] preload ok: root=%s mode=%s"
                  % (root, "player" if context.runtime else "editor"))
        if not context.runtime:
            # 编辑器面板 + MCP 操作集: 注册必须同步做(PluginManager 临时
            # import 路径仅在 preload 期有效——mcp 插件同款约束);
            # 各自失败不拖垮存档注入。
            try:
                import savegame_browser_panel  # noqa: F401  装饰器在 import 期收集
                Debug.log("[SaveGame] editor panel registered")
            except Exception as exc:
                Debug.log("[SaveGame] editor panel unavailable: %s" % exc)
            try:
                import savegame_operations
                n = savegame_operations.register_savegame_operations()
                self._unregister_ops = \
                    savegame_operations.unregister_savegame_operations
                Debug.log("[SaveGame] %d mcp operations registered" % n)
            except Exception as exc:
                Debug.log("[SaveGame] mcp operations unavailable: %s" % exc)

    def unload(self) -> None:
        unregister = getattr(self, "_unregister_ops", None)
        if unregister is not None:
            try:
                unregister()
            except Exception:
                pass
            self._unregister_ops = None
        api = getattr(self, "_api", None)
        if api is not None:
            api.shutdown()
        try:
            if api is not None and getattr(inx, "savegame", None) is api:
                delattr(inx, "savegame")
        except Exception:
            pass
