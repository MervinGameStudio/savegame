# savegame

**English** | [简体中文](README.zh-CN.md)

A write-through, slot-based key-value savegame library for the
**Infernux** engine. Zero third-party dependencies.

> **Status: pre-1.0.** The library is fully functional, but the public API
> may still change between 0.x releases. Pin your engine/plugin versions
> accordingly.
>
> **Data safety:** this is pre-1.0 software — despite the full test suite,
> undiscovered bugs may still corrupt or lose save data. **Keep backups of
> your saves**, especially before plugin upgrades and layout migrations.

> **The idea:** this library is a *butler*, not a homeowner. The location,
> content and structure of your saves belong to you — it only provides four
> services: read/write, organization, scheduling and migration. Everything is
> safe on the default path (write-through); performance and storage shape are
> your explicit choices.

## Features

- **Write-through by default** — `set` lands on disk the moment the value
  changes; a crash loses at most the last confirmed change. Same-value writes
  do zero IO.
- **Batch mode as a primitive** — `batch(True)` queues dirty keys, `flush()`
  lands them (measured: 4000 keys in ~116 ms in aggregate layout, with a
  regression guard against O(N²)).
- **Slots** — isolated save spaces (`get_slot` / `use_slot` / `list_slots`);
  the editor domain and the Player domain are separated by design.
- **File-is-value storage** — every key is a plain file you can open, read and
  hand-edit. JSON types plus top-level `bytes`. No envelopes, no opaque blobs.
- **Two storage shapes, freely switchable** — scattered (one file per key) or
  aggregate (type-sharded `.pack` files with a rebuildable index). Compression
  is an orthogonal per-slot state. `migrate.compress/plain/aggregate/scatter`
  are interruption-safe and converge on re-run; `migrate.diagnose()` reports
  silent anomalies.
- **Savegame Browser** — an editor panel (Window menu) for browsing and editing
  saves through the same public API, with staged edits and an explicit save
  confirmation.
- **MCP operations** — eight operations (`mervingamestudio.savegame.*`) so AI
  agents can inspect and drive saves without probe scripts.
- **Bundled scheduler component** — mount `MgsSavegameScheduler` to activate batch
  mode with periodic/threshold flushing; absent it, write-through still holds.

## Requirements

- Infernux engine 0.4.x (`>=0.4,<0.5`)
- On engines older than 0.4.1 the bundled scheduler component does not enter
  Player builds; write-through semantics are unaffected.

## Install

**From package (recommended):** import the released `.inxpkg` via the editor's
package import.

**From source:** copy the contents of [`package/`](package/) into
`Packages/mervingamestudio/savegame/` in your project and restart the editor.

## Quick start

```python
import infernux as inx

sg = getattr(inx, "savegame", None)
if sg is not None:
    sg.set("high_score", 700)          # -> written: on disk immediately
    sg.get_int("high_score")           # strict read: missing raises KeyError
    sg.get_int("high_score", 0)        # fallback read: missing/corrupt -> 0

    slot = sg.get_slot("save_a")       # independent slot object
    slot.set("inventory.sword", True)  # keys allow dots; case-insensitive
```

The full manual — [package/plugin_pages/usage.md](package/plugin_pages/usage.md)
([中文版](package/plugin_pages/usage.zh-CN.md)) — covers contracts, batch
mode, migration, the editor panel, MCP operations and gotchas. It also
ships as the plugin's in-editor usage page and follows the editor
language.

## Design notes

- **Files are the value.** `score.json` opens to `700` — no envelope, no
  side-car required to understand a save. Compression re-encodes the whole
  slot at once instead of mixing per-key formats.
- **Write-through is the correctness default; batching is an opt-in
  primitive.** The batch loss window (everything since the last flush) is a
  documented, chosen trade — never a silent surprise.
- **Mirror import.** `import_slot` restores an exact snapshot: keys outside
  the snapshot are deleted. A partial rollback is no rollback. Merge-style
  injection is `slot.update(mapping)` — a different tool for a different job.
- **The index is regenerable, data is authoritative.** A lost or stale
  aggregate index is rebuilt by scanning shards; saves never depend on their
  index to survive.
- **Zero dependencies, deterministic packages.** The library is stdlib-only.
  Package asset GUIDs derive from `uuid5(reference + path)`, so anyone
  building from the same source gets the same package identity.

## Testing

```sh
python -m unittest discover -s package/tests
```

150 tests, engine-independent (they exercise the pure library core).

## License

MIT — see [LICENSE](LICENSE). Release history: [CHANGELOG.md](CHANGELOG.md).
The plugin's in-engine pages (`usage`, `changelog`, localized) live under
[package/plugin_pages/](package/plugin_pages/).
