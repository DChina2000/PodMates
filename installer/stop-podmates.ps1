# 结束 PodMates 的进程（装新版、卸载之前跑）。
# 默认（卸载）：PodMates 本身、它启动的 NapCat 和 NapCat 拉起的 QQ，全部结束。
# -KeepNapCat（装新版）：只结束 PodMates 自己的进程；账号的 NapCat / QQ 一个都不碰，让它们继续挂着，装完 PodMates 重新起来会接回去，QQ 不用重新登录。
# 用户自己开的 QQ（比如 C:\Program Files\Tencent\QQ\QQ.exe）任何时候都不会碰：只认下面几类。
#   1. data\napcat-pids.json 里记下的进程（PodMates 启动 NapCat 时记的 pid 和创建时间，创建时间对得上才算，防止 pid 被别的进程复用）
#   2. 可执行文件在 -Roots 这些文件夹下面的进程（PodMates.exe、它带的东西、装在数据目录里的 NapCat 启动器和一键包里的 QQ）
#   3. QQ.exe：命令行带 NapCat 启动器加的 --enable-logging 和 -q <号>，并且它的父进程已经不在了（一键包的启动器拉起 QQ 本体就退出，QQ 本体成了孤儿）
#   4. 以上所有进程的子孙
# -KeepNapCat 时 1、3 只用来认出「NapCat 的进程」（NapCat 启动器、带 --enable-logging 和 -q 的 QQ、-ProtectDirs 下面的进程，连同它们的子孙）并放过它们，不结束；
# 第 2 类里属于 NapCat 的也放过。
# 本脚本自己和它的祖先进程（安装程序、卸载程序）不会结束。
# -DryRun：只列出会结束哪些，不真的结束。
param(
    [Parameter(Mandatory = $true)][string]$Roots,   # 文件夹，用 | 分隔
    [string]$PidFiles = '',                          # napcat-pids.json 的路径，用 | 分隔
    [string]$ProtectDirs = '',                       # -KeepNapCat 时：这些文件夹下面的进程（数据目录，NapCat 装在里面）一律不碰，用 | 分隔
    [string]$AppDir = '',                            # -KeepNapCat 时：程序目录（{app}）。要保留的进程里有从这里（数据目录除外）加载了模块的，也停掉，免得装新版覆盖不了文件
    [string]$NoteFiles = '',                         # 停掉了「占着程序目录文件的」进程时，把说明写进这些文件里的第一个（数据目录里的 install-notes.txt），PodMates 下次启动读出来记进日志，用 | 分隔
    [switch]$KeepNapCat,
    [switch]$DryRun
)
$ErrorActionPreference = 'SilentlyContinue'

