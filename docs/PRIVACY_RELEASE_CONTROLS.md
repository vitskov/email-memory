# Privacy Release Controls

The publication boundary uses two complementary gates. Neither gate is a
substitute for the other.

## Hosted generic gate

The GitHub Actions `privacy` job contains only public, value-free policy. It
checks:

- tracked files and paths
- all Git objects reachable in the CI checkout
- the clean archive produced from the candidate commit
- built wheel and source distributions
- forbidden runtime/configuration artifacts, secret-shaped values, local
  machine paths, routing destinations, and non-synthetic fixture data

The job checks out complete history with persisted credentials disabled and
runs with read-only repository permissions. It must not contain a list of real
people, accounts, hosts, destinations, addresses, or deployment paths; placing
such values in workflow configuration would itself publish them.

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
separate, Git-ignored denylist containing its exact private identifiers. Before
any public push, its local release gate must scan the clean candidate repository,
every object reachable from its local refs, and a fresh archive of the candidate
commit. That scanner and its populated denylist belong to local-only operations
material, not this repository.

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
4. Run the deployment-specific local gate with its ignored denylist.
5. Push through a pull request and require the hosted `privacy` check to pass
   before merge.

Any failure is fail-closed. Remove the offending value from the complete
reachable history or recreate the sanitized publication history; deleting it
only from the latest tree is insufficient.
