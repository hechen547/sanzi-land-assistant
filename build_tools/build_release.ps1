$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$BuildRoot = Join-Path $ProjectRoot "build\nuitka"
$DistDir = Join-Path $BuildRoot "三资辅助软件.dist"
$ReleaseDir = Join-Path $ProjectRoot "release"
$PortableDir = Join-Path $ReleaseDir "三资辅助软件_1.2_免安装版"
$PortableZip = Join-Path $ReleaseDir "三资辅助软件_1.2_免安装版.zip"
$ReleaseNotes = Join-Path $ReleaseDir "v1.2-release-notes.md"
$InnoCompiler = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"

function Copy-QtRuntimeDependencies {
    param(
        [Parameter(Mandatory = $true)]
        [string]$DistributionDirectory
    )

    $NuitkaCache = Join-Path $env:LOCALAPPDATA "Nuitka\Nuitka\Cache\downloads\gcc"
    $Objdump = Get-ChildItem $NuitkaCache -Recurse -Filter "objdump.exe" |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $Objdump) {
        throw "未找到 Nuitka MinGW objdump，无法检查 Qt 运行库依赖。"
    }

    $PySideSource = Join-Path $ProjectRoot ".venv\Lib\site-packages\PySide6"
    $ShibokenSource = Join-Path $ProjectRoot ".venv\Lib\site-packages\shiboken6"
    $SourceDlls = @{}
    Get-ChildItem $PySideSource, $ShibokenSource -File -Filter "*.dll" |
        ForEach-Object { $SourceDlls[$_.Name.ToLowerInvariant()] = $_.FullName }

    $Queue = [System.Collections.Generic.Queue[string]]::new()
    $Visited = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    Get-ChildItem $DistributionDirectory -Recurse -File |
        Where-Object { $_.Extension -in ".exe", ".dll", ".pyd" } |
        ForEach-Object { $Queue.Enqueue($_.FullName) }

    while ($Queue.Count -gt 0) {
        $Binary = $Queue.Dequeue()
        if (-not $Visited.Add($Binary)) {
            continue
        }
        $BinaryForTool = [System.IO.Path]::GetRelativePath($ProjectRoot, $Binary)
        $Dependencies = & $Objdump -p $BinaryForTool 2>$null |
            Select-String "DLL Name:\s+(.+)$" |
            ForEach-Object { $_.Matches[0].Groups[1].Value.Trim() }
        foreach ($Dependency in $Dependencies) {
            $Key = $Dependency.ToLowerInvariant()
            if (-not $SourceDlls.ContainsKey($Key)) {
                continue
            }
            $Source = $SourceDlls[$Key]
            $DestinationFolder = if ($Source.StartsWith(
                $ShibokenSource,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
                Join-Path $DistributionDirectory "shiboken6"
            } else {
                Join-Path $DistributionDirectory "PySide6"
            }
            New-Item -ItemType Directory -Force -Path $DestinationFolder | Out-Null
            $Destination = Join-Path $DestinationFolder $Dependency
            if (-not (Test-Path $Destination)) {
                Copy-Item -LiteralPath $Source -Destination $Destination
                $Queue.Enqueue($Destination)
            }
        }
    }

    foreach ($OptionalDll in "opengl32sw.dll", "avcodec-61.dll", "avformat-61.dll",
        "avutil-59.dll", "swresample-5.dll", "swscale-8.dll") {
        $Source = Join-Path $PySideSource $OptionalDll
        if (Test-Path $Source) {
            Copy-Item -LiteralPath $Source -Destination (
                Join-Path $DistributionDirectory "PySide6\$OptionalDll"
            ) -Force
        }
    }
}

if (-not (Test-Path $Python)) {
    throw "项目虚拟环境不存在：$Python"
}
if (-not (Test-Path $InnoCompiler)) {
    throw "未找到 Inno Setup：$InnoCompiler"
}

