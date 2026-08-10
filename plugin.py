from __future__ import annotations

import os
import subprocess
from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from core.exceptions import NotFoundError
from interface.program_launch_registry import ProgramLaunchSpec
from interface.settings_tab_registry import CATEGORY_REPO, SettingsTabSpec
from plugins.repo_internal.maya_launcher.link_resolution import linked_key, pinned_version
from plugins.repo_internal.maya_launcher.settings_page import MayaLauncherSettingsPage

PLUGIN_ID = "maya_launcher"
# Convention-only string match with plugins/core/software_linker/plugin.py
# — both resolve to the same cache/plugin_local_config/software_linker.json
# via PluginConfigStore, no coupling API needed.
SOFTWARE_LINKER_PLUGIN_ID = "software_linker"
# Convention-only string match with every Maya env-contributing plugin
# (plugins/core/AdvancedSkeleton, cache/plugins/MayaNgSkin, .../MayaToolkit,
# .../mGear, .../DreamwallPicker, .../StudioLibrary,
# .../PublishApi, .../MayaPublisher) — each writes its own contributions[tool_id] entry
# (plus labels[tool_id]) into this shared, studio-tracked PluginConfigStore
# at register() time. This plugin is a pure bridge reader: it owns
# launching Maya with an assembled env, not the list of what goes into
# that env, so a new tool can start contributing paths with zero code
# change here — reverted 2026-07-19 to this pre-consolidation shape (see
# git history around 2026-07-14 for the version that briefly inlined every
# tool's env-building logic into this plugin instead of a shared bridge).
MAYA_ENV_BRIDGE_PLUGIN_ID = "maya_launcher_env_bridge"
ANY_VERSION = "*"
# PublishApi is pure infrastructure — no artist-facing behavior or UI of
# its own, just path-resolution/versioning other tools (MayaPublisher)
# import directly. Its env
# contribution is force-included below regardless of whether it's checked
# under Repository Setting > Enable Plugin: there's no legitimate reason a
# repo would ever want it disabled, and doing so only breaks whatever else
# is enabled and imports it — `import PublishApi` failing inside Maya for a
# tool that's supposedly turned on.
PUBLISH_API_TOOL_ID = "publish_api"
# Convention-only string match with plugins/repo_internal/UkoreReferenceEditor/plugin.py's
# own TOOL_ID — used to decide whether to open with -loadReferenceDepth
# "none" below, not to gate the bridge contribution itself (that's already
# handled generically via enabled_tool_ids, same as every other tool).
UKORE_REFERENCE_EDITOR_TOOL_ID = "ukore_reference_editor"

MAYA_FILE_EXTENSIONS = [".ma", ".mb"]


def _maya_programs_for_repo(api, repo):
    """Every Program the repo requires whose name contains "maya" — resolved
    via repo.required_program_ids against the *active* Project's own
    Program Database (Program is per-Project now, core/models.py's
    Project.programs), since this is only ever called for the currently
    active repo (via FileOpenerRegistry/ProgramLaunchRegistry callbacks,
    which don't carry a project id of their own). Same lookup used by both
    the settings page's link-status readout and the file opener below."""
    project_id = api.local_config.active_project_id
    if project_id is None:
        return []
    programs = []
    for program_id in repo.required_program_ids:
        try:
            program = api.metadata.get_program(project_id, program_id)
        except NotFoundError:
            continue
        if "maya" in program.name.lower():
            programs.append(program)
    return programs


def _repo_root_path(api, repo) -> Path:
    """The repo's cloned root folder on this machine — where its
    workspace.mel always lives, per studio convention, so this is what gets
    passed to Maya's `setProject`. `repo.local_path` is stored relative to
    the workspace root (see core/store.py's `add_repo`), the same join every
    other page does (e.g. plugins/core/project_editor/project_graph_view.py
    via core/paths.py's resolve_repo_path)."""
    return Path(api.local_config.workspace_root) / repo.local_path


def _mel_string(path: Path) -> str:
    """A filesystem path as a MEL string literal — forward slashes (MEL's
    own convention, sidesteps backslash-escaping ambiguity) with internal
    double-quotes escaped defensively."""
    return str(path).replace("\\", "/").replace('"', '\\"')


