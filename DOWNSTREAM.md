# Gloss downstream maintenance

This repository is a public, AGPL-licensed downstream of
[`funstory-ai/BabelDOC`](https://github.com/funstory-ai/BabelDOC), maintained for
the Gloss macOS application. It preserves upstream history and keeps Gloss
integration changes out of the application-side Swift runner.

The exact upstream tag and commit are recorded in
[`UPSTREAM_BASE.toml`](./UPSTREAM_BASE.toml). The compatibility baseline is
upstream `v0.6.3`; the active base is upstream `v0.6.4`.

## Scope

This downstream owns the document-processing runtime boundary used by Gloss:

- a stable process protocol and capability handshake;
- structured progress, batch results, cancellation, and service health;
- PDF pipeline performance work that touches BabelDOC internals;
- reproducible runtime packaging and release provenance.

Gloss continues to own the macOS interface, task queue, runtime installation,
updates, rollback, and translation-provider configuration. The boundary should
remain ordinary process arguments plus JSON/JSONL; Gloss must not import or
monkeypatch BabelDOC Python internals.

Generic fixes should be proposed upstream whenever practical. Downstream
patches must stay small, independently reviewable, and listed in
[`DOWNSTREAM_PATCHES.md`](./DOWNSTREAM_PATCHES.md) with a clear removal
condition.

## Versioning

Downstream Python versions use `UPSTREAM_VERSION+gloss.N`, for example
`0.6.4+gloss.1`. Git tags use `vUPSTREAM_VERSION-gloss.N`, for example
`v0.6.4-gloss.1`.

Downstream builds are distributed through GitHub Releases or a Gloss-managed
runtime manifest. They are not published to the public PyPI project owned by
upstream. Every binary release must link to its exact source tag and include
artifact checksums, dependency/build inputs, license notices, and provenance.

## Upstream synchronization

Synchronize one reviewed upstream release tag at a time:

1. Fetch the named tag from the read-only `upstream` remote. Do not push tags.
2. Verify both its tag object and peeled commit against GitHub.
3. Create `sync/upstream-vX.Y.Z` from `origin/main`.
4. Merge the verified peeled commit with `--no-ff`; never rebase published
   downstream history.
5. Update `UPSTREAM_BASE.toml`, the downstream version, capability metadata,
   and the patch ledger.
6. Open a draft PR and run the complete PDF, packaging, and macOS checks.
7. Merge an upstream-sync PR with a merge commit. Ordinary downstream PRs may
   be squash-merged.

An automated watcher may report new upstream releases, but it must not merge or
publish them without review.

## License and source

The original project and all downstream modifications remain licensed under
the GNU Affero General Public License described in [`LICENSE`](./LICENSE).
Copyright, warranty, and upstream attribution notices must be preserved.
Distributed runtime builds must provide the corresponding source for that exact
build; linking only to a moving `main` branch is insufficient.
