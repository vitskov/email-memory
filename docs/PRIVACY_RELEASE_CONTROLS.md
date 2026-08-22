# Privacy Release Controls

[Documentation index](README.md) | [Architecture](ARCHITECTURE_OVERVIEW.md) |
[Installation and packaging](INSTALLATION.md)

The publication boundary uses two complementary gates. Neither gate is a
substitute for the other.

## Hosted generic gate

The GitHub Actions `privacy` job contains only public, value-free policy. It
checks:

- tracked files and paths
- all Git objects reachable in the CI checkout, including commit identities and
  messages, annotated-tag metadata, historical tree paths, and blob contents
- the clean archive produced from the candidate commit
- built wheel and source distributions
- forbidden runtime/configuration artifacts, secret-shaped values, local
  machine paths, routing destinations, and non-synthetic fixture data

The job checks out complete history with persisted credentials disabled and
runs with read-only repository permissions. It must not contain a list of real
people, accounts, hosts, destinations, addresses, or deployment paths; placing
such values in workflow configuration would itself publish them.

For pull requests, the privacy job checks out the contributor's actual head
repository and commit rather than GitHub's synthetic merge commit. The other CI
jobs still exercise the merge result. This prevents GitHub-owned merge metadata
from being mistaken for project history while preserving the publishable-history
gate.

For Git-backed scans, the gate validates and invokes the absolute system Git
binary rather than resolving `git` through `PATH`. It runs Git with a minimal
fixed environment, ignores inherited `GIT_*` settings, disables system and
global configuration, interactive prompts and askpass, repository-configured
filesystem monitors and hooks, and credential helpers. If this trusted boundary
cannot be established, the gate fails closed without printing private values.
Replacement objects and legacy graft files are disabled so local topology
overlays cannot hide original reachable history, and `--git-history` rejects a
shallow repository rather than treating its truncated object set as complete.

Artifact bytes are decoded loss-tolerantly for inspection. Binary files and
archive members are therefore not rejected merely for being binary, but NUL or
invalid UTF-8 bytes cannot hide a later identifier, email, or secret-shaped
value. Oversized content remains a fail-closed finding.

The generic scanner can be run before opening a pull request:

```bash
uv run --no-project --python 3.14 -- \
  python scripts/privacy_gate.py --tracked --git-history
```

GitHub repository rules for `main` must require the `privacy` status check. A
workflow file alone reports failures but does not prevent a direct push or a
merge unless the repository rule requires the check and restricts bypasses.

## Local deployment-specific gate

The machine or deployment that owns private configuration must maintain a
local denylist containing its exact private identifiers. A typical location is
`$HOME/.config/email-memory-store/public-export-denylist.txt` (or the equivalent
under `$XDG_CONFIG_HOME`). Keep both the file and its contents outside the
repository: the directory must be current-user owned and mode `0700`, and the
file must be current-user owned, a regular non-symlink file, and mode `0600`.

The file contains one literal identifier per line. Matching is
case-insensitive and uses identifier boundaries, so a rule does not match as a
substring of a longer word-like identifier. Blank lines and lines whose first
non-whitespace character is `#` are ignored. The scanner reports only the
generic `local-denylist-identifier` rule and its scan location; it never prints
the configured identifier.

Create the owner-only storage and run the same public scanner with the local
rules enabled:

```bash
install -d -m 0700 "$HOME/.config/email-memory-store"
umask 077
touch "$HOME/.config/email-memory-store/public-export-denylist.txt"
chmod 0600 "$HOME/.config/email-memory-store/public-export-denylist.txt"

uv run --no-project --python 3.14 -- \
  python scripts/privacy_gate.py \
  --local-denylist "$HOME/.config/email-memory-store/public-export-denylist.txt" \
  --tracked --git-history .privacy-ci/email-memory.tar.gz dist/*
```

Before any public push, apply this option while scanning the clean candidate
tree, every object reachable from local refs, a fresh archive of the candidate
commit, and the wheel and source distributions. The populated file belongs to
owner-only local configuration; a second private code repository or a
deployment-specific scanner is not required.

This split is intentional:

- hosted CI catches generic privacy regressions consistently for every change;
- the local gate catches exact deployment-specific values without disclosing
  those values to GitHub;
- scanning both history and artifacts prevents a clean working tree from hiding
  a leak in an older commit or a generated distribution.

## Release sequence

1. Prepare the change in the sanitized public checkout using only synthetic
   fixtures and reserved example domains.
2. Commit the candidate and ensure the checkout has no tracked or untracked
   changes.
3. Run the hosted-equivalent generic privacy scanner locally.
4. Run the generic scanner across the same release surfaces with
   `--local-denylist` pointing to the owner-only local file.
5. Push the exact candidate commit through a pull request and require the
   hosted `privacy` check to pass.
6. Publish that exact tested commit with a fast-forward update of `main`.
   Do not use a GitHub merge commit, squash merge, or server-side rebase: those
   operations create new commit metadata that the local identifier gate cannot
   validate before publication.

Any failure is fail-closed. Remove the offending value from the complete
reachable history or recreate the sanitized publication history; deleting it
only from the latest tree is insufficient.
