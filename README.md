# Repo Sync Control Repository

This repository hosts a GitHub Actions workflow that mirrors GitHub repositories into GitCode and Gitee.

It supports:

- Any public GitHub repository you can reference by `owner/repo`
- Your own GitHub private repositories when `SOURCE_GITHUB_TOKEN` is configured with read access
- Automatic target repository creation on GitCode and Gitee
- Full git refs mirroring with `git clone --mirror` and `git push --mirror`
- Optional Git LFS sync per repository
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
        sync_releases: true
      gitcode:
        enabled: true
        namespace: your-gitcode-namespace
        name: repo
        visibility: public
        sync_releases: true
```

Notes:

- `source.full_name` must be `owner/repo`
- `source.private: true` requires `SOURCE_GITHUB_TOKEN`
- `visibility` is `public` or `private`
- `lfs: true` enables `git lfs fetch --all` and `git lfs push --all`
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
      "sync_releases": true
    },
    "gitcode": {
      "enabled": true,
      "namespace": "your-gitcode-namespace",
      "name": "repo",
      "visibility": "public",
      "sync_releases": true
    }
  }
}
```

## Runtime Behavior

For each entry the workflow:

1. Reads GitHub repository metadata and recent Releases
2. Creates the target repositories if they do not exist
3. Mirrors all branches and tags to GitCode and Gitee
4. Optionally syncs Git LFS objects
5. Reconciles the latest configured Releases and uploads attachments

Each matrix job writes a per-repository summary into GitHub Actions step summary.

## Current Scope

- Syncs git refs, Releases, and optional LFS
- Does not sync Issues, Pull Requests, Discussions, Wiki, Packages, or Actions artifacts
- Release attachment replacement depends on target platform API behavior; the workflow recreates a Release when supported and otherwise updates metadata and uploads missing assets