$rootList = @($Roots -split '\|' | Where-Object { $_.Trim() } | ForEach-Object { $_.Trim().TrimEnd('\') + '\' })
$procs = @(Get-CimInstance Win32_Process)
$byId = @{}
foreach ($p in $procs) { $byId[[int]$p.ProcessId] = $p }

# 自己和祖先进程：不结束
$skip = @{}
$cur = [int]$PID
while ($cur -and -not $skip.ContainsKey($cur)) {
    $skip[$cur] = $true
    $cur = if ($byId.ContainsKey($cur)) { [int]$byId[$cur].ParentProcessId } else { 0 }
}

function Test-Under([string]$path) {
    if (-not $path) { return $false }
    foreach ($r in $rootList) {
        if ($path.StartsWith($r, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}

$hit = @{}   # pid -> 原因
$protect = @{}   # -KeepNapCat：NapCat 的进程，不结束
$protectDirList = @($ProtectDirs -split '\|' | Where-Object { $_.Trim() } | ForEach-Object { $_.Trim().TrimEnd('\') + '\' })
function Test-Protected([string]$path) {
    if (-not $path) { return $false }
    foreach ($r in $protectDirList) { if ($path.StartsWith($r, [System.StringComparison]::OrdinalIgnoreCase)) { return $true } }
    return $false
}
# 1. 记下来的 pid
foreach ($f in @($PidFiles -split '\|' | Where-Object { $_.Trim() })) {
    if (-not (Test-Path -LiteralPath $f)) { continue }
    $rec = Get-Content -Raw -LiteralPath $f | ConvertFrom-Json
    foreach ($acct in $rec.PSObject.Properties) {
        foreach ($e in $acct.Value) {
            $id = [int]$e[0]
            if ($byId.ContainsKey($id)) {
                $started = [DateTimeOffset]::new($byId[$id].CreationDate.ToUniversalTime()).ToUnixTimeSeconds()
                if ([math]::Abs($started - [double]$e[1]) -le 3) { if ($KeepNapCat) { $protect[$id] = $true } else { $hit[$id] = '记下的 pid' } }
            }
        }
    }
}
# 2、3
foreach ($p in $procs) {
    $id = [int]$p.ProcessId
    $isNapCat = ($p.Name -eq 'NapCatWinBootMain.exe') -or ($p.Name -eq 'QQ.exe' -and $p.CommandLine -match '--enable-logging' -and $p.CommandLine -match '\s-q\s+\d+')
    if ($KeepNapCat) {
        if ($isNapCat -or (Test-Protected $p.ExecutablePath)) { $protect[$id] = $true }
        elseif (Test-Under $p.ExecutablePath) { $hit[$id] = '在安装 / 数据目录下' }
    }
    elseif (Test-Under $p.ExecutablePath) { $hit[$id] = '在安装 / 数据目录下' }
    elseif ($p.Name -eq 'QQ.exe' -and $p.CommandLine -match '--enable-logging' -and $p.CommandLine -match '\s-q\s+\d+' -and -not $byId.ContainsKey([int]$p.ParentProcessId)) {
        $hit[$id] = 'NapCat 启动的 QQ（启动器已退出）'
    }
}
# 4. 子孙
if ($KeepNapCat) {
    do {
        $added = $false
        foreach ($p in $procs) {
            $id = [int]$p.ProcessId
            if (-not $protect.ContainsKey($id) -and $protect.ContainsKey([int]$p.ParentProcessId)) { $protect[$id] = $true; $added = $true }
        }
    } while ($added)
    foreach ($id in @($hit.Keys)) { if ($protect.ContainsKey($id)) { $hit.Remove($id) } }
}
do {
    $added = $false
    foreach ($p in $procs) {
        $id = [int]$p.ProcessId
        if (-not $hit.ContainsKey($id) -and -not $protect.ContainsKey($id) -and $hit.ContainsKey([int]$p.ParentProcessId)) { $hit[$id] = '子进程'; $added = $true }
    }
} while ($added)

foreach ($id in @($hit.Keys)) { if ($skip.ContainsKey($id)) { $hit.Remove($id) } }

# 5. -KeepNapCat 的保险：要保留的进程里，有从程序目录（数据目录除外）加载了模块的（比如继承了 PodMates 的 DLL 搜索目录，从 _internal 里加载了运行库），
#    装新版覆盖那些文件时会因为被占用而失败（退出码 5），半装状态比重新登录一次糟得多：把它连同同一个号的 NapCat / QQ 进程一起停掉。
$noteLines = @()
if ($KeepNapCat -and $AppDir.Trim()) {
    $appPrefix = $AppDir.Trim().TrimEnd('\') + '\'
    $family = @{}   # pid -> 账号（命令行里 -q 后面的号，或启动器命令行最后的号）；子进程沿用祖先的
    foreach ($id in @($protect.Keys)) {
        $p = $byId[[int]$id]
        $cl = [string]$p.CommandLine
        if ($cl -match '\s-q\s+(\d{5,12})') { $family[$id] = $Matches[1] }
        elseif ($p.Name -eq 'NapCatWinBootMain.exe' -and $cl -match '\s(\d{5,12})"?\s*$') { $family[$id] = $Matches[1] }
    }
    do {
        $added = $false
        foreach ($id in @($protect.Keys)) {
            if ($family.ContainsKey($id)) { continue }
            $par = [int]$byId[[int]$id].ParentProcessId
            if ($family.ContainsKey($par)) { $family[$id] = $family[$par]; $added = $true }
        }
    } while ($added)
    $offender = @{}   # pid -> 占着的文件
    foreach ($id in @($protect.Keys)) {
        if ($skip.ContainsKey($id)) { continue }
        $mods = @((Get-Process -Id $id).Modules | ForEach-Object { $_.FileName })
        foreach ($m in $mods) {
            if ($m -and $m.StartsWith($appPrefix, [System.StringComparison]::OrdinalIgnoreCase) -and -not (Test-Protected $m)) { $offender[$id] = $m; break }
        }
    }
    $stopFamilies = @{}
    foreach ($id in @($offender.Keys)) { if ($family.ContainsKey($id)) { $stopFamilies[$family[$id]] = $true } }
    foreach ($id in @($protect.Keys)) {
        if ($skip.ContainsKey($id)) { continue }
        $why = $null
        if ($offender.ContainsKey($id)) { $why = $offender[$id] }
        elseif ($family.ContainsKey($id) -and $stopFamilies.ContainsKey($family[$id])) { $why = '同一个号的 NapCat 进程占着程序目录的文件' }
        if ($why) {
            $hit[$id] = '因占用程序目录文件而停掉：' + $why
            $p = $byId[[int]$id]
            $noteLines += ('{0} 因占用程序目录文件而停掉 {1}（pid {2}）：{3}。装新版会覆盖这些文件，留着它们会半装失败；这个号的 QQ 需要重新登录。' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $p.Name, $id, $why)
        }
    }
}

foreach ($id in ($hit.Keys | Sort-Object -Descending)) {
    $p = $byId[$id]
    $line = '{0} {1} [{2}] {3}' -f $id, $p.Name, $hit[$id], $p.ExecutablePath
    if ($DryRun) { Write-Output ('会结束 ' + $line) }
    else {
        Stop-Process -Id $id -Force
        Write-Output ('已结束 ' + $line)
    }
}
if (-not $DryRun -and $noteLines.Count) {
    foreach ($f in @($NoteFiles -split '\|' | Where-Object { $_.Trim() })) {
        if (Test-Path -LiteralPath (Split-Path -Parent $f)) { Add-Content -LiteralPath $f -Value $noteLines -Encoding UTF8; break }
    }
}
if (-not $DryRun -and $hit.Count) { Start-Sleep -Milliseconds 800 }
exit 0
