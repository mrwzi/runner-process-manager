# Repository layout

Runner uses a standard source/build/publish separation:

```text
src/                 application source and bundled static inputs
  ui/ cluster/ manager/ launcher/ assets/ config/
build/scripts/       build, installer, upgrade, and packaging scripts
build/temp/          disposable PyInstaller, package, and archive staging
dist/windows/        generated Runner.exe and UpdateRunner.exe
dist/debian/         generated Debian packages
dist/portable/       generated portable archive
release/             verified user-facing release artifacts only
docs/                documentation
tests/               automated tests
```

`src/config/apps.json` is a development seed only. It is never a source of
machine identity, secrets, pairing data, witness credentials, logs, or live
configuration. Those are runtime data and are excluded from all artifacts.

Build from `build/scripts`; do not place generated binaries at repository
root. Publish only copies in `release/` after validation.
