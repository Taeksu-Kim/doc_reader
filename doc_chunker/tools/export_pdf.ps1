param([string]$dir)
$ErrorActionPreference = "Stop"
$wd = New-Object -ComObject Word.Application
$wd.Visible = $false
$doc = $wd.Documents.Open("$dir\manual.docx", $false, $true)
# ExportAsFixedFormat(path, format=17 pdf, open, intent, range, from, to, item, props, irm, bookmarks)
$doc.ExportAsFixedFormat("$dir\manual_bookmarks.pdf", 17, $false, 0, 0, 1, 1, 0, $true, $true, 1)
$doc.ExportAsFixedFormat("$dir\manual_plain.pdf", 17, $false, 0, 0, 1, 1, 0, $true, $true, 0)
"pages=" + $doc.ComputeStatistics(2)
$doc.Close($false); $wd.Quit()