def _set_project_and_open_command(repo_root: Path, scene_path: Path, defer_reference_load: bool = False) -> str:
    """Pure, testable: the MEL passed to Maya's `-command` flag. Uses the
    real `setProject` MEL command rather than the `-proj` CLI flag — `-proj`'s
    interactive-Maya support turned out unreliable in practice (a repo whose
    workspace.mel demonstrably sits at repo root still hit Maya's own "Path
    does not exist" project-restore dialog), whereas `setProject` is Maya's
    own always-available, directly-documented command for this. Opening the
    scene via `file -open -force` in the same command (instead of passing it
    as a positional CLI arg) guarantees setProject runs first.

    `defer_reference_load` adds `-loadReferenceDepth "none" -prompt false`.
    `-loadReferenceDepth "none"` alone leaves every reference unloaded but
    does **not** stop Maya's own native "could not find file" dialog by
    itself — Maya still validates each reference's path regardless of
    whether it's actually told to load the content. `-prompt false`, Maya's
    own documented flag for suppressing interactive `file`-command dialogs,
    is what actually stops it — confirmed working against a real broken
    reference 2026-08-03, see
    `developer/bug-history/2026-08-03-reference-native-dialog-not-suppressed-by-loadreferencedepth.md`
    for why `-loadReferenceDepth` alone wasn't enough. Only set when
    plugins/repo_internal/UkoreReferenceEditor is enabled for the launching repo
    (see open_maya_file below) — its own kAfterOpen callback
    (plugins/repo_internal/MayaToolkit/maya-plug-ins/ukoreMaya.py) is what actually
    loads every reference back afterward, redirecting the broken ones first;
    without that tool enabled, forcing every reference unloaded would leave
    them stuck that way with nothing to load them back."""
    open_flags = ' -loadReferenceDepth "none" -prompt false' if defer_reference_load else ""
    # Printed inside Maya's own Script Editor (not UkoreHub's console, which
    # may not even have a visible window) so it's always possible to confirm
    # which path this launch actually took, regardless of how UkoreHub itself
    # was started.
    diagnostic = (
        'print("[UkoreReferenceEditor] Maya launched with -loadReferenceDepth \\"none\\" -prompt false\\n");'
        if defer_reference_load
        else 'print("[UkoreReferenceEditor] Maya launched WITHOUT -loadReferenceDepth (tool not enabled for this repo, or UkoreHub needs a restart to have discovered it)\\n");'
    )
    return (
        f'{diagnostic}setProject "{_mel_string(repo_root)}"; '
        f'file -open -force{open_flags} "{_mel_string(scene_path)}";'
    )


_PLUGIN_FILE_EXTENSIONS = {".py", ".mll", ".pyd", ".so"}


def _force_load_plugin_names(contributions: dict, maya_version: str) -> list[str]:
    """Every compiled/script Maya plug-in file (.py/.mll/.pyd/.so) sitting
    directly in a contributed MAYA_PLUG_IN_PATH folder, by the module name
    Maya's own loadPlugin/pluginInfo commands identify it by (its filename
    without extension) — so open_maya_file can force-load them at launch
    instead of leaving the artist to tick "Auto Load" in Plug-in Manager by
    hand every session. Only sees plug-in files sitting directly in a
    contributed folder, same as Maya's own Plug-in Manager scan — a tool
    whose plug-in files live one level deeper (see MayaToolkit's nested
    maya-plug-ins/ subfolders) still won't be picked up here either."""
    names: list[str] = []
    seen: set[str] = set()
    for tool_id in sorted(contributions):
        by_version = contributions[tool_id].get("MAYA_PLUG_IN_PATH", {})
        paths = list(by_version.get(ANY_VERSION, [])) + list(by_version.get(maya_version, []))
        for path in paths:
            folder = Path(path)
            if not folder.is_dir():
                continue
            for entry in sorted(folder.iterdir()):
                if entry.is_file() and entry.suffix.lower() in _PLUGIN_FILE_EXTENSIONS and entry.stem not in seen:
                    seen.add(entry.stem)
                    names.append(entry.stem)
    return names


def _force_load_plugins_command(plugin_names: list[str]) -> str:
    """MEL that force-loads each plug-in by name, wrapped in `catch` so one
    plug-in failing to load (or already being marked loaded) can't abort
    the rest of the `-command` string — including the setProject/file-open
    that follows it. Runs before setProject/file-open so a scene that
    references plug-in node types (e.g. an ngSkinTools skin layer) opens
    without Maya flagging them as unknown nodes."""
    return "".join('catch(`loadPlugin -quiet "{}"`);'.format(name) for name in plugin_names)


def _build_maya_env(base_env: dict, contributions: dict, maya_version: str) -> dict:
    """Pure, testable: returns a new env dict with each enabled tool's paths
    prepended (not replaced) onto the env var it targets — prepending keeps
    whatever the artist's own Maya/mGear install already put there.
    `contributions` is the bridge's own already-filtered-to-enabled
    {tool_id: {var_name: {"*": [...], "<version>": [...]}}} shape;
    `maya_version` selects which version-specific entries also apply, on top
    of every "*" entry. Iterates tool_ids sorted for deterministic env var
    ordering across runs."""
    env = dict(base_env)
    for tool_id in sorted(contributions):
        for var_name, by_version in contributions[tool_id].items():
            paths = list(by_version.get(ANY_VERSION, [])) + list(by_version.get(maya_version, []))
            for new_entry in paths:
                existing = env.get(var_name)
                env[var_name] = f"{new_entry}{os.pathsep}{existing}" if existing else new_entry
    return env


def _read_bridge(api) -> tuple[dict, dict]:
    """Fresh read of the shared bridge's "contributions"/"labels" dicts,
    scoped to whichever Project is currently active (see
    MAYA_ENV_BRIDGE_PLUGIN_ID above) — empty if no project is active yet."""
    bridge = api.project_plugin_config_store(MAYA_ENV_BRIDGE_PLUGIN_ID)
    if bridge is None:
        return {}, {}
    return bridge.get("contributions", {}), bridge.get("labels", {})


