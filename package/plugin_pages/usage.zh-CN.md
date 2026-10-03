# 使用说明

面向 Infernux 引擎的写透式槽位键值存档库。零第三方依赖，要求引擎 0.4.x。

> **定位：辅助你管理存档的管家，不是户主。** 存档的位置、内容、结构都是
> 你的；库只提供读写、组织、调度、迁移四类服务。默认路径上一切安全
> （写透），性能与形态是你的显式选择。

本指南是一座阶梯：每一级只加一个能力，并告诉你什么时候才真的需要它。
**只到第 1 级，就足够一个小游戏拥有完整的存档系统**——往上的每一级都是
可选项。

## 第 1 级 · 存一个值，读回来

第一分钟就需要的能力：把进度留到下一次会话。

```python
import infernux as inx

sg = getattr(inx, "savegame", None)
if sg is not None:
    sg.set("high_score", 700)          # -> written: 变化即落盘
    sg.get_int("high_score")           # 严格读: 缺失抛 KeyError
    sg.get_int("high_score", 0)        # 回退读: 缺失/损坏 -> 0
```

这就是小游戏的全部存档契约。每个键一个明文文件——`score.json` 打开就是
`700`，没有信封、没有附件，任何工具都能打开查看、手改。

让文件树保持干净的几条规则：

- 键允许 `a-z 0-9 _ . -`（可含点，如 `settings.level`），不可点开头。
  键空间**大小写不敏感**（归一为小写）——变体拼写是更新，不是新键。
- 值：JSON 类型 + 顶层 `bytes`（`portrait.bin`）。边界情况（tuple、
  `NaN`、内嵌 bytes）见附录速查。

第 1 级再多一点，仍然都是基础：

```python
sg.get_str("player_name", "Newbie")    # typed 回退读家族
sg.get_dict("settings", {})
sg.get_list("quests", [])
sg.get_bytes("portrait", b"")
sg.has("gold")                         # True
sg.delete("gold")                      # -> removed; 再删 -> missing
sg.pop("gold", None)                   # 读取并删除
sg.update({"hp": 30, "mp": 10})        # 批量写（写透模式逐键落盘）
```

`sg` 同时是一个 Python 映射：`in`、`len`、迭代、`sg["gold"] = 120`。
**小游戏到此即可发布。**

## 第 2 级 · 多存档位（槽位）

*当你做出"从 2 号存档继续"这样的存档位 UI 时，才需要这一级。*

槽位是一个隔离的存档空间。游戏起步在 `"default"`；切槽前旧槽自动落盘。
编辑器域与 Player 域是两个池（`.editor` 后缀），编辑器里测试永远碰不到
玩家数据。

```python
sg.use_slot("save_1")                  # 切换: 旧槽先落盘
sg.set("chapter", 3)

sg.list_slots()                        # ["default", "save_1"]
sg.current_slot()                      # "save_1"

slot = sg.get_slot("save_2")           # 独立句柄——不会移动
slot.set("chapter", 1)                 # 游戏的当前活动槽

sg.delete_slot("save_2")               # 幂等: removed / missing
```

`use_slot` 是"玩家选了 N 号位"；`get_slot` 是"动另一个槽但不打扰当前槽"。

## 第 3 级 · 备份、回档与转移

*当你需要云同步、Mod 支持或 QA 测试档时，才需要这一级。*

```python
snapshot = sg.export_slot("save_1")    # 整槽导出为明文 dict（恒不编码）
sg.import_slot("save_1", snapshot)     # 镜像回档
```

**`import_slot` 是镜像不是合并**：导入后槽位与快照完全相等——快照里没有
的键会被**删除**。回档不完全就是完全不回档。要把数据*合并*进现有槽位，
用 `slot.update(mapping)`——另一个工具另一个用途。

导入是全有或全无：预检（键名归一、可编码）任一不过则整体 rejected 且零
改动；中断的导入直接重跑即可收敛。

## 第 4 级 · 高频写入（批量模式）

*当有东西每帧都在保存——位置、计数器、遥测——时，才需要这一级。*

默认是写透：`set` 在值**变化**的瞬间落盘（同值写零 IO）。批量模式是自选
原语：

```python
sg.batch(True)                         # 关写透, 脏键排队
sg.set("pos_x", 12.5)                  # -> queued（还没落盘）
sg.flush()                             # 落当前槽的脏键
sg.flush_all()                         # 或所有槽
sg.batch(False)                        # 落尾款 + 恢复写透
sg.dirty_count()                       # 当前排队数
```

代价是显式的：**丢失窗口 = 上次 flush 之后的全部排队**。关键进度后手动
flush。聚合布局下批量落盘是每片一次写——实测 4000 键约 116ms（带常驻
回归守卫）；`update(大dict)` 与 `import_slot` 在批量模式下走同一条路。
`delete` 与进行中的落盘并发是安全的（删除不会被并发写复活）。

或者直接挂 **MgsSavegameScheduler** 组件：挂载即激活批量，销毁即落尾款
恢复写透。Inspector 可调字段：`interval`（秒，0=关）、`threshold_keys`
（0=关）。它是纯优化件——没有它，写透照样保住每一次已确认的变化。
后台 worker 异常不会越界：若死亡，`status()` 报告 `dead_reason`，两种
处置（重启它，或 `batch(False)` 回写透）都会先落盘排队数据。

## 第 5 级 · 存储形态（压缩、聚合、迁移）

*当存档长大——小键成群、目录要整洁、要可携带——才需要这一级。
它不是性能开关。*

两条正交的轴、四个方向，随时切换：

| 轴 | 调用 | 改变什么 |
|---|---|---|
| 形态 | `migrate.compress()` / `migrate.plain()` | 整槽 zlib 开/关（`.z` 文件） |
| 布局 | `migrate.aggregate()` / `migrate.scatter()` | 散放 ↔ 类型分片 |

