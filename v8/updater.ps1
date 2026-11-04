# ReqDoc Studio V8 only. No legacy migration and no access to user workspace files.
param(
  [Parameter(Mandatory=$true)][string]$InstallDir,
  [Parameter(Mandatory=$true)][Alias('PackagePath')][string]$Package,
  [Parameter(Mandatory=$true)][int]$AppPid,
  [Parameter(Mandatory=$true)][string]$Sha256
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$root = Join-Path $env:LOCALAPPDATA 'ReqDocStudio'
$logDir = Join-Path $root 'logs'
$backupRoot = Join-Path $root 'program-backups'
New-Item -ItemType Directory -Path $logDir,$backupRoot -Force | Out-Null
$log = Join-Path $logDir ('update-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'.log')
function WriteLog([string]$message) { (Get-Date -Format 's')+' '+$message | Out-File -LiteralPath $log -Encoding UTF8 -Append }
$expected = @('ReqDocStudio.exe','updater.ps1','version.json')
$work = Join-Path $env:TEMP ('reqdoc-v8-stage-'+[guid]::NewGuid().ToString('N'))
$stage = Join-Path $work 'stage'
$backup = Join-Path $backupRoot (Get-Date -Format 'yyyyMMdd-HHmmss-fff')
$installed = New-Object 'System.Collections.Generic.List[string]'
$canRollback = $false
try {
  $InstallDir=[IO.Path]::GetFullPath($InstallDir)
  $Package=[IO.Path]::GetFullPath($Package)
  if (-not (Test-Path -LiteralPath (Join-Path $InstallDir 'ReqDocStudio.exe') -PathType Leaf)) {
    throw 'Not a V8 installation directory'
  }
  if (-not (Test-Path -LiteralPath $Package -PathType Leaf)) { throw 'Update package missing' }
  $actual = (Get-FileHash -LiteralPath $Package -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -ne $Sha256.ToLowerInvariant()) { throw 'ZIP SHA256 mismatch' }

  # Wait for the old application to close without terminating it.
  for ($i=0; $i -lt 120; $i++) {
    if (-not (Get-Process -Id $AppPid -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 500
  }
  if (Get-Process -Id $AppPid -ErrorAction SilentlyContinue) { throw 'Old app is still running' }

  New-Item -ItemType Directory -Path $stage,$backup -Force | Out-Null
  $archive = [IO.Compression.ZipFile]::OpenRead($Package)
  try {
    if ($archive.Entries.Count -ne $expected.Count) { throw 'Unexpected ZIP file count' }
    $seen = @{}
    foreach ($item in $archive.Entries) {
      $entryName = $item.FullName
      if ($expected -cnotcontains $entryName -or $seen.ContainsKey($entryName)) {
        throw ('Unexpected ZIP entry: '+$entryName)
      }
      if ($item.Length -gt 300MB) { throw 'Update item too large' }
      $seen[$entryName]=$true
    }
    foreach ($name in $expected) { if (-not $seen.ContainsKey($name)) { throw ('Missing update item '+$name) } }
    foreach ($item in $archive.Entries) {
      [IO.Compression.ZipFileExtensions]::ExtractToFile($item,(Join-Path $stage $item.FullName),$false)
    }
  } finally { $archive.Dispose() }
  $meta = Get-Content -LiteralPath (Join-Path $stage 'version.json') -Raw | ConvertFrom-Json
  if ($meta.name -cne 'ReqDocStudio' -or $meta.version -notmatch '^8\.\d+\.\d+$') {
    throw 'Invalid V8 product or version metadata'
  }
  foreach ($name in $expected) {
    $target = Join-Path $InstallDir $name
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw ('Existing target missing: '+$name) }
    $info = Get-Item -LiteralPath $target -Force
    if (($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Link target denied' }
    Copy-Item -LiteralPath $target -Destination (Join-Path $backup $name) -Force
  }
  $canRollback=$true
  foreach ($name in $expected) {
    Copy-Item -LiteralPath (Join-Path $stage $name) -Destination (Join-Path $InstallDir $name) -Force
    $installed.Add($name)
  }
  $app = Start-Process -FilePath (Join-Path $InstallDir 'ReqDocStudio.exe') -WorkingDirectory $InstallDir -PassThru
  Start-Sleep -Seconds 4
  if ($app.HasExited) { throw 'New version exited immediately' }
  WriteLog ('Installed '+$meta.version+'; program rollback backup='+$backup)
} catch {
  WriteLog ('ERROR: '+$_.Exception.Message)
  if ($canRollback) {
    foreach ($name in $expected) {
      $previous = Join-Path $backup $name
      if (Test-Path -LiteralPath $previous -PathType Leaf) {
        try { Copy-Item -LiteralPath $previous -Destination (Join-Path $InstallDir $name) -Force }
        catch { WriteLog ('ROLLBACK FAILURE: '+$name) }
      }
    }
    WriteLog 'Rollback attempted; old program files preserved in backup directory'
  }
  try {
    if (-not (Get-Process -Name 'ReqDocStudio' -ErrorAction SilentlyContinue)) {
      Start-Process -FilePath (Join-Path $InstallDir 'ReqDocStudio.exe') -WorkingDirectory $InstallDir
    }
  } catch { WriteLog ('Restart failure: '+$_.Exception.Message) }
  exit 1
} finally {
  if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
}
