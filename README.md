# githubfilter

将一个或多组 Git 仓库的 `main`（或指定分支）完整拉取到临时目录，使用
[`git-filter-repo`](https://github.com/newren/git-filter-repo) 固化人员身份映射，经过一致性校验后同步到内部 Git/CodeHub 仓库。

该仓库只保存同步规则、脚本和流水线，不保存源代码副本、访问令牌或运行时克隆。

## 安全模型

- 源仓库只读，任何重写都发生在一次性 bare clone 中。
- 只改 Author、Committer（若同步标签则还包括 Tagger；当前任务只同步指定分支）。
- `--preserve-commit-hashes` 和 `--preserve-commit-encoding` 避免额外修改提交消息。
- 每个历史提交的 Tree SHA 必须在重写前后相同，提交数和 commit-map 必须完整一致。
- 映射后每个 Author/Committer 必须满足公司邮箱、姓名策略；未知身份直接阻断。
- 上一轮 `old SHA -> new SHA` 映射保存在目标仓独立状态分支。旧映射发生漂移时阻断推送。
- 目标代码分支和状态分支以 `--atomic`、`--force-with-lease` 一起推送，避免半完成状态和并发覆盖。
- Git LFS 对象先于 Git refs 推送，避免目标代码先出现不可下载的 LFS 指针。
- 密码和 Token 只从环境变量读取，通过临时 `GIT_ASKPASS` 使用，不进入 URL、配置、报告和 Git 历史。

> 历史重写后的提交 SHA 与 GitHub 不同，这是预期结果。内部仓的目标分支应当是专用镜像分支，禁止人工提交。

## 目录

```text
config/
  repositories.json          多组源仓/目标仓任务
  identity.mailmap           正式身份映射（当前等待补充）
  identity.mailmap.example   格式示例
scripts/
  sync.py                    克隆、重写、校验、LFS 与原子推送
  collect_identities.py      从源分支生成待映射身份清单
tests/
  test_sync.py               本地仓端到端测试
.github/workflows/
  validate.yml               代码与端到端验证
  sync.yml                   手工/每小时定时同步
requirements-tools.txt       带 SHA-256 的 git-filter-repo 版本锁
```

## 配置多组仓库

`config/repositories.json` 的 `repositories` 数组中每项是一组源仓和目标仓；组内 `branches` 可以声明任意数量的一对一分支映射：

```json
{
  "id": "colink",
  "enabled": true,
  "sourceUrl": "https://github.com/colink-group/CoLink.git",
  "targetUrlEnv": "CODEHUB_COLINK_REPO_URL",
  "mailmap": "identity.mailmap",
  "lfs": "auto",
  "branches": [
    {
      "id": "main",
      "sourceBranch": "main",
      "targetBranch": "main",
      "stateBranch": "githubfilter-state/colink/main"
    },
    {
      "id": "release",
      "sourceBranch": "release/v2",
      "targetBranch": "github-release-v2",
      "stateBranch": "githubfilter-state/colink/release"
    }
  ]
}
```

仓库级字段：

| 字段 | 说明 |
|---|---|
| `id` | 仓库组唯一标识，只允许小写字母、数字、点、下划线和连字符 |
| `enabled` | 仓库组总开关；关闭后不进入定时矩阵，仍可手工指定 |
| `sourceUrl` / `sourceUrlEnv` | 二选一，源仓地址或保存地址的环境变量名 |
| `targetUrl` / `targetUrlEnv` | 二选一，目标仓地址或保存地址的环境变量名 |
| `mailmap` | 相对 `repositories.json` 所在目录或绝对路径；不同任务可复用或独立配置 |
| `lfs` | `auto`、`true`、`false`；`auto` 只在检测到 LFS 对象时同步 |
| `requireAtomicPush` | 默认 `true`；目标 Git 服务不支持原子推送时才可在风险评估后关闭 |
| `identityPolicy` | 映射后邮箱必需正则，以及可选的姓名必需正则 |
| `sourceAuth` / `targetAuth` | 指定用户名和令牌所处环境变量的名称，不是凭据值 |
| `branches` | 非空数组；每项是一组源分支到目标分支的映射 |

分支映射字段：

| 字段 | 说明 |
|---|---|
| `id` | 组内唯一标识；最终任务 ID 为 `仓库ID.分支ID`，例如 `colink.main` |
| `enabled` | 可选的分支开关，默认开启；仓库总开关关闭时仍不会进入定时矩阵 |
| `sourceBranch` | 原仓分支短名称 |
| `targetBranch` | 目标仓分支短名称，可以与源分支不同 |
| `stateBranch` | 可选的审计分支；默认 `githubfilter-state/<仓库ID>/<分支ID>` |

一个源分支需要写入多个目标分支时，声明多个不同 `id`、相同 `sourceBranch` 的映射即可。一个仓库组内的所有映射共享源/目标仓地址；要同步到另一个目标仓，应再增加一个仓库组。

### `stateBranch` 是什么

`stateBranch` 只存在于目标仓，是 githubfilter 的机器审计分支，不是业务代码分支，也不会从原仓拉取。它保存该分支映射的 `commit-map.tsv`、`metadata.json` 和 `identities.tsv`，用于确认：

- 相同源提交在后续运行中仍映射到相同目标 SHA；
- 目标代码分支没有被人工改写；
- mailmap 或工具版本变化没有静默重写既有历史。

每个“源分支 → 目标分支”映射必须使用独立状态分支。代码分支与状态分支通过一次原子推送共同更新。

顶层 `defaults`、仓库组和分支项按层级深度合并，所以公共 mailmap、认证变量和策略只需写一次，单个分支仍可覆盖。

### 同一组凭据管理多个仓

默认流水线使用以下 GitHub Secrets：

- `SOURCE_GIT_USERNAME`
- `SOURCE_GIT_TOKEN`：建议 GitHub fine-grained token，只授予源仓 Contents 只读权限
- `TARGET_GIT_USERNAME`
- `TARGET_GIT_TOKEN`：只授予指定 CodeHub 目标仓写权限
- `CODEHUB_COLINK_REPO_URL`：当前 CoLink 任务的目标 URL

需要多套账号时，在任务的 `sourceAuth` / `targetAuth` 中声明另一组环境变量名，并在组织的 self-hosted runner 上通过凭据管理系统注入。不要把 Secret 值写进 JSON。

## 作者映射

`git-filter-repo --mailmap` 使用 Git mailmap 语法：

```text
规范姓名（工号） <公司邮箱> GitHub 显示名 <GitHub 提交邮箱>
```

同一人有多个旧邮箱时写多行：

```text
张三（E12345） <zhangsan@corp.example.com> Zhang San <12345+zhangsan@users.noreply.github.com>
张三（E12345） <zhangsan@corp.example.com> Zhang San <zhangsan@example.net>
```

机器人必须映射到专用机器人身份；外部贡献者不得伪装成内部员工。若业务要求保留外部贡献者，应为其定义明确的规范身份和独立允许策略。

修改已经使用过的旧身份映射会改变该提交及所有后代的内部 SHA。系统会把这识别为“历史提交映射漂移”并阻断。需要更正历史映射时，应走一次人工审批的全量历史重建，而不是直接放开定时任务。

先生成待映射清单（包含 Author/Committer 角色和出现次数）：

```bash
python scripts/collect_identities.py \
  --source-url https://github.com/colink-group/CoLink.git \
  --branch main \
  --output reports/colink-identities.tsv
```

私有源仓使用与同步任务相同的 `SOURCE_GIT_USERNAME`、`SOURCE_GIT_TOKEN`。清单本身不包含令牌，但姓名、邮箱可能属于内部数据，传递和保存时应按公司数据规范处理，不要提交到本公共控制仓。

## 执行流程

```text
读取一组任务
  -> 全新 bare clone 源分支
  -> 查询目标代码/状态分支
  -> 源 SHA 未变化则校验目标未漂移并退出
  -> 枚举 Author/Committer，应用 mailmap，未知身份阻断
  -> git-filter-repo 写入独立 bare repo
  -> 校验所有提交 Tree SHA、提交数、身份和历史 commit-map
  -> 需要时先复制 LFS 对象
  -> 原子 + force-with-lease 推送代码分支和状态分支
```

状态分支包含：

- `commit-map.tsv`：源提交 SHA 与内部提交 SHA 的稳定映射
- `metadata.json`：源/目标 tip、工具版本、mailmap 摘要、提交数和运行时间
- `identities.tsv`：本轮发现的原身份与规范身份（不含凭据）

## 本地验证

要求 Python 3.11+、Git、可选 Git LFS：

Windows 推荐流程：

1. 编辑 `config/repositories.json`，配置仓库组和分支映射。
2. 编辑 `config/identity.mailmap`，配置提交人映射。
3. 复制 `config/local.env.example` 为 `config/local.env` 并填写访问凭据；该文件不会被 Git 跟踪。
4. 先执行不会推送的完整演练：

```powershell
.\scripts\local-test.ps1 -TaskId colink.main
```

也可以一次测试某个仓库组的全部分支：

```powershell
.\scripts\local-test.ps1 -RepositoryId colink
```

核对 `reports/<任务ID>/` 中的 `identities.tsv`、`metadata.json`、`commit-map.tsv` 后，再显式执行真实推送：

```powershell
.\scripts\local-test.ps1 -TaskId colink.main -Mode push
```

若目标代码分支已经存在但尚无状态分支，工具默认拒绝接管；只有确认该目标分支可以被镜像历史覆盖后，首次执行才增加 `-AllowBootstrapOverExistingTarget`。

Linux/macOS 或需要直接调用 Python 时：

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-tools.txt
python -m unittest discover -s tests -v
python scripts/sync.py --config config/repositories.json --validate-config
```

指定整个仓库组的全部分支进行不推送演练：

```bash
python scripts/sync.py \
  --config config/repositories.json \
  --repository-id colink \
  --dry-run \
  --report-dir reports
```

只执行一个分支映射：

```bash
python scripts/sync.py \
  --config config/repositories.json \
  --task-id colink.main \
  --dry-run \
  --report-dir reports
```

首次目标代码分支已存在但尚无状态分支时，工具默认拒绝接管。只有已人工核对目标分支用途后才可执行一次：

```bash
python scripts/sync.py ... --allow-bootstrap-over-existing-target
```

## 定时流水线启用步骤

1. 补充 `identity.mailmap` 和真实公司身份正则。
2. 添加 CodeHub 目标仓并设置目标分支为专用镜像分支。
3. 在能同时访问 GitHub 与内部 CodeHub 的 Linux self-hosted runner 上安装 Git、Git LFS、Python；Actions Runner 至少为 `v2.327.1`（工作流使用 Node.js 24 action）。
4. 配置上述 Secrets；不要将 CodeHub 暴露给公网来迁就 GitHub 托管 runner。
5. 先从 Actions 手工执行 `dry_run=true`，下载审计报告核对身份清单。
6. 再手工执行一次真实同步，并验证目标分支和状态分支。
7. 把仓库变量 `SYNC_ENABLED` 设为 `true`，开启每小时第 17 分钟的定时同步。

未配置 `SYNC_ENABLED=true` 时，定时事件会被安全跳过；手工工作流仍可运行。

## 有意不做的事情

- 不重写 `Signed-off-by`、`Co-authored-by` 等提交消息 trailer；mailmap 不处理这些内容。
- 不同步其他分支和标签；每一条要同步的分支应定义为独立任务，以获得独立状态和并发锁。
- 不双向同步；GitHub 源分支是唯一事实源。
- 不允许内部开发者直接向镜像分支提交。
- 不在成功后保留临时 clone；可审计结果由目标状态分支和流水线 Artifact 保存。

## 参考

- [git-filter-repo 项目](https://github.com/newren/git-filter-repo)
- [git-filter-repo 用户手册](https://github.com/newren/git-filter-repo/blob/main/Documentation/git-filter-repo.txt)
- [git-filter-repo FAQ](https://github.com/newren/git-filter-repo/blob/main/Documentation/FAQ.md)
- [Git mailmap 文档](https://git-scm.com/docs/gitmailmap)
