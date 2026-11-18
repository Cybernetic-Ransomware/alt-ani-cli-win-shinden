# Generates apps/android's Gradle wrapper without an installed `gradle`, from a hash-checked download; needs JAVA_HOME.
param(
    [string]$Version = "9.9.1",
    [string]$Sha256 = "d8a18a7a841dd5df825d35a685fad6f2d7f12ac0c280e57b67c27b4a520ba228"
)
$ErrorActionPreference = "Stop"

if (-not $env:JAVA_HOME) {
    throw "Set JAVA_HOME first, e.g. to Android Studio's bundled JBR."
}
$project = Join-Path $PSScriptRoot "..\..\apps\android" | Resolve-Path
$work = Join-Path ([System.IO.Path]::GetTempPath()) "alt-ani-gradle-$Version-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $work | Out-Null
try {
    $zip = Join-Path $work "gradle-$Version-bin.zip"
    Invoke-WebRequest -Uri "https://services.gradle.org/distributions/gradle-$Version-bin.zip" -OutFile $zip
    $actual = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLowerInvariant()
    if ($actual -ne $Sha256.ToLowerInvariant()) {
        throw "SHA-256 mismatch for gradle-$Version-bin.zip: expected $Sha256, got $actual"
    }
    Expand-Archive -Path $zip -DestinationPath $work
    $gradle = Join-Path $work "gradle-$Version\bin\gradle.bat"
    # an empty build, so the wrapper does not depend on the app's build configuring
    $stub = Join-Path $work "stub"
    New-Item -ItemType Directory -Path $stub | Out-Null
    Set-Content -Path (Join-Path $stub "settings.gradle.kts") -Value 'rootProject.name = "wrapper-stub"'
    Push-Location $stub
    try {
        & $gradle wrapper --gradle-version $Version --distribution-type bin --gradle-distribution-sha256-sum $Sha256 --no-daemon
        if ($LASTEXITCODE -ne 0) { throw "gradle wrapper failed with exit code $LASTEXITCODE" }
    } finally {
        Pop-Location
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $project "gradle\wrapper") | Out-Null
    Copy-Item (Join-Path $stub "gradlew"), (Join-Path $stub "gradlew.bat") -Destination $project -Force
    Copy-Item (Join-Path $stub "gradle\wrapper\*") -Destination (Join-Path $project "gradle\wrapper") -Force
} finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
