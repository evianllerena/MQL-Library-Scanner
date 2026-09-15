# Build the first Windows beta installer

The recommended path is GitHub Actions. Your normal PC does not need Python, Node, Rust, or PyInstaller to run the finished application.

## GitHub Actions build

1. Create a private GitHub repository for this project.
2. Upload the contents of this folder to the repository root.
3. Open **Actions → Windows Beta Build → Run workflow**.
4. Wait for the workflow to finish.
5. Open the completed workflow run and download the artifact named **MQL-Indicator-Library-Windows-Beta**.
6. Inside that artifact, run the generated `*_x64-setup.exe` installer.

For Beta 0.2, updater signing is intentionally not required because `createUpdaterArtifacts` is disabled. The updater UI remains present but will report unavailable until a permanent update endpoint and signing key are configured.

## Local Windows build (fallback)

If GitHub Actions is unavailable, run PowerShell as a normal user from the project root:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\build_windows.ps1
```

The build machine requires Python 3.11+, Node.js 20+, Rust stable, and the Microsoft C++ build tools. These are build-time dependencies only and are not required on PCs that install the finished Setup.exe.

The installer output will be under:

`src-tauri\target\release\bundle\nsis\`
