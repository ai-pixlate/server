# Windows PowerShell 5.1. Local display hints for the text review page.
param([Parameter(Mandatory)][string]$Drafts, [Parameter(Mandatory)][string]$Plan)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]
$bridge = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Await-Result($Operation, $Type) {
    $task = $bridge.MakeGenericMethod($Type).Invoke($null, @($Operation))
    $task.GetAwaiter().GetResult()
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('ko'))
if (!$engine) { throw 'Korean recognizer unavailable' }
$queue = Get-Content -Raw -Encoding utf8 -LiteralPath $Plan | ConvertFrom-Json
$outputDir = Join-Path $Drafts '_locations'
[void][IO.Directory]::CreateDirectory($outputDir)
foreach ($id in @($queue.mandatory) + @($queue.sample)) {
    $draft = Get-Content -Raw -Encoding utf8 -LiteralPath (Join-Path $Drafts "$id.json") | ConvertFrom-Json
    $path = $draft.image.path
    if ((Get-FileHash -LiteralPath $path).Hash -ne $draft.image.sha256) { throw "Image changed: $id" }
    $file = Await-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
    $stream = Await-Result ($file.OpenReadAsync()) ([Windows.Storage.Streams.IRandomAccessStreamWithContentType,Windows.Storage.Streams,ContentType=WindowsRuntime])
    try {
        $decoder = Await-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await-Result ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime])
        try {
            $result = Await-Result ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime])
            $boxes = foreach ($line in $result.Lines) {
                $words = @($line.Words | ForEach-Object {
                    $r = $_.BoundingRect
                    [ordered]@{text=$_.Text; x=$r.X; y=$r.Y; w=$r.Width; h=$r.Height}
                })
                if ($words.Count) { @{words=$words} }
            }
            $record = @{id=$id; sha256=$draft.image.sha256; lines=@($boxes | Where-Object { $_ }); source='windows-local-ko'}
            [IO.File]::WriteAllText((Join-Path $outputDir "$id.json"), ($record | ConvertTo-Json -Depth 8 -Compress))
        } finally { $bitmap.Dispose() }
    } finally { $stream.Dispose() }
}
