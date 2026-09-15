# Repository Setup

Recommended repository name: `MQL-Indicator-Library`

## Initial GitHub settings

- Visibility: Private while in beta/testing.
- Default branch: `main`.
- Actions: Enabled.
- Workflow permissions: Read and write permissions are sufficient for future release automation; current beta build only needs standard Actions artifact upload.

## First build

Open GitHub > Actions > **Windows Beta Installer** > **Run workflow**.

The workflow should create a downloadable artifact containing the Windows NSIS installer.

## Release direction

Keep beta builds as Actions artifacts until installation, scanning, persistence, performance, and classification are validated. After that, enable signed releases and the in-app updater using a permanent signing key.