New-Item -ItemType Directory -Force -Path $BuildRoot, $ReleaseDir | Out-Null
if (Test-Path $DistDir) {
    Remove-Item -LiteralPath $DistDir -Recurse -Force
}
if (Test-Path $PortableDir) {
    Remove-Item -LiteralPath $PortableDir -Recurse -Force
}
if (Test-Path $PortableZip) {
    Remove-Item -LiteralPath $PortableZip -Force
}

$env:PYTHONUTF8 = "1"
$NuitkaArgs = @(
    "-m", "nuitka",
    "--standalone",
    "--assume-yes-for-downloads",
    "--mingw64",
    "--enable-plugin=pyside6",
    "--include-package=sanzi_photo_tool",
    "--include-package-data=sanzi_photo_tool",
    "--include-data-files=$ProjectRoot\src\sanzi_photo_tool\ui\style.qss=sanzi_photo_tool/ui/style.qss",
    "--include-data-files=$ProjectRoot\src\sanzi_photo_tool\resources\visible_land_export.js=sanzi_photo_tool/resources/visible_land_export.js",
    "--include-data-files=$ProjectRoot\src\sanzi_photo_tool\resources\app-icon.ico=sanzi_photo_tool/resources/app-icon.ico",
    "--include-data-files=$ProjectRoot\src\sanzi_photo_tool\resources\app-icon.png=sanzi_photo_tool/resources/app-icon.png",
    "--include-data-files=$ProjectRoot\index.html=index.html",
    "--include-data-files=$ProjectRoot\gps_map.html=gps_map.html",
    "--windows-console-mode=disable",
    "--windows-icon-from-ico=$ProjectRoot\src\sanzi_photo_tool\resources\app-icon.ico",
    "--company-name=三资辅助软件",
    "--product-name=三资辅助软件",
    "--file-description=三资图斑、照片与无人机航线辅助工具",
    "--file-version=1.2.0.0",
    "--product-version=1.2.0.0",
    "--copyright=三资辅助软件",
    "--output-filename=三资辅助软件.exe",
    "--output-dir=$BuildRoot",
    "--remove-output",
    "$PSScriptRoot\app_entry.py"
)

Write-Host "正在使用 Nuitka 编译三资辅助软件 1.2..."
& $Python @NuitkaArgs
if ($LASTEXITCODE -ne 0) {
    throw "Nuitka 编译失败，退出代码：$LASTEXITCODE"
}

$ActualDist = Join-Path $BuildRoot "app_entry.dist"
if (-not (Test-Path $ActualDist)) {
    throw "未找到 Nuitka 输出目录：$ActualDist"
}
Write-Host "正在补齐 Qt 和 Shiboken 运行库..."
Copy-QtRuntimeDependencies -DistributionDirectory $ActualDist
Move-Item -LiteralPath $ActualDist -Destination $DistDir

Write-Host "正在制作免安装版..."
Copy-Item -LiteralPath $DistDir -Destination $PortableDir -Recurse
Compress-Archive -Path (Join-Path $PortableDir "*") -DestinationPath $PortableZip -CompressionLevel Optimal

Write-Host "正在制作标准安装包..."
& $InnoCompiler (Join-Path $PSScriptRoot "installer.iss")
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup 编译失败，退出代码：$LASTEXITCODE"
}

$HashFile = Join-Path $ReleaseDir "三资辅助软件_1.2_SHA256.txt"
$ReleaseFiles = @(
    Get-Item -LiteralPath $PortableZip
    Get-Item -LiteralPath (Join-Path $ReleaseDir "三资辅助软件_1.2_安装程序.exe")
)
$HashLines = foreach ($File in $ReleaseFiles) {
    $Hash = (Get-FileHash -LiteralPath $File.FullName -Algorithm SHA256).Hash
    "$Hash  $($File.Name)"
}
$HashLines | Set-Content -LiteralPath $HashFile -Encoding UTF8
Copy-Item -LiteralPath (Join-Path $ProjectRoot "1.2发布说明.md") `
    -Destination $ReleaseNotes -Force

Write-Host "发布文件已生成："
Get-ChildItem -LiteralPath $ReleaseDir -File | Select-Object Name, Length, LastWriteTime
