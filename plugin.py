from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from plugin_api import CATEGORY_REPO, NotFoundError, ProgramLaunchSpec, SettingsTabSpec

# Standalone-repo import fix
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from link_resolution import linked_key, pinned_version
from settings_page import MayaLauncherSettingsPage

PLUGIN_ID = "maya_launcher"
SOFTWARE_LINKER_PLUGIN_ID = "software_linker"
MAYA_ENV_BRIDGE_PLUGIN_ID = "maya_launcher_env_bridge"
ANY_VERSION = "*"
PUBLISH_API_TOOL_ID = "publish_api"

MAYA_FILE_EXTENSIONS = [".ma", ".mb"]


def _maya_programs_for_repo(api, repo):
    """Every Program the repo requires whose name contains "maya"."""
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
    """The repo's cloned root folder on this machine."""
    return Path(api.local_config.workspace_root) / repo.local_path


def _mel_string(path: Path) -> str:
    """A filesystem path as a MEL string literal."""
    return str(path).replace("\\", "/").replace('"', '\\"')


def _build_launch_commands(
    repo_root: Path,
    scene_path: Path | None,
    active_hooks: list[dict],
) -> tuple[list[str], str]:
    """Assembles extra CLI flags and MEL command string from registered tool hooks
    sorted by their declared 'order' priority.

    Returns:
        tuple[cli_flags, mel_command]
    """
    sorted_hooks = sorted(active_hooks, key=lambda h: h.get("order", 100))

    cli_flags: list[str] = []
    pre_mel_parts: list[str] = []
    post_mel_parts: list[str] = []
    diagnostics: list[str] = []

    for hook in sorted_hooks:
        if "cli_flags" in hook and isinstance(hook["cli_flags"], list):
            cli_flags.extend(hook["cli_flags"])
        if "pre_open_mel" in hook and hook["pre_open_mel"]:
            pre_mel_parts.append(hook["pre_open_mel"].strip())
        if "post_open_mel" in hook and hook["post_open_mel"]:
            post_mel_parts.append(hook["post_open_mel"].strip())
        if "diagnostic_msg" in hook and hook["diagnostic_msg"]:
            diagnostics.append(hook["diagnostic_msg"].strip())

    mel_parts: list[str] = []

    # 1. Print diagnostic logs
    for diag in diagnostics:
        mel_parts.append(f'print("[MayaLauncher] {diag}\\n");')

    # 2. Pre-open MEL commands from tools
    if pre_mel_parts:
        mel_parts.append(" ".join(pre_mel_parts))

    # 3. Set Project
    mel_parts.append(f'setProject "{_mel_string(repo_root)}";')

    # 4. Open Scene (if launching with a file)
    if scene_path is not None:
        mel_parts.append(f'file -open -force "{_mel_string(scene_path)}";')

    # 5. Post-open MEL commands
    if post_mel_parts:
        mel_parts.append(" ".join(post_mel_parts))

    return cli_flags, " ".join(mel_parts)


_PLUGIN_FILE_EXTENSIONS = {".py", ".mll", ".pyd", ".so"}


def _force_load_plugin_names(contributions: dict, maya_version: str) -> list[str]:
    """Every compiled/script Maya plug-in file sitting directly in MAYA_PLUG_IN_PATH."""
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
    """MEL that force-loads each plug-in by name."""
    return "".join('catch(`loadPlugin -quiet "{}"`);'.format(name) for name in plugin_names)


def _build_maya_env(base_env: dict, contributions: dict, maya_version: str) -> dict:
    """Returns a new env dict with each enabled tool's paths prepended."""
    env = dict(base_env)
    for tool_id in sorted(contributions):
        for var_name, by_version in contributions[tool_id].items():
            paths = list(by_version.get(ANY_VERSION, [])) + list(by_version.get(maya_version, []))
            for new_entry in paths:
                existing = env.get(var_name)
                env[var_name] = f"{new_entry}{os.pathsep}{existing}" if existing else new_entry
    return env


def _read_bridge(api) -> tuple[dict, dict, dict]:
    """Fresh read of the shared bridge's contributions, labels, and launch_hooks dicts."""
    bridge = api.project_plugin_config_store(MAYA_ENV_BRIDGE_PLUGIN_ID)
    if bridge is None:
        return {}, {}, {}
    return (
        bridge.get("contributions", {}),
        bridge.get("labels", {}),
        bridge.get("launch_hooks", {}),
    )


def _find_linked_maya(api, repo) -> tuple[str, str] | None:
    """Resolves (exe_path, version) for the repo's required Maya Program."""
    linked = api.plugin_config_store(SOFTWARE_LINKER_PLUGIN_ID, shared=False)
    for program in _maya_programs_for_repo(api, repo):
        version = pinned_version(repo, program)
        candidate = linked.get(linked_key(program, version))
        if candidate:
            return candidate, version
    return None


def _prepare_env_and_plugins(
    api, repo, maya_version: str
) -> tuple[dict, list[str], list[dict]]:
    """Bridge read + env-merge + force-load-plugin-names + active hooks resolution."""
    all_contributions, _labels, all_hooks = _read_bridge(api)
    enabled_tool_ids = set(repo.required_plugin_ids) & set(all_contributions)
    
    contributions = {tid: c for tid, c in all_contributions.items() if tid in enabled_tool_ids}
    if PUBLISH_API_TOOL_ID in all_contributions:
        contributions[PUBLISH_API_TOOL_ID] = all_contributions[PUBLISH_API_TOOL_ID]

    # Resolve hooks for enabled tools
    active_hooks = [
        hook_data for tid, hook_data in all_hooks.items() if tid in enabled_tool_ids
    ]

    env = _build_maya_env(os.environ.copy(), contributions, maya_version or "")
    plugin_names = _force_load_plugin_names(contributions, maya_version or "")
    return env, plugin_names, active_hooks


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
            return True
        maya_exe, maya_version = found

        env, plugin_names, active_hooks = _prepare_env_and_plugins(api, repo, maya_version)
        repo_root = _repo_root_path(api, repo)

        cli_flags, custom_mel = _build_launch_commands(repo_root, path, active_hooks)
        mel_command = _force_load_plugins_command(plugin_names) + custom_mel

        cmd_args = [maya_exe] + cli_flags + ["-command", mel_command]
        subprocess.Popen(cmd_args, env=env)
        return True

    def launch_maya_standalone(repo) -> bool:
        found = _find_linked_maya(api, repo)
        if found is None:
            _warn_no_linked_maya()
            return True
        maya_exe, maya_version = found

        env, plugin_names, active_hooks = _prepare_env_and_plugins(api, repo, maya_version)
        repo_root = _repo_root_path(api, repo)

        cli_flags, custom_mel = _build_launch_commands(repo_root, None, active_hooks)
        mel_command = _force_load_plugins_command(plugin_names) + custom_mel

        cmd_args = [maya_exe] + cli_flags + ["-command", mel_command]
        subprocess.Popen(cmd_args, env=env)
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