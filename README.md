# Repo Sync 控制仓库

这个仓库承载了一套 GitHub Actions 工作流，用于把 GitHub 仓库自动镜像到 GitCode 和 Gitee。

它支持：

- 任何可以用 `owner/repo` 表示的公开 GitHub 仓库
- 配置了 `SOURCE_GITHUB_TOKEN` 且具备读取权限时，你自己的 GitHub 私有仓库
- 自动创建 GitCode 和 Gitee 目标仓库
- 基于 bare mirror 的分支与标签同步，并排除 GitHub 专有引用，例如 `refs/pull/*`
- 按仓库选择是否同步 Git LFS
- 可选的 README 链接重写，把“同仓库 GitHub 链接”改写为目标平台链接
- 同步最新 `N` 个 GitHub Releases，包括元数据和附件

## 文件说明

- `.github/workflows/repo-sync.yml`：工作流入口
- `config/repos.yaml`：持久化的仓库清单
- `scripts/repo_sync.py`：矩阵解析与同步引擎

## 必需 Secrets

- `GITEE_TOKEN`：具备建仓、推送、发布权限的个人访问令牌
- `GITCODE_TOKEN`：具备建仓、推送、发布权限的个人访问令牌
- `SOURCE_GITHUB_TOKEN`：可选；同步私有源仓库时必需，也建议配置以提升 GitHub API 配额

Actions 里的 `github.token` 仍然会用于访问公开 GitHub API，但它不能替代私有源仓库所需的 `SOURCE_GITHUB_TOKEN`。

## 配置结构

`config/repos.yaml` 用来保存同步清单：

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

说明：

- `source.full_name` 必须是 `owner/repo`
- `source.private: true` 需要提供 `SOURCE_GITHUB_TOKEN`
- `visibility` 只能是 `public` 或 `private`
- `lfs: true` 会启用 `git lfs fetch --all` 和 `git lfs push --all`
- `rewrite_readme_links: true` 会在 mirror push 结束后重写根目录 README 里的同仓库 GitHub 链接；这会让目标平台默认分支额外多出一个“仅目标端存在”的提交
- `release_limit` 会覆盖该仓库的默认 release 同步数量

## 手动运行

工作流支持两种模式：

- `manifest`：读取 `config/repos.yaml`
- `adhoc`：传入一份临时 JSON 配置，仅执行一次

`adhoc` 模式示例：

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

## 运行行为

对每个清单项，工作流会：

1. 读取 GitHub 仓库元数据和近期 Releases
2. 如果目标仓库不存在，则自动创建
3. 将全部分支和标签同步到 GitCode 与 Gitee，并删除目标端已过期的远端分支与标签
4. 按配置选择是否同步 Git LFS 对象
5. 按配置选择是否重写根目录 README 中的同仓库 GitHub 链接，并推送一个仅存在于目标端的提交
6. 对齐最近配置数量内的 Releases，并上传附件

每个矩阵任务都会把该仓库的执行摘要写入 GitHub Actions 的 step summary。

## 当前范围

- 支持同步分支、标签、Releases，以及可选的 LFS
- 可选重写根目录 README，且只处理同仓库 GitHub 的 repo、clone、blob、tree、raw 链接
- 有意不处理 GitHub 的 pull request refs，例如 `refs/pull/*`
- 不同步 Issues、Pull Requests、Discussions、Wiki、Packages、Actions 产物
- Release 附件替换能力取决于目标平台 API；当前策略是在支持时重建 Release，否则更新元数据并补传缺失附件
- 开启 README 重写后，目标平台默认分支会有一个额外的目标端专属提交，这是预期行为
- GitCode 有时会在分支和内容 API 已正常的情况下，仓库概览页仍显示 `empty_repo=true`；当前工作流会把这个状态差异写进 job summary
