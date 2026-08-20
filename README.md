นี่คือเนื้อหาไฟล์ `README.md` ที่ได้รับการปรับปรุงและอัปเดตให้ครอบคลุมการทำงานล่าสุด รวมถึงโครงสร้าง API, Launch Hooks, และฟังก์ชันต่าง ๆ ตามไฟล์ซอร์สโค้ดปัจจุบันครับ

```markdown
# plugins/repo_internal/maya_launcher/

Launches Maya with the linked executable (via Software Linker), auto-sets the project to the repo root, and assembles Maya env vars merged from independent Maya tool plugins via a shared bridge. This plugin is a **pure bridge reader** — it owns launching Maya with an assembled env, not the list of what goes into that env. New tools can contribute paths, CLI flags, or MEL commands with zero code changes in this plugin.

---

## 📁 Files

- `manifest.json` — Plugin metadata (`id: maya_launcher`, entry point `plugin.py`).
- `plugin.py` — Core entry point. Registers file openers (`.ma`/`.mb`), standalone launcher, and Settings tab. Handles bridge reading, env merging, plugin force-loading, and MEL command assembly.
- `link_resolution.py` — Helper functions for resolving multi-version program keys and pinned repo versions (`linked_key`, `pinned_version`).
- `settings_page.py` — `MayaLauncherSettingsPage`: Settings UI tab displaying Software Linker status (✅/⚠️) for required Maya programs in the active repo.

---

## 🔌 API Reference & Internal Mechanics

### 1. Shared Bridge (`maya_launcher_env_bridge`)

The communication between tool plugins and `maya_launcher` relies on a convention-based `PluginConfigStore` ID:
```python
MAYA_ENV_BRIDGE_PLUGIN_ID = "maya_launcher_env_bridge"

```

Tool plugins write to this bridge during their registration using `api.project_plugin_config_store(MAYA_ENV_BRIDGE_PLUGIN_ID)`.

#### Bridge Schema Structure

```json
{
  "contributions": {
    "<tool_id>": {
      "<VAR_NAME>": {
        "*": ["/path/to/common/dir"],
        "<maya_version>": ["/path/to/version_specific/dir"]
      }
    }
  },
  "labels": {
    "<tool_id>": "Human-readable Tool Name"
  },
  "launch_hooks": {
    "<tool_id>": {
      "order": 100,
      "cli_flags": ["-flag1"],
      "pre_open_mel": "source \"my_pre_script.mel\";",
      "post_open_mel": "source \"my_post_script.mel\";",
      "diagnostic_msg": "Initializing My Tool..."
    }
  }
}

