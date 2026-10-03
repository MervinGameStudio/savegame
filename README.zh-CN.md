# savegame

[English](README.md) | **简体中文**

面向 **Infernux** 引擎的写透式槽位键值存档库，零第三方依赖。

> **状态：1.0 之前。** 库功能完整，但公开 API 在 0.x 版本间仍可能变动，
> 请相应锁定引擎/插件版本。
>
> **数据安全：** 这是 1.0 之前的软件——尽管有全量测试覆盖，仍可能存在
> 未发现的 bug 导致存档损坏或丢失。**请为存档保留备份**，升级插件与执行
> 布局迁移前尤其如此。

> **定位：辅助你管理存档的管家，不是户主。** 存档的位置、内容、结构都是
> 你的；库只提供读写、组织、调度、迁移四类服务。默认路径上一切安全
> （写透），性能与形态是你的显式选择。

- **写透默认**——`set` 变化即落盘，崩溃最多丢最后一次已确认变化；同值写零 IO
- **批量原语**——`batch(True)` 排队脏键，`flush()` 落盘（聚合布局实测 4000 键
  约 116ms，含防 O(N²) 回归守卫）
- **槽位**——隔离存档空间；编辑器域与 Player 域天然分离
- **文件即值**——每个键一个明文文件，打开能看、能手改；JSON 类型 + 顶层
  `bytes`，没有信封没有黑盒
- **两种形态自由切换**——散放（一键一文件）或聚合（类型分片 + 可再生索引），
  压缩是正交的槽级状态；`migrate` 四向迁移中断安全、重跑收敛，
  `diagnose()` 报告静默异常
- **Savegame Browser**——编辑器面板（Window 菜单），同一公开 API 浏览/暂存
  /编辑存档，保存有显式确认
- **MCP 操作**——八个操作（`mervingamestudio.savegame.*`），AI agent 免探针
  脚本驱动存档调试
- **内置调度组件**——挂载即批量 + 定期/阈值落盘；缺席时写透兜底

**安装**：推荐用编辑器导入发布的 `.inxpkg`；或把 [`package/`](package/) 内容
拷入工程 `Packages/mervingamestudio/savegame/` 后重启编辑器。要求引擎 0.4.x；引擎低于 0.4.1 时内置调度组件不进 Player 构建（写透语义不受影响）。

完整手册（契约、批量、迁移、面板、MCP、坑位）：
[package/plugin_pages/usage.zh-CN.md](package/plugin_pages/usage.zh-CN.md)
（[English](package/plugin_pages/usage.md)），随插件内嵌并跟随编辑器语言。
发布记录：[CHANGELOG.md](CHANGELOG.md)。

**测试**（150 项，引擎无关）：

```sh
python -m unittest discover -s package/tests
```

**协议**：[MIT](LICENSE)。
