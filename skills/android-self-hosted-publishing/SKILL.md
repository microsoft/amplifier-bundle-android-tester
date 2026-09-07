---
name: android-self-hosted-publishing
description: |
  Use when building, reviewing, or debugging a self-hosted Android update rail — shipping
  builds to testers without a store — or when a device bug report needs its first question
  answered: is the fix actually on the device? Covers signed APK plus signed manifest
  sidecar, versionCode monotonicity enforcement, HTTPS-first with a TLS-only HTTP
  fallback, and installed-build verification.
version: 1.0.0
license: MIT
---

# Android Self-Hosted Publishing

Getting a build onto a tester's phone repeatedly, without a store, comes down to a small
number of rules. All of these were validated by a rail that ran daily against a real device
during an extended field project — each rule below fired for real at least once.

## `versionCode` must strictly increase — refuse otherwise

A publish step that would serve changed bytes under an existing `versionCode` must
**refuse**, naming the collision.

This is not theoretical tidiness. The refusal fired on the rail's first real use, catching a
genuine attempt to republish changed bytes under the same version. Without it, that build
reaches a device whose updater correctly decides it is already up to date — producing the
worst possible failure shape: a tester debugging a bug that was already fixed, on a build
that silently never arrived.

## Signed APK plus a signed manifest sidecar

The shape that worked:

| Artifact | Contents |
|---|---|
| `app-release.apk` | The signed release build |
| `app-release.json` (sidecar, signed) | `version`, `versionCode`, `sha256` of the APK |

The in-app updater verifies **both** the signature on the manifest **and** the `sha256` of
the downloaded APK against the manifest before installing. Two independent checks: the
signature says who published it, the hash says the bytes are intact.

Two health checks are worth building in, because both failures are silent:

- **published-build-is-signed** — a debug-signed artifact served from the release channel
  installs fine and then fails to update over the previously-installed release build.
- **served-build-is-current** — the thing the server is *actually serving* matches the
  latest build produced. A publish step that succeeded while writing to the wrong path
  passes every other check.

## HTTPS first, fall back to HTTP only on a TLS failure — never on a 404

The update client tries HTTPS first, reusing **the app's own already-pinned CA and trust
manager** rather than constructing a new permissive one. It falls back to plain HTTP only
when the failure is TLS or connect-level.

**A 404 is an answer.** Falling back to HTTP on a 404 converts "the server does not have
this file" into "silently retry the same missing file with less security". The distinction
is the whole point: a transport failure means *try another transport*; an HTTP status means
*the server responded, and this is what it said*.

Reuse the existing trust manager. An updater that builds its own TLS context is the classic
place a permissive trust-all implementation gets introduced "temporarily" and stays.

## Prove the installed build is the build you just made

**Symptom:** a fix that is definitely in the code is definitely not in the app. Hours
disappear into debugging code that is not running.

**Technique:** compare hashes, not version strings:

```bash
LOCAL=$(sha256sum app-debug.apk | cut -d' ' -f1)
DEV_PATH=$(adb -s "$SERIAL" shell pm path com.example.app | sed 's/package://' | tr -d '\r')
DEV=$(adb -s "$SERIAL" shell sha256sum "$DEV_PATH" | cut -d' ' -f1 | tr -d '\r')
[ "$LOCAL" = "$DEV" ] && echo "installed build IS the local build" || echo "MISMATCH"
```

Make this the first question of any device bug report, before reproducing anything: what
build is on the device, and is it the one containing the fix? A version string can be stale
(it is only correct if someone remembered to bump it); a hash cannot.

## Source

Full narrative and additional context: `docs/FIELD-NOTES-2026-08.md` §5, "App publishing
and self-hosted update rails".
