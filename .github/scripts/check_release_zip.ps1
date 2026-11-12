#Requires -Version 7.0
<#
检查发布用的 Windows zip，windows.yml 的"检查发布用的 zip"和"发布到 GitHub Release"两个作业在取回产物之后都运行它：

  ./.github/scripts/check_release_zip.ps1 -Directory release-assets -ExpectedName bustag-<版本>-windows-x64.zip

目录里（递归查找）必须恰好有 1 个 zip，文件名是 ExpectedName，它是单层的：解压后顶层只有 bustag/，里面有 bustag.exe 和 LICENSE。
不符合时用中文报错（先列出目录里的全部文件）；通过时把这个 zip 的相对路径写进 GITHUB_OUTPUT 的 path，发布作业把它附到 Release
#>
param(
    [string]$Directory = 'release-assets',
    [Parameter(Mandatory = $true)][string]$ExpectedName
)
$ErrorActionPreference = 'Stop'

function Get-RelativePath([string]$Path) {
    [IO.Path]::GetRelativePath((Get-Location).Path, $Path) -replace '\\', '/'
}

if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
    throw "没有 $Directory 目录：取回产物这一步没有得到任何文件"
}
$files = @(Get-ChildItem -LiteralPath $Directory -Recurse -Force -File)
"--- $Directory 里的文件（$($files.Count) 个）---"
foreach ($file in $files) { '{0,12}  {1}' -f $file.Length, (Get-RelativePath $file.FullName) }

$zips = @($files | Where-Object { $_.Extension -ieq '.zip' })
if ($zips.Count -eq 0 -and $files.Count -eq 1) {
    # 产物只有一个文件；万一取回时没拿到原文件名，就是这一个
    $renamed = Join-Path $files[0].DirectoryName $ExpectedName
    "::warning::取回的文件名是 $($files[0].Name)，改名为 $ExpectedName"
    Move-Item -LiteralPath $files[0].FullName -Destination $renamed
    $zips = @(Get-Item -LiteralPath $renamed)
}
if ($zips.Count -ne 1) {
    $found = if ($zips.Count) { ($zips | ForEach-Object { Get-RelativePath $_.FullName }) -join '，' } else { '一个也没有' }
    throw ("$Directory 里应该恰好有 1 个 zip，实际找到 $($zips.Count) 个：$found。" +
           'zip 被当成归档解开成了目录、或者取回了多余的文件时会这样，见上面的文件列表')
}
$zip = $zips[0]
if ($zip.Name -ne $ExpectedName) {
    throw "$Directory 里的 zip 是 $($zip.Name)，应该是 $ExpectedName"
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
try {
    $archive = [IO.Compression.ZipFile]::OpenRead($zip.FullName)
} catch {
    throw "$($zip.Name) 不是有效的 zip：$($_.Exception.Message)"
}
try {
    # 不同版本的 Compress-Archive 写出的路径分隔符不同，比较前统一成 /
    $entries = @($archive.Entries | ForEach-Object { $_.FullName -replace '\\', '/' })
} finally {
    $archive.Dispose()
}
$outside = @($entries | Where-Object { -not $_.StartsWith('bustag/') })
if ($outside.Count) {
    throw "$($zip.Name) 解压后的顶层应该只有 bustag/，还有：$($outside[0..4] -join '，')"
}
foreach ($name in 'bustag/bustag.exe', 'bustag/LICENSE') {
    if ($entries -notcontains $name) { throw "$($zip.Name) 里没有 $name" }
}

$path = Get-RelativePath $zip.FullName
'{0}：{1} 个条目，{2:N1} MB，顶层是 bustag/，有 bustag.exe 和 LICENSE' -f $path, $entries.Count, ($zip.Length / 1MB)
if ($env:GITHUB_OUTPUT) {
    "path=$path" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
}
