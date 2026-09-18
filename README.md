# MQL Indicator Library — Beta 0.2 source build

This is the first desktop-beta structure for the local MQ4/MQ5 indicator library.

## User-facing architecture

- **One Windows installer** for end users. Python, Node and Rust are **not** required on the user's PC.
- Tauri 2 desktop shell for a lightweight, responsive Windows UI.
- The classifier is packaged as an internal `mql-engine.exe` sidecar and runs outside the UI process.
- SQLite library is stored in the application's local data directory and survives app updates.
- Core classification/search works offline.
- Tauri updater support is wired into the UI; a permanent signing key and HTTPS release endpoint must be configured before shipping updater-enabled builds.

## Implemented in this beta source

- Source-folder picker.
- Multiple source folders.
- Recursive `.mq4` / `.mq5` scanning.
- MQ4/MQ5 source classification engine from Prototype 0.1.
- Converted MQL4→MQL5 compatibility-wrapper filtering.
- SQLite persistence.
- **Incremental scans:** unchanged files are skipped using file size + nanosecond modified timestamp.
- Exact duplicate detection via SHA-256.
- Library search.
- Platform and category filters.
- Indicator detail drawer.
- Review queue for low-confidence / unknown results.
- Update-check UI path.
- Windows NSIS/current-user installer configuration.
- GitHub Actions Windows build workflow.

## Verified engine behavior on the supplied 100-file sample

First scan: 100 processed, 0 failed.

Immediate second scan: 0 processed, 100 skipped, 0 failed.

This confirms incremental scanning is active before scaling to the full library.

## Important build status

This environment is not Windows and does not have the Rust toolchain, so a Windows installer has **not** been fabricated or falsely labeled as tested. The Windows packaging source and build workflow are included and are intended to compile on a Windows build machine / GitHub Actions runner.

## Building on Windows

Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1
```

The development/build machine needs Python 3.11+, Node 20+ and Rust 1.77.2+. Those dependencies are build-time only; end users receive the resulting Setup.exe.

## Updater

Before distributing an updater-enabled release, create one permanent Tauri updater signing key and configure the public key + HTTPS `latest.json` endpoint. Never rotate or lose the private key after distribution unless you intentionally establish a migration plan.


## First Windows installer
See `BUILD_WINDOWS.md` for the GitHub Actions and local Windows build paths, and `FIRST_TEST_PLAN.md` for the first installed beta test.