def _find_linked_maya(api, repo) -> tuple[str, str] | None:
    """Resolves (exe_path, version) for the repo's required Maya Program
    from Settings > Software Linker, or None if nothing is linked yet.
    Shared by open_maya_file and launch_maya_standalone below."""
    linked = api.plugin_config_store(SOFTWARE_LINKER_PLUGIN_ID, shared=False)
    for program in _maya_programs_for_repo(api, repo):
        version = pinned_version(repo, program)
        candidate = linked.get(linked_key(program, version))
        if candidate:
            return candidate, version
    return None


def _prepare_env_and_plugins(api, repo, maya_version: str) -> tuple[dict, list[str], set[str]]:
    """Shared bridge read + env-merge + force-load-plugin-names — every
    part of a Maya launch that doesn't depend on whether a specific scene
    is being opened. Shared by open_maya_file and launch_maya_standalone
    below.

    Which tools' contributions actually apply is gated by
    `repo.required_plugin_ids` — Repository Setting > Enable Plugin — since
    every contributing tool (MayaNgSkin, MayaToolkit, ...) is
    its own plugin (plugins/repo_internal/<Name>/ or cache/plugins/<Name>/)
    with a manifest id that matches its bridge tool_id exactly (see this
    plugin's README). This
    used to be a separate opt-out toggle owned by this plugin
    (RepoToolsStore, removed 2026-08-05) — folded into Enable Plugin so
    there's a single place that decides whether a repo uses a given tool,
    instead of two. Enable Plugin is opt-in (unchecked by default), unlike
    the old opt-out store, so a repo that has never touched Enable Plugin
    now gets zero tool contributions until someone checks the ones it
    needs."""
    all_contributions, _labels = _read_bridge(api)
    enabled_tool_ids = set(repo.required_plugin_ids) & set(all_contributions)
    contributions = {tid: c for tid, c in all_contributions.items() if tid in enabled_tool_ids}
    if PUBLISH_API_TOOL_ID in all_contributions:
        contributions[PUBLISH_API_TOOL_ID] = all_contributions[PUBLISH_API_TOOL_ID]
    env = _build_maya_env(os.environ.copy(), contributions, maya_version or "")
    plugin_names = _force_load_plugin_names(contributions, maya_version or "")
    return env, plugin_names, enabled_tool_ids


def _warn_no_linked_maya() -> None:
    QMessageBox.warning(
        None,
        "Maya Launcher",
        "No linked Maya executable found for this repo's required "
        "Maya version. Configure it in Settings > Software Linker.",
    )


def register(api) -> None:
    def open_maya_file(path: Path, repo) -> bool:
        found = _find_linked_maya(api, repo)
        if found is None:
            _warn_no_linked_maya()
            return True  # handled (with a warning) — do not fall back to OS default
        maya_exe, maya_version = found

        env, plugin_names, enabled_tool_ids = _prepare_env_and_plugins(api, repo, maya_version)
        repo_root = _repo_root_path(api, repo)
        defer_reference_load = UKORE_REFERENCE_EDITOR_TOOL_ID in enabled_tool_ids
        mel_command = _force_load_plugins_command(plugin_names) + _set_project_and_open_command(
            repo_root, path, defer_reference_load=defer_reference_load
        )
        subprocess.Popen([maya_exe, "-command", mel_command], env=env)
        return True

    def launch_maya_standalone(repo) -> bool:
        """Launches Maya for `repo` with no scene to open — just the same
        setProject/env-merge/force-load-plugins wiring open_maya_file
        uses. Registered below as a ProgramLaunchSpec so
        plugins/core/program_launcher/'s card grid launches Maya
        through this instead of a bare subprocess.Popen of the raw
        linked exe."""
        found = _find_linked_maya(api, repo)
        if found is None:
            _warn_no_linked_maya()
            return True
        maya_exe, maya_version = found

        env, plugin_names, _enabled_tool_ids = _prepare_env_and_plugins(api, repo, maya_version)
        repo_root = _repo_root_path(api, repo)
        mel_command = _force_load_plugins_command(plugin_names) + f'setProject "{_mel_string(repo_root)}";'
        subprocess.Popen([maya_exe, "-command", mel_command], env=env)
        return True

    api.register_file_opener(PLUGIN_ID, MAYA_FILE_EXTENSIONS, open_maya_file)
    api.register_program_launcher(
        ProgramLaunchSpec(match=lambda program: "maya" in program.name.lower(), launch=launch_maya_standalone)
    )
    api.register_settings_tab(
        SettingsTabSpec(
            key=PLUGIN_ID,
            label="Maya Launcher",
            order=110,
            page_factory=lambda: MayaLauncherSettingsPage(api=api),
            on_activated=lambda page: page.refresh(),
            category=CATEGORY_REPO,
        )
    )
