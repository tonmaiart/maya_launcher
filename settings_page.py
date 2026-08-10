from __future__ import annotations

from PySide6.QtWidgets import QGroupBox, QLabel, QVBoxLayout, QWidget

from core.exceptions import NotFoundError
from plugins.repo_internal.maya_launcher.link_resolution import linked_key, pinned_version

SOFTWARE_LINKER_PLUGIN_ID = "software_linker"


class MayaLauncherSettingsPage(QWidget):
    """Settings > Maya Launcher — Software Linker status readout (✅/⚠️ per
    required Maya `Program`) for whichever repo is active when this tab is
    shown; see on_activated wiring in plugin.py's register(), which calls
    refresh() every time this tab becomes visible —
    interface/settings_tab_registry.py's SettingsTabSpec.on_activated.

    Per-tool env enable/disable ("Enabled Tools for Active Repo") used to
    live here too, backed by its own RepoToolsStore opt-out toggle — removed
    2026-08-05. Every Maya tool (MayaNgskin, MayaToolkit, ...)
    is its own plugins/repo_internal/<Name>/ plugin now, each already
    toggleable per repo via Repository Setting > Enable Plugin
    (Repo.required_plugin_ids), so a second, separate toggle for the same
    decision here was redundant. See this plugin's own README.md ("Per-repo
    tool gating") for the full history."""

    def __init__(self, parent=None, *, api):
        super().__init__(parent)
        self._api = api

        self._active_repo_label = QLabel("")
        self._active_repo_label.setWordWrap(True)

        self._link_status_label = QLabel("")
        self._link_status_label.setWordWrap(True)
        link_group = QGroupBox("Software Linker Status")
        link_layout = QVBoxLayout(link_group)
        link_layout.addWidget(self._link_status_label)

        layout = QVBoxLayout(self)
        layout.addWidget(self._active_repo_label)
        layout.addWidget(link_group)
        layout.addStretch()

        self.refresh()

    def refresh(self) -> None:
        project_id = self._api.local_config.active_project_id
        repo_id = self._api.local_config.active_repo_id
        if not project_id or not repo_id:
            self._active_repo_label.setText("No repo selected.")
            self._link_status_label.setText("")
            return

        try:
            project = self._api.metadata.get_project(project_id)
            repo = self._api.metadata.get_repo(project_id, repo_id)
        except NotFoundError:
            self._active_repo_label.setText("No repo selected.")
            self._link_status_label.setText("")
            return

        self._active_repo_label.setText(f"Active repo: {project.name} / {repo.name}")
        self._refresh_link_status(project_id, repo)

    def _refresh_link_status(self, project_id: str, repo) -> None:
        maya_programs = []
        for program_id in repo.required_program_ids:
            try:
                program = self._api.metadata.get_program(project_id, program_id)
            except NotFoundError:
                continue
            if "maya" in program.name.lower():
                maya_programs.append(program)

        if not maya_programs:
            self._link_status_label.setText("This repo doesn't require Maya.")
            return

        linked = self._api.plugin_config_store(SOFTWARE_LINKER_PLUGIN_ID, shared=False)
        lines = []
        for program in maya_programs:
            version = pinned_version(repo, program)
            path = linked.get(linked_key(program, version))
            version_label = f"v{version}" if version else ""
            if path:
                lines.append(f"✅ {program.name} {version_label} — linked: {path}")
            else:
                lines.append(
                    f"⚠️ {program.name} {version_label} — not linked. "
                    "Configure it in Settings > Software Linker."
                )
        self._link_status_label.setText("\n".join(lines))
