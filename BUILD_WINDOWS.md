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

## Building locally (same steps as CI)

The repository is the Tauri project: `ui/` (frontend), `src-tauri/` (Rust app, config, capabilities,
installer hook), `index.html` and `package.json` at the root, and the Python sidecar sources at the root.
(The old `build_windows.ps1` and the `MQL-Indicator-Library-Repo-Ready.zip` baseline were removed.)
`.github/workflows/windows-beta.yml` is the reference build script; on Windows with Python 3.12, Node 22
and Rust stable the steps are:

1. copy the Python engine files (`engine*.py`, `mql_text.py`, `preview_*.py`, `nnfx_*.py`,
   `requirements-build.txt`) into `engine/` and `pip install -r engine/requirements-build.txt`;
2. build the four sidecars with PyInstaller (`mql-engine`, `mql-preview`, `mql-preview-control`,
   `mql-backtest-batch`) and copy them into `src-tauri/binaries/` with the `-x86_64-pc-windows-msvc.exe` suffix;
3. `npm install` and `npm run tauri build`.

The installer lands in `src-tauri/target/release/bundle/nsis/`.