聚合布局把值收进类型分片的明文片（`int-0.pack`、`str-0.pack`……JSONL，
人可读），bytes 恒散放。它的索引（`<slot>/.savegame/index.json`）**可再生**
——丢失或过期都会被扫片重建，数据永不依赖索引。单片护栏 1MB，溢出开新片。

迁移性质（四向通用）：逐键原子（写新→验→删旧）；任何点中断旧态都完整
可读、重跑收敛（已转键记 skipped）；失败键不阻塞不回滚——留旧态可读，
重跑或该键下次 set 自然治愈。可选小端口：`slot="save_a"` 限单槽、
`defaults=False` 不动出生默认。明细按槽报告 `{converted, skipped, failed}`。

未迁移的存档始终可读可写——布局混用时读写自动适配；迁移是彻底统一、
防手滑编辑的手段。

**`sg.migrate.diagnose()`** 是纯读扫描，报告四类静默异常——孤儿片、索引
悬空、布局残留、损坏片——各附处置建议；健康槽返回空清单。

## 工具箱

按需翻阅的页面，不是阶梯的一级：

- **存档在哪**——根路径解析：环境变量 `MGS_SAVES_ROOT` > 默认
  `%LOCALAPPDATA%/Infernux/Saves/<game_name>`（Linux/macOS
  `~/AppData/Local`；Android 走引擎持久化目录）。`<game_name>` 跟随
  Build Settings 的产品名——与给打包 exe 命名的是同一个权威，编辑器/
  Player 两域一致；玩家改名 exe 不影响存档位置。

  ```text
  Saves/<game>/                     .editor 后缀 = 编辑器池
    .savegame/defaults.json          游戏级出生默认
    <slot>/
      .savegame/state.json           槽级事实 {"compress", "aggregate"}
      score.json / portrait.bin      明文值（或 .z 压缩变体）
      int-0.pack ...                 聚合分片（聚合态）
      .savegame/index.json           可再生路由索引
  ```

- **Savegame Browser**（编辑器面板，Window 菜单）——编辑器存档池的可视化
  浏览与编辑：左键表、右编辑＋暂存清单；页脚 Discard/Save 常驻可见；行
  右键菜单（复制键/值、暂存删除）；暂存逐条撤销；搜索、类型过滤、排序；
  超 200 键虚拟化渲染。修改先进暂存，点 Save 并确认才落盘——Play 中的
  游戏立刻可见。bytes 只读（文件即值）。所有操作走与游戏代码相同的公开
  API。

- **AI agent：MCP 操作**——引擎 MCP 网关上的八个操作，agent 免探针脚本
  驱动存档调试：`mervingamestudio.savegame.slots / keys / get / stats /
  diagnose`（查询）与 `set / delete / flush`（命令，走引擎
  `runtime.write` 授权）。`slot` 缺省 `"default"`；bytes 以
  `{"type":"bytes","hex":...}` 传输（≤4MB）。

## 附录

### API 契约

| 线 | 规则 |
|---|---|
| 读 | `get(key)` 严格：缺失 `KeyError`、损坏 `SavegameError`；`get(key, default)` 回退到底；typed 严格读类型不符加抛 `TypeError` |
| 变更/IO | 返回 `SaveResult`（`ok/status/detail/data`；真值即 ok） |
| 异常 | = 编程错误（非法 key/slot 名 `ValueError`） |

`SaveResult.status`：set → `written` / `queued` / `rejected` / `failed`；
flush → `done` / `partial`；删除 → `removed` / `missing`。

`refresh(flush=True)` 先落盘再以盘为真相重读，**保留活动槽光标**（槽目录
消失回 default 并在 detail 注明）。`refresh(flush=False)` 丢弃缓存**和未
落盘的排队写**——只在确认放弃内存态时使用。

### 值域边界

tuple 归一为 list；`bytearray` 归一为 `bytes`；`NaN`/`Infinity` 拒绝；
dict/list 内嵌 bytes 不支持——拆成顶层键。

### 坑位

- **`awake()` 不只在 Play 时执行**：编辑器每次加载场景也会跑。存档初始
  化/迁移写在这里的话会反复执行——要么保持幂等（跑多少次结果都一样），
  要么用存档里的首跑标记守卫，只执行一次。
- **api 单例跨 Play 存活**：编辑器域里 `inx.savegame` 建一次就用到底，
  值缓存也跟着活。停止后再 Play，第一次 set 与上次相同的值会因"无变化"
  跳过写盘——数据本来就在盘上，无实际影响，只是别期待看到写盘动作。
- **批量的丢失窗口**：批量模式下，上次 `flush()` 之后排队未落盘的变更在
  进程崩溃时会丢——这是批量换性能的显式代价，关键进度后手动 `sg.flush()`。
- **装/更新插件后先重启编辑器再 Play**：引擎域重载会洗掉 `inx.*` 注入，
  不重启的话 Play 里拿不到 `inx.savegame`。

### 速查

**KV**（活动槽）：`set / get / get_int / get_float / get_str / get_bool /
get_list / get_dict / get_bytes / has / keys / delete / pop / setdefault /
update`，dict 语法，Mapping 协议。

**槽位**：`get_slot / use_slot / current_slot / list_slots / delete_slot /
delete_all_slots / export_slot / import_slot / refresh`。

**原语**：`batch / flush / flush_all / dirty_count / create_worker /
stats`。

**迁移**：`migrate.compress / plain / aggregate / scatter / diagnose`。

**测试套**（150 项，引擎无关）：

```sh
python -m unittest discover -s Packages/mervingamestudio/savegame/tests
```
