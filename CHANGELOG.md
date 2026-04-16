# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Added `__slots__` to all dataclasses (`RankedFile`, `FocusTask`, `FocusBatch`, `Finding`) for improved memory efficiency
- Added `py.typed` marker file for PEP 561 type hint support
- Added `.advisorignore` support - create a `.advisorignore` file in your project root with glob patterns to exclude files from analysis
  - New `load_advisorignore(base_dir)` function to load patterns from file
  - New `ignore_patterns` parameter to `rank_files()` function
  - Supports glob patterns like `tests/`, `*.md`, `vendor/`

### UI/UX Improvements
- Added visual banner headers to `advisor status` for clearer hierarchy
- Improved first-run setup message with success box and quick-start guide
- Enhanced empty state message in `advisor plan` with helpful tips and emoji
- New styling helpers: `spinner_frame()`, `banner()`, `success_box()`, `info_box()`, `warning_box()`
- All UI improvements respect `NO_COLOR=1` for accessibility

## [0.3.0] - 2024-XX-XX

### Changed
- Advisor now uses Opus for direct discovery instead of delegating to Sonnet explorer
- Runners now receive custom prompts written by Opus based on structural discovery
- Added live two-way dialogue between advisor and runners throughout the pipeline

## [0.2.0] - 2024-XX-XX

### Added
- Initial support for `/advisor` slash command via skill installation
- File priority ranking (P1–P5) based on security-relevant keywords
- Focus batching for parallel runner dispatch
- Verification pass to filter findings
- CLI commands: `pipeline`, `plan`, `prompt`, `install`, `uninstall`, `status`

## [0.1.0] - 2024-XX-XX

### Added
- Initial release
- Basic advisor/runner pattern implementation
