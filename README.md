# Repo Sync Control Repository

This repository hosts a GitHub Actions workflow that mirrors GitHub repositories into GitCode and Gitee.

It supports:

- Any public GitHub repository you can reference by `owner/repo`
- Your own GitHub private repositories when `SOURCE_GITHUB_TOKEN` is configured with read access
- Automatic target repository creation on GitCode and Gitee
- Branch and tag synchronization from a bare mirror clone, excluding GitHub-only refs such as `refs/pull/*`
- Optional Git LFS sync per repository
- Optional README link rewriting so same-repo GitHub links point at the target platform
- Release synchronization for the latest `N` GitHub Releases, including metadata and attachments

## Files

- `.github/workflows/repo-sync.yml`: workflow entrypoint
- `config/repos.yaml`: persistent repository inventory
- `scripts/repo_sync.py`: matrix resolution and sync engine

## Required Secrets

- `GITEE_TOKEN`: personal access token with repository create/push/release permissions
- `GITCODE_TOKEN`: personal access token with repository create/push/release permissions
- `SOURCE_GITHUB_TOKEN`: optional, but required for private source repositories and recommended for higher GitHub API limits

`github.token` is still used for public GitHub API access inside Actions, but it does not replace `SOURCE_GITHUB_TOKEN` for private source repos.

## Config Shape

`config/repos.yaml` stores the sync inventory:

```yaml
defaults:
  release_limit: 10
  max_parallel: 3

repos:
  - id: upstream-example
    source:
      full_name: owner/repo
      private: false
    lfs: false
    release_limit: 10
    targets:
      gitee:
        enabled: true
        namespace: your-gitee-namespace
        name: repo
        visibility: public
        rewrite_readme_links: false
        sync_releases: true
      gitcode:
        enabled: true
        namespace: your-gitcode-namespace
        name: repo
        visibility: public
        rewrite_readme_links: false
        sync_releases: true
```

Notes:

- `source.full_name` must be `owner/repo`
- `source.private: true` requires `SOURCE_GITHUB_TOKEN`
- `visibility` is `public` or `private`
- `lfs: true` enables `git lfs fetch --all` and `git lfs push --all`
- `rewrite_readme_links: true` rewrites same-repo GitHub links in the root README after mirror push; this adds one target-only commit on the target default branch
- `release_limit` overrides the default for that repository

## Manual Runs

The workflow supports two modes:

- `manifest`: reads `config/repos.yaml`
- `adhoc`: runs one temporary entry passed as JSON

Example adhoc payload:

```json
{
  "id": "manual-open-source",
  "source": {
    "full_name": "owner/repo",
    "private": false
  },
  "lfs": false,
  "targets": {
    "gitee": {
      "enabled": true,
      "namespace": "your-gitee-namespace",
      "name": "repo",
      "visibility": "public",
      "rewrite_readme_links": false,
      "sync_releases": true
    },
    "gitcode": {
      "enabled": true,
      "namespace": "your-gitcode-namespace",
      "name": "repo",
      "visibility": "public",
      "rewrite_readme_links": false,
      "sync_releases": true
    }
  }
}
```

## Runtime Behavior

For each entry the workflow:

1. Reads GitHub repository metadata and recent Releases
2. Creates the target repositories if they do not exist
3. Syncs all branches and tags to GitCode and Gitee, and deletes stale remote branches/tags
4. Optionally syncs Git LFS objects
5. Optionally rewrites same-repo GitHub links in the root README to target-platform links and pushes a target-only commit
6. Reconciles the latest configured Releases and uploads attachments

Each matrix job writes a per-repository summary into GitHub Actions step summary.

## Current Scope

- Syncs branches, tags, Releases, and optional LFS
- Optionally rewrites only the root README and only for same-repo GitHub repo, clone, blob, tree, and raw links
- Intentionally does not sync GitHub pull-request refs such as `refs/pull/*`
- Does not sync Issues, Pull Requests, Discussions, Wiki, Packages, or Actions artifacts
- Release attachment replacement depends on target platform API behavior; the workflow recreates a Release when supported and otherwise updates metadata and uploads missing assets
- README rewriting intentionally makes the target default branch diverge from the source by one target-only commit when enabled
- GitCode may still report `empty_repo=true` on the repository overview even when branch and content APIs are populated; the workflow now surfaces that mismatch in the job summary
