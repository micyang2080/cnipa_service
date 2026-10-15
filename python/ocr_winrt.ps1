param(
  [Parameter(Mandatory=$true)][string]$ImgPath,
  [string]$Lang = 'zh-CN'
)

# 2026-09-01 19:02 实跑教训：管道 stdout 默认按系统 ANSI 代码页（中文系统=GBK）
# 输出，Python 侧按 UTF-8 解码 → 中文全成 U+FFFD（shot1_ocr.txt 实证 EF BF BD）。
# 强制无 BOM UTF-8；无控制台句柄（GUI 宿主）时 setter 抛异常 → 由 Python 侧
# gbk 兜底解码（cnipa_ocr._decode）双保险。
try {
  [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
} catch { }

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine,Windows.Media.Ocr,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]

Function Await($WinRtTask, $ResultType) {
  $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                   $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
  $netTask = $asTask.Invoke($null, @($WinRtTask))
  $netTask.Wait(-1) | Out-Null
  $netTask.Result
}

try {
  $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImgPath)) ([Windows.Storage.StorageFile])
  $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bmp = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  if ($Lang -and $Lang -ne 'user') {
    $langObj = New-Object Windows.Globalization.Language($Lang)
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($langObj)
  } else {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  }
  if ($null -eq $engine) { Write-Output 'OCR_ENGINE_NULL(' + $Lang + ')'; exit 2 }
  Write-Output ("LANG=" + $engine.RecognizerLanguage.LanguageTag)
  $result = Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
  Write-Output ("LINES=" + $result.Lines.Count)
  foreach ($line in $result.Lines) {
    $l = [double]::MaxValue; $t = [double]::MaxValue; $r = 0.0; $b = 0.0
    foreach ($w in $line.Words) {
      $rc = $w.BoundingRect
      if ($rc.X -lt $l) { $l = $rc.X }
      if ($rc.Y -lt $t) { $t = $rc.Y }
      if (($rc.X + $rc.Width) -gt $r) { $r = $rc.X + $rc.Width }
      if (($rc.Y + $rc.Height) -gt $b) { $b = $rc.Y + $rc.Height }
    }
    Write-Output ("L|" + [int]$l + "," + [int]$t + "," + [int]$r + "," + [int]$b + "|" + $line.Text)
  }
} catch {
  Write-Output ("OCR_ERR: " + $_.Exception.Message)
  exit 1
}