```

* **`"*"` (`ANY_VERSION`)**: Applies to any Maya launch.
* **`"<maya_version>"`** (e.g. `"2024"`): Applies only when launching that specific Maya version.
* **`launch_hooks`**: Allows tools to inject custom command-line flags and MEL code before or after the scene/project is loaded, sorted deterministically by `order`.

---

### 2. Core Functions Reference (`plugin.py`)

#### `_read_bridge(api)`

* **Returns:** `tuple[dict, dict, dict]` -> `(contributions, labels, launch_hooks)`
* **Description:** Performs a fresh read of the shared bridge store.

#### `_prepare_env_and_plugins(api, repo, maya_version)`

* **Returns:** `tuple[dict, list[str], list[dict]]` -> `(env, plugin_names, active_hooks)`
* **Description:** Filters tool contributions and hooks based on `repo.required_plugin_ids` (with `publish_api` forced as infrastructure), **AND** on `api.plugin_catalog` — a tool id must also belong to a plugin actually discovered via a real `manifest.json` this launch. Merges environment variables and scans `MAYA_PLUG_IN_PATH` directories for plugins (`.py`, `.mll`, `.pyd`, `.so`) to force-load.

  **Why the `plugin_catalog` gate exists:** the bridge (`maya_launcher_env_bridge`) is purely additive — a tool writes its `contributions`/`labels`/`launch_hooks` entry once in its own `register(api)`, and nothing ever removes it. Likewise nothing ever prunes a stale id out of `repo.required_plugin_ids` if its checkbox disappears from Requirements & Plugins (which can only show/uncheck a tool that `discover_plugins()` still finds — once a `cache/plugins/<Name>/` folder is deleted, that checkbox is gone too, so a lingering id becomes literally un-uncheckable through the UI). Before this gate, either kind of leftover — an orphaned bridge entry or an orphaned `required_plugin_ids` id — was enough on its own to reactivate a retired tool's `launch_hooks` (including any unguarded `python(...)` import) on every Maya launch, indefinitely. Requiring the id to also appear in `api.plugin_catalog` this session means a tool's hooks/env only ever apply while its plugin is actually installed and loaded — not off historical bridge/required_plugin_ids data alone. (Incident: a renamed `dw_publish_picker` → `dreamwall_picker` left exactly this kind of orphan on `RigTeam`, breaking every Maya launch from Explorer on that repo with an unguarded `ModuleNotFoundError: DwPublishPicker`.)

#### `_build_maya_env(base_env, contributions, maya_version)`

* **Returns:** `dict`
* **Description:** Prepends contribution paths to the environment variables (`PYTHONPATH`, `MAYA_PLUG_IN_PATH`, `MAYA_MODULE_PATH`, etc.) without overwriting existing system variables.

#### `_build_launch_commands(repo_root, scene_path, active_hooks)`

* **Returns:** `tuple[list[str], str]` -> `(cli_flags, mel_command)`
* **Description:** Assembles CLI arguments and MEL command strings from active launch hooks ordered by priority:
1. Diagnostic `print()` statements.
2. Pre-open MEL code (`pre_open_mel`).
3. `setProject "<repo_root>"`.
4. `file -open -force "<scene_path>"` (if opening a scene file).
5. Post-open MEL code (`post_open_mel`).



#### `_force_load_plugins_command(plugin_names)`

* **Returns:** `str`
* **Description:** Wraps each plugin name in `catch(\`loadPlugin -quiet "pluginName"`);` to prevent scene loading errors without crashing the launch sequence if a plugin fails.

---

### 3. Link Resolution API (`link_resolution.py`)

#### `linked_key(program, version="") -> str`

Resolves the storage key for `SoftwareLinker`. Returns `program.id` if single-version, or `"<id>:<version>"` if the program supports multiple versions.

#### `pinned_version(repo, program) -> str`

Determines which version to launch for a given repo. Prefers `repo.program_version_pins[program.id]` if valid; defaults to the first available version in `program.versions`.

---

## ⚙️ Per-Repo Tool Gating

Tool execution is gated per repository via `repo.required_plugin_ids` (managed under **Repository Settings > Enable Plugin** in the main application).

1. When Maya launches, `maya_launcher` intersects `repo.required_plugin_ids` with the tool IDs present in `contributions`.
2. **Exception (`PublishApi`):** `PUBLISH_API_TOOL_ID = "publish_api"` is pure infrastructure and is always force-included regardless of repository setting toggles.

---

## 🛠️ Adding a New Tool Contribution

To add a new Maya tool/plugin without touching `maya_launcher`:

1. Create a plugin directory under `plugins/core/<ToolName>/` or `plugins/repo_internal/<ToolName>/`.
2. In your plugin's `register(api)` function, update the shared bridge:

```python
MAYA_ENV_BRIDGE_PLUGIN_ID = "maya_launcher_env_bridge"

def register(api):
    bridge = api.project_plugin_config_store(MAYA_ENV_BRIDGE_PLUGIN_ID)
    
    # 1. Add path contributions
    contributions = bridge.get("contributions", {})
    contributions["my_tool_id"] = {
        "PYTHONPATH": {
            "*": [str(Path(__file__).parent / "scripts")]
        },
        "MAYA_PLUG_IN_PATH": {
            "2024": [str(Path(__file__).parent / "plug-ins" / "2024")]
        }
    }
    
    # 2. Add labels
    labels = bridge.get("labels", {})
    labels["my_tool_id"] = "My Custom Maya Tool"
    
    # 3. (Optional) Add launch hooks
    launch_hooks = bridge.get("launch_hooks", {})
    launch_hooks["my_tool_id"] = {
        "order": 50,
        "pre_open_mel": "python(\"import my_tool; my_tool.init()\");",
        "diagnostic_msg": "My Tool loaded successfully."
    }
    
    # Save back to bridge
    bridge.set("contributions", contributions)
    bridge.set("labels", labels)
    bridge.set("launch_hooks", launch_hooks)

```

```

```