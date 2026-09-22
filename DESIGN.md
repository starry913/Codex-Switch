# Codex Switch desktop

Purpose: switch the connection used by an existing Codex installation, without moving its work data.

Use a compact utility window, not a dashboard. Keep the environment selector at the top right and the current connection in a single dark blue strip. The strip stays distinct from the saved service list so selecting a profile cannot be confused with activating it.

Palette: background #F0F3F8; ink #202B43; secondary #65738B; action #3559C7; selection #DFE7FF; surface #FFFFFF.

Typography: Segoe UI Semibold for the restrained product wordmark, Microsoft YaHei UI for Chinese UI, Consolas for the current local path. System fonts keep the application offline and legible on Windows; Tk fallback applies on Linux.

Layout: environment → actual connection and data location → saved services → history and recovery / switch action → operation status. Avoid quotas or health badges without measurements. Environment changes immediately clear the unlocked profile list.

The identifying element is the current-connection strip: it ties the selected service to the actual local data location. The rest of the UI remains quiet. Review removed decorative cards and summary dashboards, since neither helps perform this single task.
