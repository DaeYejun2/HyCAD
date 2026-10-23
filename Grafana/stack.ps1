# HyCAD 데모 스택 (Prometheus + Grafana). 설치 없이 zip 풀어둔 것을 그대로 쓴다.
#
#   .\stack.ps1 start      Prometheus(9090) + Grafana(3000) 기동
#   .\stack.ps1 status     지금 뭐가 돌고 있나 + 자원 사용량
#   .\stack.ps1 stop       둘 다 종료
#   .\stack.ps1 backfill   캡처를 다시 구워 넣는다 (기존 블록은 지운다)
#   .\stack.ps1 demo       배속 데모(9901)까지 같이 — 공격 3종 + 실제 드리프트, 한 바퀴 2분 40초
#   .\stack.ps1 replay     리플레이 익스포터(9900)까지 같이 띄운다
#
# 대시보드 두 개
#   /d/hycad-live   백필된 실험 전체 5.1시간  (job="hycad",      포트 9900)
#   /d/hycad-demo   배속 데모                (job="hycad-demo", 포트 9901)
#
# 둘 다 127.0.0.1 에만 바인딩한다. 방화벽 경고를 피하려는 것이고,
# 다른 기기에서 봐야 하면 custom.ini 의 http_addr 과 --web.listen-address 를 바꾼다.
param([string]$Action = "start")

$Live  = $PSScriptRoot
$Stack = Join-Path $Live "stack"
$Prom  = Join-Path $Stack "prometheus-3.15.1.windows-amd64\prometheus.exe"
$PTool = Join-Path $Stack "prometheus-3.15.1.windows-amd64\promtool.exe"
$Graf  = Join-Path $Stack "grafana-v11.6.0"
$Data  = Join-Path $Stack "data"
$OM    = Join-Path $Stack "openmetrics.txt"

function Stop-All {
    Get-Process prometheus, grafana-server, grafana -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "name='python.exe'" |
        Where-Object { $_.CommandLine -like "*hycad_live.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host "종료함"
}

function Start-Prom {
    Start-Process -FilePath $Prom -WorkingDirectory $Stack -WindowStyle Minimized `
        -ArgumentList "--config.file=$Live\prometheus.yml", "--storage.tsdb.path=$Data",
                      "--web.listen-address=127.0.0.1:9090", "--web.enable-lifecycle",
                      "--storage.tsdb.retention.time=1y"
    # 보존기간 기본값은 15일이다. 그대로 두면 구워 넣은 블록이 조용히 사라진다.
    Write-Host "Prometheus  http://localhost:9090"
}

function Start-Graf {
    Start-Process -FilePath (Join-Path $Graf "bin\grafana-server.exe") `
        -WorkingDirectory $Graf -WindowStyle Minimized `
        -ArgumentList "--homepath", $Graf, "--config", (Join-Path $Graf "conf\custom.ini")
    Write-Host "Grafana     http://localhost:3000/d/hycad-live  (로그인 없이 보기 가능)"
}

function Show-Status {
    $rows = @()
    foreach ($n in @("prometheus", "grafana", "grafana-server")) {
        Get-Process -Name $n -ErrorAction SilentlyContinue | ForEach-Object {
            $rows += [pscustomobject]@{ 이름 = $_.ProcessName; PID = $_.Id
                                        RAM_MB = [math]::Round($_.WorkingSet64 / 1MB) }
        }
    }
    Get-CimInstance Win32_Process -Filter "name='python.exe'" |
        Where-Object { $_.CommandLine -like "*hycad_live.py*" } | ForEach-Object {
            $p = Get-Process -Id $_.ProcessId
            $mode = if ($_.CommandLine -like "*demo*") { "python(demo)" } else { "python(replay)" }
            $rows += [pscustomobject]@{ 이름 = $mode; PID = $p.Id
                                        RAM_MB = [math]::Round($p.WorkingSet64 / 1MB) }
        }
    if ($rows) {
        $rows | Format-Table -AutoSize
        "RAM 합계 {0} MB" -f ($rows | Measure-Object RAM_MB -Sum).Sum
        "  Prometheus  http://localhost:9090"
        "  Grafana     http://localhost:3000/d/hycad-live"
        "              http://localhost:3000/d/hycad-demo"
    } else {
        "돌고 있는 것이 없다. .\stack.ps1 demo 로 시작할 것"
    }
}

switch ($Action) {
    "stop" { Stop-All }

    "status" { Show-Status }

    "backfill" {
        Stop-All
        Start-Sleep -Seconds 2
        if (Test-Path $Data) { Remove-Item -Recurse -Force $Data }
        $env:PYTHONIOENCODING = "utf-8"
        & python (Join-Path $Live "backfill.py")
        & $PTool tsdb create-blocks-from openmetrics $OM $Data
        Write-Host "`n블록 생성 완료. .\stack.ps1 start 로 띄울 것"
    }

    "demo" {
        Start-Prom
        Start-Graf
        $env:PYTHONIOENCODING = "utf-8"
        # 공격 3종(크립토마이닝·정찰·리버스 셸) + 실제 드리프트(Node 4.8 -> 10.24).
        # 159 스냅샷 x 1초 = 한 바퀴 2분 40초. 드리프트 구간에서 재학습이 1회 돈다.
        Start-Process -FilePath "python" -WorkingDirectory $Live `
            -ArgumentList "hycad_live.py", "demo", "--interval", "1", "--limit", "10",
                          "--loop", "--port", "9901"
        Write-Host "데모        http://localhost:3000/d/hycad-demo  (한 바퀴 2분 40초)"
    }

    "replay" {
        Start-Prom
        Start-Graf
        $env:PYTHONIOENCODING = "utf-8"
        # 공격 투입 경계 앞뒤 12 스냅샷씩만, 5초 간격(스크레이프와 동일), 무한 반복.
        # 캡처 하나당 2분이라 4종 한 바퀴가 8분이다.
        Start-Process -FilePath "python" -WorkingDirectory $Live `
            -ArgumentList "hycad_live.py", "replay",
                          "cat_1of4", "cat_3of4", "cat_4of4", "recon_4of4",
                          "--limit", "12", "--interval", "5", "--loop"
        Write-Host "익스포터    http://localhost:9900/metrics  (5초/스냅샷, 한 바퀴 8분)"
        Write-Host "            Grafana 시간 범위를 Last 15 minutes 로 바꿀 것"
    }

    default { Start-Prom; Start-Graf }
}
