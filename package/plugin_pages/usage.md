# Usage

A write-through, slot-based key-value savegame library for the Infernux
engine. Zero third-party dependencies; requires the engine 0.4.x line.

> **The butler, not the homeowner.** The location, content and structure of
> your saves are yours — the library only serves reads/writes, organization,
> scheduling and migration. The default path is always safe (write-through);
> performance and storage shape are your explicit choices.

This guide is a ladder: each level adds one capability and tells you the
moment you actually need it. **Level 1 alone is a complete save system for a
small game** — everything above it is opt-in.

## Level 1 — Save a value, read it back

You need this from minute one: persist progress between sessions.

```python
import infernux as inx

sg = getattr(inx, "savegame", None)
if sg is not None:
    sg.set("high_score", 700)          # -> written: on disk immediately
    sg.get_int("high_score")           # strict read: missing raises KeyError
    sg.get_int("high_score", 0)        # fallback read: missing/corrupt -> 0
```

That is already the whole contract for a small game. Everything you save is
one plain file per key — `score.json` opens to `700`, no envelope, no
side-car. You can inspect and hand-edit saves with any tool.

A few rules that keep the file tree clean:

- Keys allow `a-z 0-9 _ . -` (dots fine: `settings.level`) and never start
  with a dot. The key space is **case-insensitive** (normalized to
  lowercase) — a different casing is an update, not a new key.
- Values: JSON types plus top-level `bytes` (`portrait.bin`). Edge cases
  (tuples, `NaN`, nested bytes) are listed in the Reference appendix.

Slightly more of Level 1, still basics:

```python
sg.get_str("player_name", "Newbie")    # typed fallback reads
sg.get_dict("settings", {})
sg.get_list("quests", [])
sg.get_bytes("portrait", b"")
sg.has("gold")                         # True
sg.delete("gold")                      # -> removed; again -> missing
sg.pop("gold", None)                   # read and delete
sg.update({"hp": 30, "mp": 10})        # bulk set (per-key write-through)
```

`sg` also behaves like a Python mapping: `in`, `len`, iteration,
`sg["gold"] = 120`. **A small game can ship on this alone.**

## Level 2 — Multiple save positions (slots)

*You need this when the game grows a save-slot UI — "Continue from slot 2".*

A slot is one isolated save space. The game starts on `"default"`; a slot
switch flushes the old slot first. Editor and Player domains are separate
pools (`.editor` suffix), so testing in the editor never touches player
data.

```python
sg.use_slot("save_1")                  # switch: old slot flushed first
sg.set("chapter", 3)

sg.list_slots()                        # ["default", "save_1"]
sg.current_slot()                      # "save_1"

slot = sg.get_slot("save_2")           # independent handle — does NOT
slot.set("chapter", 1)                 # move the game's active slot

sg.delete_slot("save_2")               # idempotent: removed / missing
```

`use_slot` is "the player chose slot N"; `get_slot` is "touch another slot
without disturbing the active one".

## Level 3 — Backup, restore and transfer

*You need this for cloud sync, modding support, or QA fixtures.*

```python
snapshot = sg.export_slot("save_1")    # the whole slot as a plain dict,
                                       # always unencoded
sg.import_slot("save_1", snapshot)     # MIRROR restore
```

**`import_slot` is a mirror, not a merge**: the slot ends up exactly equal
to the snapshot — keys not in the snapshot are **deleted**. A partial
rollback is no rollback. To *merge* data into an existing slot, use
`slot.update(mapping)` — a different tool for a different job.

Import is all-or-nothing: a precheck (key normalization, encodability)
rejects the whole call on any invalid key with zero changes; an interrupted
import simply re-runs and converges.

## Level 4 — High-frequency writes (batch mode)

*You need this when something saves every frame — positions, counters,
telemetry.*

The default is write-through: `set` lands on disk the moment the value
**changes** (same-value writes do zero IO). Batch mode is the opt-in
primitive:

```python
sg.batch(True)                         # write-through off, dirty keys queue
sg.set("pos_x", 12.5)                  # -> queued (not on disk yet)
sg.flush()                             # land the active slot's dirty keys
sg.flush_all()                         # ... or every slot's
sg.batch(False)                        # flush tail + restore write-through
sg.dirty_count()                       # queued changes right now
```

The trade is explicit: **the loss window is everything since the last
flush**. Flush after critical progress. In aggregate layout a batch flush
is one write per shard — measured 4000 keys in ~116 ms with a permanent
regression guard; `update(big_dict)` and `import_slot` ride the same path
in batch mode. `delete` racing an in-flight flush is safe (a delete is
never resurrected).

Or mount the bundled **MgsSavegameScheduler** component: mounting activates
batch mode, destroying it flushes and restores write-through. Inspector
fields: `interval` (seconds, 0 = off), `threshold_keys` (0 = off). It is a
pure optimization — without it, write-through still keeps every confirmed
change. Its worker never lets exceptions escape: if it dies, `status()`
reports the `dead_reason` and both remedies (restart, or `batch(False)`)
flush pending data first.

## Level 5 — Storage shape (compression, aggregate, migration)

*You need this when saves grow — many small keys, tidier directories,
portability. Not a performance switch.*

Two independent axes, four directions, switchable at any time:

