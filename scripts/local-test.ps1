[CmdletBinding()]
param(
    [string]$TaskId,
    [string]$RepositoryId,
    [ValidateSet("dry-run", "push")]
    [string]$Mode = "dry-run",
    [string]$Config = "config/repositories.json",
    [string]$EnvFile = "config/local.env",
    [switch]$AllowBootstrapOverExistingTarget
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ([string]::IsNullOrWhiteSpace($TaskId) -eq [string]::IsNullOrWhiteSpace($RepositoryId)) {
    throw "TaskId 和 RepositoryId 必须且只能指定一个。"
}

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$configPath = if ([System.IO.Path]::IsPathRooted($Config)) {
    $Config
} else {
    Join-Path $repositoryRoot $Config
}
$envPath = if ([System.IO.Path]::IsPathRooted($EnvFile)) {
    $EnvFile
} else {
    Join-Path $repositoryRoot $EnvFile
}

if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "配置文件不存在：$configPath"
}
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    throw "本地凭据文件不存在：$envPath。请复制 config/local.env.example 为 config/local.env。"
}

foreach ($line in Get-Content -LiteralPath $envPath -Encoding UTF8) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith("#")) {
        continue
    }
    $separator = $trimmed.IndexOf("=")
    if ($separator -lt 1) {
        throw "local.env 存在无效行（必须是 NAME=VALUE）：$line"
    }
    $name = $trimmed.Substring(0, $separator).Trim()
    $value = $trimmed.Substring($separator + 1).Trim()
    if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
        throw "local.env 环境变量名无效：$name"
    }
    if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
        ($value.StartsWith("'") -and $value.EndsWith("'"))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    if (-not $value -or $value.StartsWith("REPLACE_WITH_") -or $value.Contains("example.com")) {
        throw "local.env 的 $name 仍为空或包含示例占位值。"
    }
    [Environment]::SetEnvironmentVariable($name, $value, "Process")
}

$venvPython = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Write-Host "创建本地 Python 虚拟环境..."
    & python -m venv (Join-Path $repositoryRoot ".venv")
    if ($LASTEXITCODE -ne 0) {
        throw "创建 Python 虚拟环境失败。"
    }
}

Write-Host "安装/核对固定版本的 git-filter-repo..."
& $venvPython -m pip install --disable-pip-version-check --require-hashes `
    -r (Join-Path $repositoryRoot "requirements-tools.txt")
if ($LASTEXITCODE -ne 0) {
    throw "安装 git-filter-repo 失败。"
}

$arguments = @(
    (Join-Path $repositoryRoot "scripts\sync.py"),
    "--config", $configPath,
    "--include-disabled",
    "--report-dir", (Join-Path $repositoryRoot "reports")
)
if ($TaskId) {
    $arguments += @("--task-id", $TaskId)
} else {
    $arguments += @("--repository-id", $RepositoryId)
}
if ($Mode -eq "dry-run") {
    $arguments += "--dry-run"
} else {
    Write-Warning "push 模式会更新目标代码分支和状态分支。"
}
if ($AllowBootstrapOverExistingTarget) {
    $arguments += "--allow-bootstrap-over-existing-target"
}

Write-Host "执行 $Mode，本次审计报告将写入 reports/。"
& $venvPython @arguments
if ($LASTEXITCODE -ne 0) {
    throw "githubfilter 本地测试失败，退出码：$LASTEXITCODE"
}

Write-Host "githubfilter 本地测试成功。"
