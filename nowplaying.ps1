# What the PC is playing, for the deck's now-playing card. Started by deck_server.py.
# Uses Windows' own media controls (the same info as the volume pop-up), so it works with
# YouTube in a browser, Spotify, Media Player and anything else that shows there.
# Prints one JSON line about every second; the cover picture is saved to $ArtFile when it changes.
# Reads commands from deck_server.py, one per line: "seek <seconds>", "next" or "prev".
param([string]$ArtFile)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false  # so titles with emoji or accents arrive intact
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
# waits at most 5 s: Windows' media info can hang, and reading .Result of an unfinished task would wait forever
function Wait-Op($op, [type]$type) {
  $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
  if (-not $t.Wait(5000)) { throw (New-Object System.TimeoutException) }
  $t.Result
}

$TMgr = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime]
$TProps = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media.Control, ContentType = WindowsRuntime]
$TStream = [Windows.Storage.Streams.IRandomAccessStreamWithContentType, Windows.Storage.Streams, ContentType = WindowsRuntime]
$asStream = [System.IO.WindowsRuntimeStreamExtensions].GetMethod("AsStream", [type[]]@([Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]))
$mgr = Wait-Op ($TMgr::RequestAsync()) $TMgr
$lastArt = ""; $artNo = 0; $stuck = 0; $artHash = ""; $tries = 0; $hasArt = $false
Remove-Item "$ArtFile*" -ErrorAction SilentlyContinue  # pictures from a previous run
$md5 = [System.Security.Cryptography.MD5]::Create()  # to tell whether the picture really changed
$shown = $null  # the player the deck is showing; its jump/skip commands go to this one
# (not [Console]::In: its ReadLineAsync waits for a line before returning)
$in = New-Object System.IO.StreamReader ([Console]::OpenStandardInput()); $cmd = $in.ReadLineAsync()

while ($true) {
  $out = @{ playing = $false }; $shown = $null
  try {
    # whatever is actually playing wins; Windows' own "current" player can be a paused video
    $s = @($mgr.GetSessions()) | Where-Object { [string]$_.GetPlaybackInfo().PlaybackStatus -eq "Playing" } | Select-Object -First 1
    # Some Spotify installs never join Windows' media list, but they put "Artist - Song" in their window title
    # while playing ("Spotify Premium" when paused) and obey the PC's next/previous keys
    $spot = if (-not $s) { Get-Process Spotify -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -match ' - ' } | Select-Object -First 1 -ExpandProperty MainWindowTitle }
    if (-not $s -and $spot) {
      $i = $spot.IndexOf(" - "); $lastArt = ""
      $out = @{ playing = $true; status = "Playing"; title = $spot.Substring($i + 3); artist = $spot.Substring(0, $i); app = "Spotify"
                position = 0; length = 0; art = 0; next = $true; prev = $true; seek = $false; keys = $true }  # keys: skip with the PC's media keys
    }
    if (-not $s -and -not $spot) { $s = $mgr.GetCurrentSession() }
    if ($s) {
      $shown = $s
      $p = Wait-Op ($s.TryGetMediaPropertiesAsync()) $TProps
      $tl = $s.GetTimelineProperties()
      $info = $s.GetPlaybackInfo(); $status = [string]$info.PlaybackStatus
      $pos = $tl.Position.TotalSeconds
      # the position is as of LastUpdatedTime; while playing, add the time since then
      if ($status -eq "Playing" -and $tl.LastUpdatedTime.Year -gt 2000) { $pos += ([DateTimeOffset]::Now - $tl.LastUpdatedTime).TotalSeconds }
      $len = ($tl.EndTime - $tl.StartTime).TotalSeconds
      $key = "$($p.Title)|$($p.Artist)|$($s.SourceAppUserModelId)"
      if ($key -ne $lastArt) { $lastArt = $key; $tries = 0; $artHash = ""; $hasArt = $false }
      # Apps often change the picture a moment AFTER the title (Brave switching tabs keeps the old video's picture for
      # a second), so after a change the picture is read every second for 15 s, then every 10 s, and replaced
      # whenever it's different
      $tries++
      if ($p.Thumbnail -and ($tries -le 15 -or $tries % 10 -eq 0)) {
        # a picture that can't be read is skipped on its own: the title, time and keys still reach the deck
        $io = $null
        try {
          $st = Wait-Op ($p.Thumbnail.OpenReadAsync()) $TStream
          $io = $asStream.Invoke($null, @($st))  # called by reflection: PowerShell can't pick the overload for this object
          $ms = New-Object System.IO.MemoryStream; $io.CopyTo($ms)
          if ($ms.Length -gt 0) {
            $hash = [Convert]::ToBase64String($md5.ComputeHash($ms.ToArray()))
            if ($hash -ne $artHash) {
              # each picture gets its own file (art.bin.<n>), written before its number is announced, so the deck can
              # never get one song's picture for another; the one two back is removed
              [System.IO.File]::WriteAllBytes("$ArtFile.$($artNo + 1)", $ms.ToArray())
              $artNo++; $artHash = $hash; $hasArt = $true
              Remove-Item "$ArtFile.$($artNo - 2)" -ErrorAction SilentlyContinue
            }
          }
        } catch {} finally { if ($io) { $io.Dispose() } }
      }
      $out = @{ playing = ($status -eq "Playing"); status = $status; title = $p.Title; artist = $p.Artist; album = $p.AlbumTitle
                app = $s.SourceAppUserModelId; position = [math]::Round([math]::Max(0, $pos), 1); length = [math]::Round([math]::Max(0, $len), 1)
                art = $(if ($hasArt) { $artNo } else { 0 })
                # what the app lets you do: YouTube has no next/previous outside a playlist, but lets you jump in the video
                next = $info.Controls.IsNextEnabled; prev = $info.Controls.IsPreviousEnabled; seek = $info.Controls.IsPlaybackPositionEnabled }
    }
    $stuck = 0
  } catch {
    $out = @{ playing = $false; error = $_.Exception.GetType().Name }
    # stuck three times running: quit, and deck_server.py starts a fresh reader
    if ($_.Exception -is [System.TimeoutException] -or $_.Exception.InnerException -is [System.TimeoutException]) { if (++$stuck -ge 3) { exit 1 } }
  }
  [Console]::Out.WriteLine(($out | ConvertTo-Json -Compress))
  [Console]::Out.Flush()
  # wait up to a second for a command; one that arrives is done straight away, then the new state is printed
  if ($cmd.Wait(1000)) {
    $line = $cmd.Result
    if ($null -eq $line) { exit }  # deck_server.py stopped
    try {
      $s = $shown
      if ($s -and $line -match '^seek (\d+(\.\d+)?)$') { Wait-Op ($s.TryChangePlaybackPositionAsync([long]([double]$Matches[1] * 1e7))) ([bool]) | Out-Null }
      elseif ($s -and $line -eq "next") { Wait-Op ($s.TrySkipNextAsync()) ([bool]) | Out-Null }
      elseif ($s -and $line -eq "prev") { Wait-Op ($s.TrySkipPreviousAsync()) ([bool]) | Out-Null }
    } catch {}
    $cmd = $in.ReadLineAsync()
    Start-Sleep -Milliseconds 150  # give the app a moment to move before reporting
  }
}
