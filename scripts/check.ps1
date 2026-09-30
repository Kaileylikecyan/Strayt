<#
.SYNOPSIS
    一键全量自检（服务端）。

.DESCRIPTION
    按依赖顺序跑完所有检查，任一失败立刻停并返回非零退出码 —— 免得一堆报错糊在
    一屏里，真正的根因被埋在最后面。

    为什么必须是 .ps1 而不是几条命令串起来：本地有四个环境陷阱（见 AGENTS.md §1），
    PowerShell 少任一步都会把"代码没问题"报成"环境有问题"：
      1. npm/ps1 执行策略 —— 这里只用 uv，不碰 npm
      2. GBK 控制台 —— 强制 UTF-8，否则中文报错全花屏
      3. uv 不在 PATH —— 补 APPDATA 的 Scripts 目录
      4. 迁移没跑 —— alembic check 会报出"库落后于模型"

.PARAMETER Fast
    跳过真实 PDF 的端到端验证（var_test/job_e2e.py，约 40 秒）。
    提交前请用完整模式跑。

.PARAMETER SkipTests
    只跑 lint / 格式 / 迁移检查，不跑 pytest。调格式时用。

.EXAMPLE
    .\scripts\check.ps1
    .\scripts\check.ps1 -Fast

    # 若报 PSSecurityException（执行策略禁止 .ps1），二选一：
    #   一次性放行（只影响当前用户，不改系统策略）
    #   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
    #   或每次单次绕过（不改任何策略，推荐在 CI / 别人的机器上用）
    #   powershell -ExecutionPolicy Bypass -File .\scripts\check.ps1

    # 想存日志的话**用文件重定向，不要用 *> 或 2>&1**：PS 5.1 把原生命令的 stderr
    # 合并进管道时，若该命令同时大量写 stdout 和 stderr（alembic 就会）可能死锁。
    #   .\scripts\check.ps1 *> check.log          # 可能卡住，别用
    #   Start-Process powershell -ArgumentList '-File','.\scripts\check.ps1' `
    #       -RedirectStandardOutput check.log -NoNewWindow    # 用这个
#>
[CmdletBinding()]
param(
    [switch]$Fast,
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# --- 环境修复（必须在任何 uv 调用之前）--------------------------------------
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:UV_LINK_MODE = 'copy'  # Windows 上 uv 建软链常失败，报 hardlink 错，copy 最稳
$scriptsDir = Join-Path $env:APPDATA 'Python\Python312\Scripts'
if (Test-Path -LiteralPath $scriptsDir) {
    $env:Path = "$env:Path;$scriptsDir"
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$ServerDir = Join-Path $RepoRoot 'server'
if (-not (Test-Path -LiteralPath (Join-Path $ServerDir 'pyproject.toml'))) {
    Write-Host "找不到 server/pyproject.toml —— 仓库根判断错了：$RepoRoot" -ForegroundColor Red
    exit 2
}

# --- 极简断言层 -------------------------------------------------------------
$script:Failures = [System.Collections.Generic.List[string]]::new()

# Invoke-Step 用 & 调用 scriptblock，会开子作用域：步骤之间共享的变量必须提到
# 脚本级，否则 StrictMode 会报"检索不到变量"。（踩过：$junit 定义在 pytest 那步里。）
$script:JunitPath = Join-Path $ServerDir 'var_test\junit.xml'

function Invoke-Step {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][scriptblock]$Body,
        [switch]$Optional
    )

    Write-Host ''
    Write-Host "=== $Name ===" -ForegroundColor Cyan
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    # 步骤内把 ErrorActionPreference 降回 Continue：alembic / uvicorn 会往 stderr 写
    # INFO 行，PS 5.1 会把它变成 NativeCommandError，配合外层的 Stop 直接把步骤打断
    # ——明明退出码是 0 却报失败。真正的失败信号是 $LASTEXITCODE，下面单独判。
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Body
        $code = $LASTEXITCODE
        if ($code -ne 0) {
            throw "退出码 $code"
        }
        $sw.Stop()
        Write-Host ("--- {0} 通过（{1:N1}s）" -f $Name, $sw.Elapsed.TotalSeconds) -ForegroundColor Green
    }
    catch {
        $sw.Stop()
        $msg = $_.Exception.Message
        if ($Optional) {
            Write-Host ("--- {0} 跳过/不可用：{1}" -f $Name, $msg) -ForegroundColor Yellow
        }
        else {
            Write-Host ("--- {0} 失败（{1:N1}s）" -f $Name, $sw.Elapsed.TotalSeconds) -ForegroundColor Red
            Write-Host $msg -ForegroundColor Red
            $script:Failures.Add($Name)
        }
    }
    finally {
        $ErrorActionPreference = $prevEap
    }
}

