# 模块: scripts/watchdog
# 职责: 看护 newsbot 服务进程——主进程消失就拉起，保证无人值守长跑不中断
# 依赖: uv（需在 PATH 中）
#
# 为什么需要它：服务原本跑在编辑器终端里，关掉终端 / 断开远程会话进程就没了；
# 而且没有任何东西负责"挂了再拉起来"。看护脚本把服务启动成独立进程，并周期检查。
#
# 边界：本脚本只负责"进程是否活着"。进程活着但机器人离线的情况由 qqbot 适配器
# 自身的无限重连负责（那里已不存在"超过 N 次就放弃"的分支），这里不重复实现，
# 否则两处状态判断会互相打架。
#
# 用法：
#   pwsh -File scripts/watchdog.ps1              # 常驻看护，每 60s 检查一次
#   pwsh -File scripts/watchdog.ps1 -Once        # 只检查一次（可交给计划任务定时调用）

[CmdletBinding()]
param(
    [int]$IntervalSeconds = 60,
    [string]$RootDir = '',
    [switch]$Once
)

$ErrorActionPreference = 'Stop'

# Windows PowerShell 5.1 在解析 param 默认值时 $PSScriptRoot 还是空的,
# 因此只能在脚本体里取，不能写在 param 块里。
if (-not $RootDir) { $RootDir = Split-Path -Parent $PSScriptRoot }
if (-not $RootDir) { $RootDir = (Get-Location).Path }

$logDir = Join-Path $RootDir 'data\logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
$logFile = Join-Path $logDir 'watchdog.log'
$consoleLog = Join-Path $logDir 'newsbot.console.log'
$consoleErr = Join-Path $logDir 'newsbot.console.err'

function Write-Log([string]$Message) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

function Get-ServiceProcess {
    # 用 -m newsbot run 匹配：命令行的其余部分会随解释器路径变化，不可依赖。
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            ($_.CommandLine -match 'newsbot') -and
            ($_.CommandLine -match '-m\s+newsbot\s+run')
        }
}

function Start-Service {
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        Write-Log 'ERROR 找不到 uv，无法启动服务（请确认 uv 在 PATH 中）'
        return $false
    }
    # 控制台重定向文件不会轮转（app 自己的 data/logs/newsbot.log 会按天轮转）,
    # 长跑时它会被 httpx 之类的 INFO 日志撑大，超过上限就清掉重来。
    # 保留它的意义在于：服务在 setup_logging 生效之前就崩了的话，只有这里有线索。
    foreach ($path in @($consoleLog, $consoleErr)) {
        if ((Test-Path $path) -and (Get-Item $path).Length -gt 64MB) {
            Write-Log ('INFO 控制台日志超过 64MB，清空 {0}' -f $path)
            Remove-Item $path -Force -ErrorAction SilentlyContinue
        }
    }
    # 独立控制台 + Hidden：不依附当前终端，关掉终端/编辑器也不受影响。
    # 标准输出另写一个文件，避免和 app 自己的 newsbot.log 抢同一个句柄。
    Start-Process -FilePath $uv.Source `
        -ArgumentList 'run', 'python', '-m', 'newsbot', 'run' `
        -WorkingDirectory $RootDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $consoleLog `
        -RedirectStandardError $consoleErr
    Write-Log 'INFO 已拉起 newsbot 服务'
    return $true
}

function Invoke-Check {
    $procs = @(Get-ServiceProcess)
    if ($procs.Count -eq 0) {
        Write-Log 'WARN 未发现 newsbot 进程，正在重启'
        Start-Service | Out-Null
        return
    }
    Write-Log ('INFO 进程存活 pid={0}' -f ($procs.ProcessId -join ','))
}

Write-Log ('INFO 看护启动 interval={0}s root={1}' -f $IntervalSeconds, $RootDir)

if ($Once) {
    Invoke-Check
    exit 0
}

while ($true) {
    try {
        Invoke-Check
    } catch {
        Write-Log ('ERROR 检查失败：{0}' -f $_.Exception.Message)
    }
    Start-Sleep -Seconds $IntervalSeconds
}
