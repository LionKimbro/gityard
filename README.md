# gityard

`gityard` is a `lionscliapp` command line tool for evaluating whether a local Git checkout can be deleted without losing work.

## Commands

`gityard scan-local`
Scans the managed root and writes `.gityard/local-repos.json`.

`gityard scan-github`
Fetches GitHub repositories for `github.user` and writes `.gityard/github-repos.json`.

`gityard scan`
Runs `scan-local`, `scan-github`, and `analyze` in sequence.

`gityard analyze`
Builds `.gityard/deletion-analysis.json`.

`gityard status`
Prints the analyzed repositories grouped by deletion safety.

`gityard repos`
Fetches each repository's configured remote, then prints a compact table of local repository git state, including branch, remote, and ahead/behind status.

`gityard pull`
Performs `git pull --ff-only` in each local repository that is behind its upstream and has no local changes. Also reports repositories that are behind but skipped because they are dirty.

`gityard available`
Prints GitHub repositories from `.gityard/github-repos.json` that do not appear in `.gityard/local-repos.json`.

`gityard delete --target.repo <name|owner/repo|path>`
Deletes a local repository if it is classified as safe, or if `--force.delete 1` is present.

`gityard clone --target.repo <owner/repo>`
Clones a repository into the managed root.

## Configuration

`github.user`
GitHub username for `scan-github`. If unset, `gityard` falls back to `git config github.user`, then the local-part of `git config user.email`.

`path.root`
Optional override for the managed root path. If unset, `gityard` resolves `machineroot.get("github-checkouts")`.

`force.delete`
Override flag for deletion safety checks. Must be `"1"` to force deletion.

`target.repo`
Per-invocation selector for `delete` and `clone`.
