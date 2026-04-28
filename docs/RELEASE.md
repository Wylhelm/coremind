# Release Process

**Version:** 0.1
**Status:** Stable
**Audience:** Maintainers cutting a tagged release of CoreMind.

---

## Versioning policy

CoreMind follows [Semantic Versioning 2.0.0](https://semver.org).

- **MAJOR** — incompatible changes to a public contract:
  - the `WorldEvent` schema (`spec/worldevent.schema.json`)
  - the plugin gRPC protocol (`spec/plugin.proto`)
  - the audit journal entry shape (`spec/audit_log.md`)
  - the `coremind` CLI surface
  - any Python symbol re-exported from `coremind.__init__`
- **MINOR** — backward-compatible additions: new layers, optional fields, new CLI subcommands, new plugin permissions, new dashboard pages.
- **PATCH** — bug fixes, doc-only changes, dependency bumps that do not change observable behavior.

Pre-1.0 caveat: while the project is `0.x`, MINOR bumps may break compatibility, but we will document the break in `CHANGELOG.md` and provide a migration note. We will not silently break things.

### Schema and protocol versions

The `source_version` field on `WorldEvent` and the `protocol_version` field on the plugin handshake are **independent** of the package version. They follow their own semver, advanced only when the contract changes. A single CoreMind release may bundle multiple frozen schema versions for compatibility.

### Supported branches

- `main` — current development line; only the latest MINOR receives bug-fix backports.
- `release/0.x` branches are cut at MAJOR boundaries if long-term support is needed.

---

## Pre-release checklist

Run before tagging. Each item must pass.

- [ ] `just lint` — Ruff + mypy strict, zero findings.
- [ ] `just test` — unit suite, all green.
- [ ] `just test-integration` — integration suite with `docker compose up -d`, all green.
- [ ] `just spec-validate` — JSON schemas and example payloads validate.
- [ ] `just proto-gen-check` — generated stubs are committed.
- [ ] `coremind audit verify` against a soak instance — journal hash chain intact.
- [ ] `CHANGELOG.md` updated — move entries from `[Unreleased]` to the new version with today's date.
- [ ] `pyproject.toml` `version` bumped.
- [ ] Phase doc for the active phase has its Success Criteria checked off.
- [ ] `README.md` Project Status section reflects the new release.

---

## Cutting a release

Releases are cut from `main`. The maintainer cutting the release must hold the project release-signing key (see [Release signing](#release-signing)).

```bash
# 1. Sync and verify
git checkout main
git pull --ff-only
just ci

# 2. Bump version and finalize CHANGELOG
$EDITOR pyproject.toml CHANGELOG.md
git add pyproject.toml CHANGELOG.md
git commit -m "chore(release): v0.1.0"

# 3. Tag, signed
git tag -s v0.1.0 -m "CoreMind v0.1.0"

# 4. Build artifacts
.venv/bin/python -m pip install --upgrade build
.venv/bin/python -m build         # produces dist/coremind-0.1.0.tar.gz and .whl

# 5. Sign artifacts
gpg --detach-sign --armor dist/coremind-0.1.0.tar.gz
gpg --detach-sign --armor dist/coremind-0.1.0-py3-none-any.whl

# 6. Push
git push origin main
git push origin v0.1.0
```

After the tag is pushed, the GitHub release workflow attaches the four `dist/` files (sdist, wheel, and their `.asc` signatures) to the GitHub release page and generates release notes from the `CHANGELOG.md` section.

If the release workflow is unavailable, attach the artifacts manually via `gh release create v0.1.0 dist/*`.

---

## Install paths

Each tagged release supports the following install methods. CI smoke-tests each one before the release is marked latest.

### `pipx` (recommended for end users)

```bash
pipx install coremind
coremind --version
```

This installs the daemon CLI into an isolated environment. End users get the `coremind` entry point on `$PATH`. Plugins published as separate packages are installed alongside:

```bash
pipx inject coremind coremind-plugin-systemstats
```

### Docker image

A multi-stage image is published to GitHub Container Registry as `ghcr.io/gagnongui/coremind:<version>` and `ghcr.io/gagnongui/coremind:latest`. The image bundles the daemon and the reference plugins; SurrealDB and Qdrant remain external services brought up via the [`docker-compose.yml`](../docker-compose.yml) at the repository root.

```bash
docker pull ghcr.io/gagnongui/coremind:0.1.0
docker compose -f docker-compose.yml up -d   # SurrealDB + Qdrant
docker run --rm -v ~/.coremind:/root/.coremind --network host \
  ghcr.io/gagnongui/coremind:0.1.0 daemon start
```

### From source

```bash
git clone https://github.com/gagnongui/coremind
cd coremind
git checkout v0.1.0
just setup
```

### Standalone binary (deferred)

A single-file binary (PyInstaller, or a future Rust port of the hot path) is **not** part of v0.1.0. Tracked as a post-release deliverable in `docs/phases/PHASE_4_REFLECTION_ECOSYSTEM.md` (Post-Release section).

---

## Release signing

CoreMind tags and release artifacts are signed with the project GPG key. The fingerprint is published in the repository root as `RELEASE-KEY.asc` and on the GitHub release page.

- **Tags:** `git tag -s` — required for every annotated release tag.
- **Artifacts:** detached ASCII-armored signatures (`.asc`) for each sdist and wheel.
- **Verification:**

  ```bash
  gpg --import RELEASE-KEY.asc
  gpg --verify dist/coremind-0.1.0.tar.gz.asc dist/coremind-0.1.0.tar.gz
  git tag --verify v0.1.0
  ```

Key rotation: if the release key is ever compromised, the next release ships under a new key and a `SECURITY.md` advisory is filed within 24 hours documenting the rotation. The old key is revoked publicly.

The release-signing key is **distinct** from the daemon's runtime ed25519 keypair (`~/.coremind/keys/daemon.ed25519`). Do not reuse keys across the two roles.

---

## Hot-fix releases

A `vX.Y.Z+1` patch can be cut from a `release/X.Y` branch when:

1. The fix has a regression test.
2. The fix passes `just ci` on the release branch.
3. The fix is already merged to `main` (forward-port first).

Hot-fix tags follow the same signing and artifact procedure as MAJOR/MINOR tags.

---

## Yanking a release

If a published release is found to corrupt user data, leak secrets, or introduce a remote-exploitable vulnerability:

1. Mark the GitHub release as a pre-release and add a **DO NOT INSTALL** banner.
2. `pip` yank: `twine upload --skip-existing` cannot yank; use the PyPI web UI or `pypi-cli yank coremind 0.1.0 -r <reason>`.
3. Tag a `vX.Y.Z+1` with the fix using the hot-fix flow.
4. File a `SECURITY.md` advisory with the disclosure window agreed in `CONTRIBUTING.md`.

Yanked versions stay listed for forensic purposes; they are not deleted.
