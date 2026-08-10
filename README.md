# plugins/repo_internal/maya_launcher/

Launches Maya with the linked executable (via Software Linker), auto-sets
the project to the repo root, and assembles Maya env vars merged from
whatever independent Maya tool plugins have contributed to a shared
bridge. This plugin is a **pure bridge reader** — it owns launching Maya
with an assembled env, not the list of what goes into that env, so a new
tool can start contributing paths with zero code change here.

This is a **reverted architecture**, changed 2026-07-19. Between
2026-07-14 and 2026-07-19 this plugin briefly consolidated 8 separate
add-ons (itself plus 7 pure env-contributing add-ons that did nothing but
write into the same shared bridge) into one plugin with the 7 tools
nested inside its own folder (`tools.py`'s `TOOL_FOLDERS`/`build_contributions`)
— see git history around those dates if you need that version. It was
un-consolidated back to independent plugins because grouping unrelated
vendored tools under one plugin folder made "stay inside the folder the
task names" (see root `CLAUDE.md`) impossible to honor for a
single-tool change, and because a plugin catalog that lists "Maya
Launcher" as one opaque entry hides which individual tools a repo actually
depends on. The current shape is functionally identical to the
**original, pre-2026-07-14 add-on architecture** — see
`add-on/MayaLauncher/plugin.py` in git history for the version this was
restored from.

## Files

- `manifest.json` — plugin id `maya_launcher`, entry point `plugin.py`.
- `plugin.py` — `register(api)`: registers the `.ma`/`.mb` file opener and
  the `Maya Launcher` settings tab. Also has the launch/merge logic:
  `_maya_programs_for_repo`, `_repo_root_path`, `_set_project_and_open_command`,
  `_build_maya_env`, `_read_bridge`, `_prepare_env_and_plugins` (see "Env
  merge" and "Auto set-project" below).
- `settings_page.py` — `MayaLauncherSettingsPage`: the Settings > Maya
  Launcher tab — just the Software Linker link-status readout (✅/⚠️ per
  required Maya `Program`). No longer owns any per-tool enable/disable UI
  (see "Per-repo tool gating" below).

**The 6 nested tool payload folders that used to live here
(`AdvancedSkeleton/`, `MayaNgskin/`, `MayaToolkit/`, `mGear/`,
`DreamwallPicker/`, `StudioLibrary/`)
all moved to their own top-level `plugins/core/<Name>/` folders on
2026-07-19.** Each is
its own plugin now (own `manifest.json` + `plugin.py`), contributing to
the shared bridge described below instead of being read directly off disk
by this plugin. See each one's own README for its specific vendored
payload shape.

**`UkorePublisher` went further still, the same day**: extracted out of
`MayaToolkit/maya-scripts/` into its own plugin, then immediately split
again into three type-specific plugins — `ModelPublisher`, `RigPublisher`,
`AnimationPublisher` — each with its own dedicated UI instead of one
shared "pick a Type, then a Ticket" window. All three were built on
`plugins/repo_internal/PublishApi/`, a new non-UI Maya-side library
(itself one of these bridge-contributing tool plugins) that resolves a
publish destination from the active repo's Project Editor pipeline
metadata and creates versioned publish folders — the single source of
truth those three shared instead of each
carrying its own copy of that logic. **2026-08-05**: since the three
plugins turned out to be near-identical (same `plugin.py`/`interface.py`/
`ui.ui`, only the export logic differed), they were merged back into one
`plugins/repo_internal/MayaPublisher/`, with a per-repo "Publish Mode"
setting choosing which of Rig/Model/Animation that repo publishes as — see
`plugins/repo_internal/PublishApi/README.md`.

## The `maya_launcher_env_bridge` shared bridge

`MAYA_ENV_BRIDGE_PLUGIN_ID = "maya_launcher_env_bridge"` — a convention-only
string both this plugin and every contributing tool plugin agree on as a
`PluginConfigStore` id (`api.plugin_config_store(MAYA_ENV_BRIDGE_PLUGIN_ID,
shared=True)` → `data/plugins/core/maya_launcher_env_bridge.json` — `shared=True`
always resolves under the literal `"core"` subdir regardless of which root
the calling plugin itself lives under, see `interface/plugin_api.py`'s
`plugin_config_store`), no
import between them at all. Each tool plugin's own `register(api)` writes
its entry unconditionally, every app start:

```json
{
  "contributions": {
    "<tool_id>": {"<VAR_NAME>": {"*": ["path", ...], "<maya_version>": ["path", ...]}}
  },
  "labels": {
    "<tool_id>": "Human-readable label for the Settings checkbox"
  }
}
```

`"*"` (`ANY_VERSION`) applies no matter which Maya version launches; an
explicit version key (matching `Program.version`, e.g. `"2024"`) applies
only when that exact Maya version launches — this is how MayaNgSkin keys
its per-version `MAYA_PLUG_IN_PATH` entries. This plugin's `_read_bridge(api)`
does a **fresh** read (a new `PluginConfigStore` is constructed and loaded
from disk on every call) — safe regardless of plugin load order, since
`open_maya_file`/the Settings tab are only ever triggered by the user well
after every plugin has finished registering (see `core/extensibility/loader.py`).

| Tool plugin | Contributes |
|---|---|
| `plugins/core/AdvancedSkeleton` | `PYTHONPATH` + `ADVANCEDSKELETON_ROOT` (single-directory value, not a search-path list — see that plugin's README) |
| `cache/plugins/MayaNgSkin` | `PYTHONPATH` + versioned `MAYA_PLUG_IN_PATH` |
| `plugins/repo_internal/MayaToolkit` | `PYTHONPATH` + flat `MAYA_PLUG_IN_PATH` |
| `plugins/core/mGear` | `MAYA_MODULE_PATH` + `MGEAR_SHIFTER_COMPONENT_PATH` |
| `plugins/core/DreamwallPicker` | `PYTHONPATH` |
| `plugins/core/StudioLibrary` | `PYTHONPATH` |
| `plugins/repo_internal/PublishApi` | `PYTHONPATH` (its own `maya-scripts/` **and** `api.app_root`, so `import core.store`/`core.paths` resolves inside Maya's Python — that's how it talks to UkoreHub's own Project/Repo model) |
| `plugins/repo_internal/MayaPublisher` | `PYTHONPATH` |
| `plugins/repo_internal/UkorePlayblast` | `PYTHONPATH` |

`mGear.mod` is itself version-aware (`+MAYAVERSION:2018 ...` blocks), so
`MAYA_MODULE_PATH` only needs mGear's flat `maya-modules` folder — Maya
resolves the right platform/version subfolder from the `.mod` file itself.
`ngSkinTools2.mll`, by contrast, is a compiled plug-in shipped **one build
per Maya version**, hence the versioned keying.

## Per-repo tool gating — owned by Repository Setting > Enable Plugin

Whether a tool's env contribution actually applies to a given repo's Maya
launch is gated per-repo by `repo.required_plugin_ids` — the same
`Repo.required_plugin_ids` list `interface/repo_settings/
requirements_and_plugins_page.py`'s "Enable Plugin" section already
manages for every plugin's sidebar visibility (see that page and
`interface/main_window.py`'s `_apply_plugin_visibility`). `plugin.py`'s
`_prepare_env_and_plugins` intersects `repo.required_plugin_ids` with the
bridge's `"contributions"` keys — this works with no extra mapping because
every contributing tool's manifest `id` is identical to its own bridge
`tool_id` (e.g. `cache/plugins/MayaNgSkin/manifest.json`'s
`"id": "maya_ngskin"` matches its `plugin.py`'s `TOOL_ID = "maya_ngskin"`).
Enable Plugin is **opt-in** (unchecked by default) — a repo that has never
had a tool checked there gets **zero** tool contributions on Maya launch,
unlike the old mechanism below.

**Before 2026-08-05** this was a separate opt-out toggle owned entirely by
this plugin: `RepoToolsStore` (`repo_tools_store.py`, deleted), backed by
`api.plugin_config_store("maya_launcher", shared=True)` →
`data/plugins/core/maya_launcher.json`, with its own
`MayaLauncherSettingsPage` checkbox list ("Enabled Tools for Active Repo").
Removed because every tool it gated is now its own independent
`plugins/repo_internal/<Name>/` plugin (since the 2026-07-19
un-consolidation below), each *already* individually toggleable per repo
via Enable Plugin — so the same decision ("does this repo use
MayaNgskin?") had two separate checkboxes in two different places that
could disagree with each other. Folding gating into Enable Plugin removes
that duplication; the tradeoff is the opt-in-by-default-off behavior noted
above, a deliberate change (previously: opt-out, "no entry = everything
on") — an existing repo that relied on the old all-enabled-by-default
behavior needs its tools re-checked once under Repository Setting > Enable
Plugin. `data/plugins/core/maya_launcher.json`'s old
`repo_disabled_tools`/`repo_enabled_tools` data was simply abandoned, never
migrated — nothing read that file anymore, so the local copy was deleted
2026-08-09 (its cloud blob is meant to follow).

**`plugins/repo_internal/PublishApi` is never gated by this at all** — it's
pure infrastructure (no artist-facing behavior or UI of its own, only
path-resolution/versioning functions `MayaPublisher` imports directly), so
there's no legitimate reason to ever disable it per-repo. `open_maya_file`
force-includes its contribution regardless of `repo.required_plugin_ids`
(`PUBLISH_API_TOOL_ID` in `plugin.py`) — unlike the pre-2026-08-05
`MayaLauncherSettingsPage`, Enable Plugin's generic plugin list does still
show a PublishApi checkbox (it doesn't special-case any plugin id), but
checking/unchecking it has no effect on the Maya env either way since the
force-include bypasses it.

## Env merge (`plugin.py::_build_maya_env`)

`open_maya_file` reads the bridge's full `"contributions"` dict, filters
it down to the active repo's `required_plugin_ids` (see "Per-repo tool
gating" above), then `_build_maya_env(base_env, contributions, maya_version)` merges every
remaining contribution — iterates `tool_id`s sorted for deterministic
ordering; for each, each `var_name`'s `"*"` paths then its
`maya_version`-specific paths (if any) are prepended in that order. **It
prepends, it never replaces**:
```python
env[var_name] = f"{new_entry}{os.pathsep}{existing}" if existing else new_entry
```
This matters because an artist's machine may already have its own Maya/
mGear install contributing to `PYTHONPATH` etc. — replacing would silently
break whatever that install already relies on. Returns a **new** dict
rather than mutating the input, so callers can safely pass
`os.environ.copy()`.

## Force-loading compiled/script plug-ins on launch

Being on `MAYA_PLUG_IN_PATH` only makes a plug-in *visible* in Maya's
Plug-in Manager — it doesn't load it. Before this, an artist had to tick
"Auto Load" by hand, per plug-in, per machine, every time. `open_maya_file`
now force-loads them instead: `_force_load_plugin_names(contributions,
maya_version)` scans every contributed `MAYA_PLUG_IN_PATH` folder for
`.py`/`.mll`/`.pyd`/`.so` files sitting **directly** in it (same shallow
scan Maya's own Plug-in Manager does — a file nested one level deeper,
like most of `MayaToolkit`'s `maya-plug-ins/` subfolders, still won't be
found), and `_force_load_plugins_command` turns that into
`catch(\`loadPlugin -quiet "name"\`);` MEL for each, prepended onto the
`-command` string **before** `setProject`/`file -open` — so a scene
referencing plug-in node types (an ngSkinTools skin layer, say) opens
without Maya flagging them as unknown nodes. Each load is wrapped in
`catch` so one plug-in failing (or already being loaded) can't take the
rest of the `-command` string down with it.

## The SoftwareLinker dependency (shared-config convention, not an import)

`plugin.py` doesn't import `plugins/core/software_linker/`'s code —
both agree on the literal string `"software_linker"` as a
`PluginConfigStore` id:
```python
SOFTWARE_LINKER_PLUGIN_ID = "software_linker"
linked = api.plugin_config_store(SOFTWARE_LINKER_PLUGIN_ID, shared=False)
maya_exe = linked.get(program.id)   # program.id is the Program catalog entry's id
```
`shared=False` because which local `maya.exe` path a specific artist's
machine has is inherently per-machine, not team data (contrast with
`Repo.required_plugin_ids` above, which *is* shared/studio-tracked). If SoftwareLinker's
plugin id or config key ever changes, this constant has to change in
lockstep — there's no compiler/test to catch drift, so grep both files if
you touch either. `_maya_programs_for_repo(api, repo)` is the shared lookup
used by both `open_maya_file` and `MayaLauncherSettingsPage`: resolves
`api.local_config.active_project_id` (Program is per-Project now, see
`core/models.py`'s `Project.programs`), walks `repo.required_program_ids`,
resolves each through `api.metadata.get_program(project_id, id)`
(catching `core.exceptions.NotFoundError` for stale ids), keeps ones whose
`name` contains `"maya"` case-insensitively. If a repo requires multiple
Maya versions, `open_maya_file` picks the **first one with a linked path**
— it does not disambiguate further.

## Auto set-project on launch

`open_maya_file` sets Maya's project on launch so the artist never has to
do it by hand. Per studio convention, every repo's `workspace.mel` always
lives at the repo's own root — `_repo_root_path` computes
`Path(api.local_config.workspace_root) / repo.local_path` (`Repo.local_path`
is stored relative to the workspace root, same join `core/paths.py`'s
`resolve_repo_path` produces elsewhere, done directly here since
`open_maya_file` only has `repo`, not the owning `Project`). This goes
through Maya's `setProject` MEL command via the `-command` flag, **not**
the `-proj` CLI flag — `-proj` proved unreliable in practice (Maya's own
last-session project preference surfaced an unrelated "Path does not
exist" dialog even against a repo whose `workspace.mel` demonstrably sits
at repo root), whereas `setProject` is Maya's directly-documented,
always-available command. The scene is opened via `file -open -force`
inside the same MEL string (not a positional CLI arg) so `setProject` is
guaranteed to run first: `[maya_exe, "-command", mel_command]`, nothing
else on the command line.

## Deferred reference loading for Ukore Reference Editor

`open_maya_file` opens with two extra flags —
`-loadReferenceDepth "none" -prompt false`
(`_set_project_and_open_command`'s `defer_reference_load` param) — whenever
`plugins/repo_internal/UkoreReferenceEditor`'s tool id (`UKORE_REFERENCE_EDITOR_TOOL_ID
= "ukore_reference_editor"`, convention-only match with that plugin's own
`TOOL_ID`) is in this repo's `enabled_tool_ids`. `-loadReferenceDepth "none"`
leaves every reference in the scene unloaded regardless of whether its file
resolves; `-prompt false` is the flag that actually stops Maya's own native
"could not find file" dialog from appearing — `-loadReferenceDepth` alone
does **not** suppress it (Maya still validates each reference's path
independently of whether it loads the content — confirmed empirically, see
`developer/bug-history/2026-08-03-reference-native-dialog-not-suppressed-by-loadreferencedepth.md`).
`UkoreReferenceEditor`'s `kAfterOpen` callback
(`plugins/repo_internal/MayaToolkit/maya-plug-ins/ukoreMaya.py`) is what actually
loads every reference back afterward — redirecting the broken ones first,
per its own internal/external/Connect-Input-Path rules — so this plugin
never needs to know anything about *how* references get fixed, only that
something downstream will load them. This is why the flag is gated on
`enabled_tool_ids` rather than always on: without UkoreReferenceEditor
enabled for a repo, there'd be nothing to load the references back, and
every scene from that repo would open with everything permanently unloaded.

## Failure mode: repo needs Maya but nothing is linked

`open_maya_file` deliberately does **not** silently fall back to the OS
file association when no linked `maya.exe` is found — it shows
`QMessageBox.warning(None, "Maya Launcher", ...)` telling the user to go to
Settings → Maya Launcher / Software Linker, and returns `True` (meaning
"handled", so the caller doesn't additionally fall back to
`open_with_default_app`). Silent fallback was explicitly rejected because
it would make missing env injection indistinguishable from a
working-but-unconfigured setup.

## Standalone launch for plugins/core/program_launcher/

Added 2026-08-03. `plugins/core/program_launcher/`'s card grid (one
square card per Program a repo requires) used to `subprocess.Popen` the
raw linked exe for every Program, Maya included — which skipped
`setProject`/env-merge/force-load-plugins entirely. `register(api)` now
also calls `api.register_program_launcher(ProgramLaunchSpec(match=...,
launch=launch_maya_standalone))` (`interface/program_launch_registry.py`)
— `match` is `"maya" in program.name.lower()`, same substring check
`_maya_programs_for_repo` already used; `launch_maya_standalone(repo)`
does everything `open_maya_file` does except the `file -open` (just
`setProject`, no scene). The resolve-linked-exe step (`_find_linked_maya`)
and the bridge-read/env-merge/force-load-plugin-names step
(`_prepare_env_and_plugins`) were extracted out of `open_maya_file` so
both entry points share them rather than duplicating the logic. This is a
convention-only registration, not a coupling — `program_launcher` never
imports this plugin; it just checks `api.program_launch_registry.
find_launcher(program)` before falling back to its own generic exe
launch, and finds this plugin's spec by Program name match at click time.

## Adding a new nested tool

1. Create a new `plugins/core/<Name>/` plugin folder (own `manifest.json`
   + `plugin.py`), following the shape any of the existing tool plugins
   above use — `register(api)` reads/updates the bridge's `"contributions"`
   and `"labels"` dicts and writes them back, nothing else.
2. That's it — this plugin never needs a code change: `_read_bridge` and
   `_prepare_env_and_plugins` iterate whatever the bridge currently knows
   about generically, and gating comes from Repository Setting > Enable
   Plugin (see "Per-repo tool gating" above), which lists every discovered
   plugin generically too.

## Extending this pattern to another DCC (Houdini, Nuke, Blender, ...)

A different, larger task than "add a nested tool" above — a new DCC needs
its own launcher plugin, not a contribution to this one. Copy the shape,
not the Maya specifics: own `plugins/core/<dcc>_launcher/` folder (own
`manifest.json`/`plugin.py`), its own `_xxx_programs_for_repo` lookup
(currently Maya-specific here by string-matching `"maya"` in
`program.name`, not extracted to a shared helper — do that extraction if a
second DCC launcher makes the duplication worth it),
`api.register_file_opener(id, [".ext1", ".ext2"], open_xxx_file)` (reuses
`FileOpenerRegistry` as-is), and its own `_build_xxx_env`
prepend-not-replace merge function if it has multiple nested tools, or none
at all if it's self-contained. Decide its own shared-bridge `PluginConfigStore`
id convention up front (same `{tool_id: {var_name: {...}}}` shape this
plugin uses) so a new DCC's contributing tool plugins have something to
write into. Note: `add-on/BlenderLauncher/` and `add-on/UnrealLauncher/`
were removed during the 2026-07-14 consolidation — neither had a real
`manifest.json`/`plugin.py`/`register(api)`, so there's no existing
Blender/Unreal launcher logic to build from; a Blender or Unreal launcher
plugin would be a from-scratch build following this pattern, not a
migration.

## Testing

`_build_maya_env`, `_maya_programs_for_repo` are pure/Qt-free and worth
covering if you touch them — but this plugin's `.py` files aren't
reachable by normal pytest `import` from outside their own package (the
loader always imports `plugin.py` standalone via
`importlib.util.spec_from_file_location`, same as any other plugin — see
`plugins/README.md`'s Testing section). Verify with a throwaway scratchpad
script that imports `plugins.repo_internal.maya_launcher.link_resolution`
directly (a real importable module, just not part of a pytest `tests/`
package) and asserts on its pure-function outputs, or loads `plugin.py`
the same way the real loader does for anything that needs `register(api)`'s
closures specifically.
# maya_launcher