| Axis | Calls | What it changes |
|---|---|---|
| Form | `migrate.compress()` / `migrate.plain()` | whole-slot zlib on/off (`.z` files) |
| Layout | `migrate.aggregate()` / `migrate.scatter()` | scattered ↔ type-sharded packs |

Aggregate layout packs values into type-sharded text packs (`int-0.pack`,
`str-0.pack` … JSONL, still human readable), with `bytes` always staying
scattered. Its index (`<slot>/.savegame/index.json`) is **regenerable** —
lost or stale, it is rebuilt by scanning shards; data never depends on its
index. Shards cap at 1 MB and overflow into new shards.

Migration properties, all four directions: per-key atomic (write-new →
verify → delete-old); interruption at any point leaves the old state fully
readable and re-running converges (`skipped` for already-converted keys);
failed keys neither block nor roll back — they stay readable in their old
form and heal on re-run or on the key's next `set`. Optional small ports:
`slot="save_a"` limits one slot, `defaults=False` leaves birth defaults
untouched. Detail reports per-slot `{converted, skipped, failed}`.

Unmigrated saves keep reading and writing normally — layouts can mix and
adapt automatically; migration is the thorough unifier.

**`sg.migrate.diagnose()`** is a read-only scan reporting four classes of
silent anomalies — orphan shards, dangling index entries, layout residue,
corrupt shards — each with a suggested remedy. Healthy slots return an
empty list.

## Toolbox

Pages you open when the need arises — not ladder steps:

- **Where saves live** — root resolution: `MGS_SAVES_ROOT` env override >
  `%LOCALAPPDATA%/Infernux/Saves/<game_name>` (Linux/macOS
  `~/AppData/Local`; Android: the engine's persistent-data directory).
  `<game_name>` follows the Build Settings product name — the same
  authority that names the built exe, in both editor and Player; renaming
  the packaged exe does not move saves.

  ```text
  Saves/<game>/                     .editor suffix = editor pool
    .savegame/defaults.json          game-level birth defaults
    <slot>/
      .savegame/state.json           slot facts {"compress", "aggregate"}
      score.json / portrait.bin      plain values (or .z compressed variants)
      int-0.pack ...                 aggregate shards (when aggregated)
      .savegame/index.json           regenerable routing index
  ```

- **Savegame Browser** (editor panel, Window menu) — browse and edit the
  editor save pool visually: keys table left, editor + staged list right;
  Discard/Save always visible in the footer; row context menu (copy
  key/value, stage delete); per-key unstage; search, type filter, sort;
  virtualized beyond 200 keys. Edits are staged and only reach disk after
  Save + confirmation — a running Play session reads them at once. bytes
  keys are read-only (files are the value). Every operation goes through
  the same public API as game code.

- **AI agents: MCP operations** — eight operations on the engine MCP
  gateway so agents can drive savegame debugging without probe scripts:
  `mervingamestudio.savegame.slots / keys / get / stats / diagnose`
  (queries) and `set / delete / flush` (commands, gated by the engine's
  `runtime.write` capability). `slot` defaults to `"default"`; bytes travel
  as `{"type":"bytes","hex":...}` (≤4MB).

## Appendix

### API contracts

| Line | Rule |
|---|---|
| Reads | `get(key)` strict: missing → `KeyError`, corrupt → `SavegameError`; `get(key, default)` falls back; typed strict reads add `TypeError` on type mismatch |
| Mutations & IO | return `SaveResult` (`ok/status/detail/data`; truthy = ok) |
| Exceptions | mean programming errors (invalid key/slot name → `ValueError`) |

`SaveResult.status`: `set` → `written` / `queued` / `rejected` / `failed`;
`flush` → `done` / `partial`; deletes → `removed` / `missing`.

`refresh(flush=True)` flushes then re-reads from disk **keeping the active
slot cursor** (a vanished slot falls back to `default`, noted in `detail`).
`refresh(flush=False)` discards the cache **and queued writes** — use it
only when you truly mean to abandon in-memory state.

### Value domain edge cases

Tuples normalize to lists; `bytearray` to `bytes`; `NaN`/`Infinity` are
rejected; bytes nested inside dict/list are not supported — split them
into top-level keys.

### Gotchas

- **`awake()` runs when the editor loads a scene**, not only in Play. Save
  initialization/migration there must be idempotent or guarded by a
  first-run flag in the save itself.
- **The editor-domain API singleton survives across Play sessions** — value
  caches too. After Stop, a first `set` of an unchanged value skips disk IO
  (nothing changed = already on disk; harmless, just don't expect a write).
- **Batch loss window** = since the last flush. Critical progress deserves
  a `sg.flush()`.
- **Restart the editor after installing/updating the plugin** before
  entering Play — a domain reload wipes the `inx.*` injections.

### Reference

**KV** (active slot): `set / get / get_int / get_float / get_str /
get_bool / get_list / get_dict / get_bytes / has / keys / delete / pop /
setdefault / update`, dict syntax, Mapping protocol.

**Slots**: `get_slot / use_slot / current_slot / list_slots / delete_slot /
delete_all_slots / export_slot / import_slot / refresh`.

**Primitives**: `batch / flush / flush_all / dirty_count / create_worker /
stats`.

**Migration**: `migrate.compress / plain / aggregate / scatter / diagnose`.

**Test suite** (150 tests, engine-independent):

```sh
python -m unittest discover -s Packages/mervingamestudio/savegame/tests
```