# --- 检查项 ----------------------------------------------------------------
Push-Location $ServerDir
try {
    Invoke-Step '工具可用性 (uv / Python)' {
        uv run python -c "import sys; print('python', sys.version.split()[0])"
    }

    Invoke-Step 'Ruff 静态检查' { uv run ruff check . }

    Invoke-Step 'Ruff 格式检查' { uv run ruff format --check . }

    Invoke-Step 'Alembic 迁移与模型一致' {
        # 库落后于模型时会报 pending 迁移；模型比库新会报 "New upgrade operations"
        uv run alembic current
        uv run alembic check
    }

    Invoke-Step 'OpenAPI 契约未过期' {
        # docs/api/openapi.yaml 是 packages/api-client 的类型来源。改了接口忘了重新
        # 导出，客户端就会按旧契约生成类型 —— 错得很难查。
        Push-Location $RepoRoot
        try { uv run --project server python scripts\export_openapi.py --check }
        finally { Pop-Location }
    }

    Invoke-Step '文档相对链接有效' {
        # 文件改名/删除后 .md 里的死链没人会发现，读者点到才知道
        Push-Location $RepoRoot
        try { uv run --project server python scripts\check_doc_links.py }
        finally { Pop-Location }
    }

    if (-not $SkipTests) {
        Invoke-Step 'pytest 全量测试' {
            uv run pytest -q -p no:cacheprovider --junitxml=$script:JunitPath
        }

        # pytest 的退出码只说明"有没有挂"，说不出"挂了几个"。从 JUnit XML 里读真数：
        # 之前一次 0 失败却因为 PowerShell 解析中文类名失败而误判成有问题。
        Invoke-Step '测试计数核对' {
            if (-not (Test-Path -LiteralPath $script:JunitPath)) {
                throw "没生成 $script:JunitPath，读不到测试计数"
            }
            [xml]$xml = Get-Content -LiteralPath $script:JunitPath -Raw -Encoding utf8
            $suite = $xml.testsuites.testsuite
            if (-not $suite) { $suite = $xml.testsuite }
            $t = [int]$suite.tests; $f = [int]$suite.failures; $e = [int]$suite.errors; $s = [int]$suite.skipped
            Write-Host ("    {0} 项：失败 {1} 错误 {2} 跳过 {3}" -f $t, $f, $e, $s)
            if ($f -ne 0 -or $e -ne 0) { throw "有 $f 失败 / $e 错误" }
        }
    }

    if (-not $Fast) {
        # 真实 PDF 跑完整加工链路。样板 PDF 不在仓库里（被 .gitignore 排除），
        # 缺了就跳过而不是失败 —— 新克隆的仓库本来就跑不了。
        Invoke-Step 'E2E：真实 PDF 加工链路' {
            $pdf = Join-Path $ServerDir 'fixtures\daoyouci.pdf'
            if (-not (Test-Path -LiteralPath $pdf)) {
                Write-Host '    样板 PDF 不在，跳过（真实数据验证需要它）' -ForegroundColor Yellow
                return
            }
            uv run python -B var_test\job_e2e.py
        }

        Invoke-Step 'E2E：对齐质量（通道 A / B）' {
            $pdf = Join-Path $ServerDir 'fixtures\daoyouci.pdf'
            if (-not (Test-Path -LiteralPath $pdf)) {
                Write-Host '    样板 PDF 不在，跳过' -ForegroundColor Yellow
                return
            }
            uv run python -B var_test\quality_e2e.py
        }
    }
}
finally {
    Pop-Location
}

# --- 汇总 ------------------------------------------------------------------
Write-Host ''
if ($script:Failures.Count -eq 0) {
    Write-Host '全部通过' -ForegroundColor Green
    exit 0
}
Write-Host ("不通过 {0} 项：" -f $script:Failures.Count) -ForegroundColor Red
$script:Failures | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
exit 1
