# Changelog

## 5.1.0 - 2026-09-11

### Added

- Added English and Spanish README documentation links.
- Added CLI commands for serving the Web UI, inspecting devices, and reading task results.
- Added separated FastAPI routers and service modules for devices, tasks, comparisons, labels, and exports.
- Added iOS Simulator support and native build tooling.

### Changed

- Reorganized the package structure for the FastAPI application and platform collectors.
- Updated packaging metadata and locked project dependencies.
- Published `client-perf` 5.1.0 to PyPI.

### Validation

- Python source compilation passed.
- Application import and local Web API smoke checks passed.
- PyPI wheel and source distribution metadata checks passed.