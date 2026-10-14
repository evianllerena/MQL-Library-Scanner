# Build the first Windows beta installer

The installer is built by GitHub Actions only. Your normal PC does not need Python, Node, Rust, or PyInstaller to run the finished application.

## GitHub Actions build

1. Create a private GitHub repository for this project.
2. Upload the contents of this folder to the repository root.
3. Open **Actions → Windows Beta Build → Run workflow**.
4. Wait for the workflow to finish.
5. Open the completed workflow run and download the artifact named **MQL-Indicator-Library-Windows-Beta**.
6. Inside that artifact, run the generated `*_x64-setup.exe` installer.

For Beta 0.2, updater signing is intentionally not required because `createUpdaterArtifacts` is disabled. The updater UI remains present but will report unavailable until a permanent update endpoint and signing key are configured.

## No local build

There is no supported local build script (the old `build_windows.ps1` pointed above the repo, expected a
layout that only exists inside `MQL-Indicator-Library-Repo-Ready.zip`, and built only 2 of the 5 sidecars,
so it was removed). The buildable Tauri layout exists only inside that zip, which the workflow extracts and
overlays with the repo-root sources. Use the GitHub Actions build above.
