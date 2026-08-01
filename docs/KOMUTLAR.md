# AlphaForge Komut Rehberi

Bu sayfa AlphaForge'u kurmak, güncellemek, test etmek, BACKTEST/PAPER çalıştırmak, dashboard açmak ve çok günlük PAPER burn-in kampanyasını yönetmek için doğrulanmış komutları tek yerde toplar.

> **Güvenlik:** AlphaForge varsayılan olarak LIVE-ready değildir. Bu rehber PAPER ve BACKTEST işletimine odaklanır. LIVE modu veya gerçek emir yolu, yerel readiness kanıtları ve bütün fail-closed güvenlik kapıları geçmeden açılmamalıdır.

---

## 1. Repo köküne geç

Bütün komutları repository kökünden çalıştır.

### macOS / Linux

```bash
cd /Volumes/Slave/Projects/AlphaForge
```

### Windows PowerShell

```powershell
cd E:\Projeler\AlphaForge
```

Konumu doğrula:

```bash
pwd
git status
```

PowerShell:

```powershell
Get-Location
git status
```

---

## 2. `dev` branch'i güncelle

```bash
git switch dev
git pull origin dev
git status
```

Geçerli commit:

```bash
git rev-parse --short HEAD
git log -1 --oneline
```

Burn-in preflight temiz çalışma ağacı bekler. `git status` çıktısında commitlenmemiş değişiklik bırakma.

---

## 3. Sanal ortam ve kurulum

### macOS / Linux

İlk kurulum:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e '.[dev]'
```

Sonraki oturumlarda yalnızca:

```bash
source .venv/bin/activate
```

### Windows PowerShell

İlk kurulum:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

Sonraki oturumlarda yalnızca:

```powershell
.\.venv\Scripts\Activate.ps1
```

Kurulumu doğrula:

```bash
python --version
python -c "import alphaforge; print(alphaforge.__file__)"
```

CLI yardım ekranları:

```bash
python -m alphaforge.burnin_ops --help
python -m alphaforge.burnin_cli --help
python backtest_order.py --help
python -m uvicorn --help
```

---

## 4. Ortam profilini seç

Tek bir profili `.env` olarak kopyala.

### BACKTEST / yerel teşhis

macOS / Linux:

```bash
cp .env.test.example .env
```

PowerShell:

```powershell
Copy-Item .env.test.example .env
```

### Dengeli PAPER / dashboard değerlendirmesi

macOS / Linux:

```bash
cp .env.medium.example .env
```

PowerShell:

```powershell
Copy-Item .env.medium.example .env
```

### LIVE hazırlık şablonu

Bu profil gerçek emirleri kendiliğinden açmaz. Yalnızca readiness hazırlığı içindir.

macOS / Linux:

```bash
cp .env.live.example .env
```

PowerShell:

```powershell
Copy-Item .env.live.example .env
```

Mod için kanonik değişken:

```text
ALPHAFORGE_EXECUTION_MODE=BACKTEST|PAPER|LIVE
```

Geriye uyumluluk alias'ı:

```text
EXECUTION_MODE=BACKTEST|PAPER|LIVE
```

PAPER burn-in öncesinde ikisinin de PAPER olduğundan emin ol.

macOS / Linux:

```bash
export ALPHAFORGE_EXECUTION_MODE=PAPER
export EXECUTION_MODE=PAPER
```

PowerShell:

```powershell
$env:ALPHAFORGE_EXECUTION_MODE="PAPER"
$env:EXECUTION_MODE="PAPER"
```

---

## 5. Veritabanını tanımla

### macOS / Linux

```bash
DB="/Volumes/Slave/Projects/AlphaForge/data/runtime/alphaforge_runtime.db"
export DB
export ALPHAFORGE_DB_PATH="$DB"
```

### Windows PowerShell

```powershell
$DB="E:\Projeler\AlphaForge\data\runtime\alphaforge_runtime.db"
$env:ALPHAFORGE_DB_PATH=$DB
```

Dosyayı kontrol et:

macOS / Linux:

```bash
ls -lh "$DB"
```

PowerShell:

```powershell
Get-Item $DB
```

SQLite bütünlük kontrolü:

```bash
sqlite3 "$DB" "PRAGMA integrity_check;"
```

PowerShell:

```powershell
sqlite3 $DB "PRAGMA integrity_check;"
```

Beklenen çıktı:

```text
ok
```

---

## 6. Migration çalıştır

```bash
alembic upgrade head
```

Mevcut migration seviyesini göster:

```bash
alembic current
```

Migration geçmişi:

```bash
alembic history
```

---

## 7. Testler

### Tam test paketi

```bash
pytest -q
```

### İlk hatada dur

```bash
pytest -q -x
```

### Ayrıntılı hata çıktısı

```bash
pytest -vv
```

### Belirli test dosyası

```bash
pytest -q tests/test_phase8_burnin_campaign.py
pytest -q tests/test_phase9_burnin_ops.py
pytest -q tests/test_dashboard_app.py
```

### Belirli test adı / anahtar kelime

```bash
pytest -q -k burnin
pytest -q -k dashboard
pytest -q -k runtime
```

### Son başarısız testleri tekrar çalıştır

```bash
pytest -q --lf
```

### Yalnızca önceki başarısızlardan başla, sonra devam et

```bash
pytest -q --ff
```

---

## 8. BACKTEST çalıştır

### Doğrudan Python komutu

```bash
python backtest_order.py \
  --interval 1h \
  --last-n-days 30 \
  --symbols BTCUSDT,ETHUSDT \
  --output-dir data/backtests/manual_1h_30d
```

### Binance geçmiş önbelleğini yenile

```bash
python backtest_order.py \
  --interval 1h \
  --last-n-days 30 \
  --symbols BTCUSDT,ETHUSDT \
  --output-dir data/backtests/manual_1h_30d \
  --force-refresh
```

### Ağ çağrısı yapmayan CI/offline smoke backtest

```bash
python backtest_order.py \
  --ci \
  --interval 1h \
  --last-n-days 7 \
  --symbols BTCUSDT \
  --output-dir data/backtests/ci_smoke
```

### BACKTEST-only SHORT breakdown rescue karşılaştırması

macOS / Linux:

```bash
ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED=true \
python backtest_order.py \
  --interval 1h \
  --last-n-days 30 \
  --symbols BTCUSDT,ETHUSDT \
  --output-dir data/backtests/rescue_on
```

PowerShell:

```powershell
$env:ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED="true"
python backtest_order.py --interval 1h --last-n-days 30 --symbols BTCUSDT,ETHUSDT --output-dir data/backtests/rescue_on
Remove-Item Env:ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED
```

### Kısayol scriptleri

PowerShell:

```powershell
.\scripts\run_backtest.ps1 -Interval 1h -Days 30 -Symbols BTCUSDT,ETHUSDT
```

macOS / Linux:

```bash
bash scripts/run_backtest.sh 1h 30 BTCUSDT,ETHUSDT
```

### BACKTEST durdurma

Foreground çalışıyorsa terminalde:

```text
Ctrl+C
```

---

## 9. PAPER runtime çalıştır

### Doğrudan çalıştır

macOS / Linux:

```bash
ALPHAFORGE_MODE=PAPER python -m alphaforge.runtime
```

PowerShell:

```powershell
$env:ALPHAFORGE_MODE="PAPER"
python -m alphaforge.runtime
```

### Güvenli placeholder scanner ile deterministik smoke

macOS / Linux:

```bash
ALPHAFORGE_MODE=PAPER ALPHAFORGE_RUNTIME_SAFE_SCANNER=1 python -m alphaforge.runtime
```

PowerShell:

```powershell
$env:ALPHAFORGE_MODE="PAPER"
$env:ALPHAFORGE_RUNTIME_SAFE_SCANNER="1"
python -m alphaforge.runtime
```

### Kısayol scriptleri

PowerShell:

```powershell
.\scripts\run_paper.ps1
```

macOS / Linux:

```bash
bash scripts/run_paper.sh
```

### PAPER runtime durdurma

Foreground çalışıyorsa terminalde:

```text
Ctrl+C
```

Durdurduktan sonra runtime/burn-in durumunu ve son hatayı kontrol et. İşletim sistemi seviyesinde zorla öldürme, temiz kapanış kanıtı üretmeyebilir.

---

## 10. Dashboard çalıştır

### Doğrudan çalıştır

```bash
python -m uvicorn alphaforge.dashboard.app:create_app \
  --factory \
  --host 127.0.0.1 \
  --port 8000
```

Tarayıcı:

```text
http://127.0.0.1:8000
```

### Belirli SQLite DB ile çalıştır

macOS / Linux:

```bash
export ALPHAFORGE_DATABASE_URL="sqlite+pysqlite:///$DB"
python -m uvicorn alphaforge.dashboard.app:create_app --factory --host 127.0.0.1 --port 8000
```

PowerShell:

```powershell
$env:ALPHAFORGE_DATABASE_URL="sqlite+pysqlite:///$($DB -replace '\\','/')"
python -m uvicorn alphaforge.dashboard.app:create_app --factory --host 127.0.0.1 --port 8000
```

### Kısayol scriptleri

PowerShell:

```powershell
.\scripts\run_dashboard.ps1 -Port 8000
```

macOS / Linux:

```bash
bash scripts/run_dashboard.sh 8000
```

### Dashboard durdurma

Foreground çalışıyorsa terminalde:

```text
Ctrl+C
```

Portu kullanan süreci bul:

macOS / Linux:

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

PowerShell:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen
```

---

# PAPER Burn-in Operasyonları

Yeni işletim akışında tercih edilen arayüz `alphaforge.burnin_ops` komutudur.

## 11. Burn-in yardım komutları

```bash
python -m alphaforge.burnin_ops --help
python -m alphaforge.burnin_ops preflight --help
python -m alphaforge.burnin_ops launch --help
python -m alphaforge.burnin_ops health --help
python -m alphaforge.burnin_ops watch --help
python -m alphaforge.burnin_ops recovery-drill --help
python -m alphaforge.burnin_ops audit --help
python -m alphaforge.burnin_ops pause --help
python -m alphaforge.burnin_ops resume --help
python -m alphaforge.burnin_ops status --help
python -m alphaforge.burnin_ops report --help
python -m alphaforge.burnin_ops finalize --help
```

Makine tarafından işlenecek JSON çıktı için global `--json` seçeneğini `--db` sonrasında ve alt komuttan önce kullan:

```bash
python -m alphaforge.burnin_ops --db "$DB" --json status --campaign-id CAMP_ID
```

---

## 12. Preflight

`RELEASE_ID` her kampanya için bilinçli seçilmeli ve çalışma boyunca değiştirilmemelidir.

macOS / Linux:

```bash
$RELEASE_ID="phase9_trial_1"

python -m alphaforge.burnin_ops \
  --db "$DB" \
  preflight \
  --release-id "$RELEASE_ID" \
  --symbols BTCUSDT,ETHUSDT \
  --intervals 1h
```

PowerShell:

```powershell
$RELEASE_ID="phase9_trial_1"

python -m alphaforge.burnin_ops `
  preflight `
  --release-id $RELEASE_ID `
  --symbols BTCUSDT `
  --intervals 1h
```

Özel preflight çıktı klasörü:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  preflight \
  --release-id "$RELEASE_ID" \
  --symbols BTCUSDT ETHUSDT \
  --intervals 1h \
  --output-dir "artifacts/burnin/preflight_${RELEASE_ID}"
```

Preflight `PASS` olmadan launch yapma. Özellikle şunları düzelt:

- çalışma ağacı temizliği
- `dev` branch kontrolü
- PAPER execution mode
- DB yazılabilirliği ve schema
- release/config/strategy/universe/execution-cost identity eşleşmesi
- sembol ve interval doğrulaması
- saat sapması ve read-only market data erişimi

---

## 13. Çok günlük kampanyayı başlat

### Detached worker ile önerilen çalıştırma

macOS / Linux:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  launch \
  --release-id "$RELEASE_ID" \
  --duration-days 3 \
  --symbols BTCUSDT,ETHUSDT \
  --intervals 1h \
  --detach \
  --attach-timeout-seconds 60
```

PowerShell:

```powershell

```

Komut çıktısındaki gerçek `campaign_id` değerini kaydet.

macOS / Linux örneği:

```bash
CAMPAIGN_ID="camp_xxxxxxxxxxxxxxxx"
export CAMPAIGN_ID
```

PowerShell örneği:

```powershell
$CAMPAIGN_ID="camp_xxxxxxxxxxxxxxxx"
```

> `campaign_id` tahmin edilmez. Launch çıktısından veya SQL sorgusundan alınır.

---

## 14. Son kampanya ID'sini SQL'den bul

```bash
sqlite3 "$DB" <<'SQL'
.headers on
.mode column
SELECT
    campaign_id,
    release_id,
    campaign_status,
    active_run_id,
    worker_pid,
    created_at,
    last_heartbeat_at,
    last_error
FROM burnin_campaigns
ORDER BY created_at DESC
LIMIT 10;
SQL
```

PowerShell tek satır:

```powershell
sqlite3 $DB "SELECT campaign_id,release_id,campaign_status,active_run_id,worker_pid,created_at,last_heartbeat_at,last_error FROM burnin_campaigns ORDER BY created_at DESC LIMIT 10;"
```

---

## 15. Kampanya status

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  status \
  --campaign-id "$CAMPAIGN_ID"
```

JSON:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  --json \
  status \
  --campaign-id "$CAMPAIGN_ID"
```

PowerShell:

```powershell
python -m alphaforge.burnin_ops --db $DB status --campaign-id $CAMPAIGN_ID
```

---

## 16. Health kontrolü

Tek kontrol:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  health \
  --campaign-id "$CAMPAIGN_ID"
```

JSON:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  --json \
  health \
  --campaign-id "$CAMPAIGN_ID"
```

---

## 17. Watch

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  watch \
  --campaign-id "$CAMPAIGN_ID"
```

`watch` tek bir operasyon kontrol çevrimi çalıştırır; sürekli terminal ekranı varsayımı yapma. Periyodik izleme gerekiyorsa komutu scheduler veya kontrollü shell döngüsüyle çağır.

macOS / Linux örneği, 60 saniyede bir:

```bash
while true; do
  date
  python -m alphaforge.burnin_ops --db "$DB" watch --campaign-id "$CAMPAIGN_ID"
  sleep 60
done
```

Döngüyü durdur:

```text
Ctrl+C
```

---

## 18. Worker loglarını izle

```bash
tail -n 200 "artifacts/burnin/$CAMPAIGN_ID/worker.stdout.log"
tail -n 200 "artifacts/burnin/$CAMPAIGN_ID/worker.stderr.log"
```

Canlı takip:

```bash
tail -f "artifacts/burnin/$CAMPAIGN_ID/worker.stdout.log"
```

Hata logunu canlı takip:

```bash
tail -f "artifacts/burnin/$CAMPAIGN_ID/worker.stderr.log"
```

PowerShell:

```powershell
Get-Content "artifacts\burnin\$CAMPAIGN_ID\worker.stdout.log" -Tail 200
Get-Content "artifacts\burnin\$CAMPAIGN_ID\worker.stderr.log" -Tail 200
Get-Content "artifacts\burnin\$CAMPAIGN_ID\worker.stderr.log" -Wait -Tail 50
```

---

## 19. Kampanyayı normal şekilde duraklat

Detached burn-in'i durdurmak için ilk tercih `pause` olmalıdır:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  pause \
  --campaign-id "$CAMPAIGN_ID"
```

PowerShell:

```powershell
python -m alphaforge.burnin_ops --db $DB pause --campaign-id $CAMPAIGN_ID
```

Ardından doğrula:

```bash
python -m alphaforge.burnin_ops --db "$DB" status --campaign-id "$CAMPAIGN_ID"
python -m alphaforge.burnin_ops --db "$DB" health --campaign-id "$CAMPAIGN_ID"
```

`pause`, kampanyayı silmez ve tamamlanmış gibi işaretlemez.

---

## 20. Kampanyayı devam ettir

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  resume \
  --campaign-id "$CAMPAIGN_ID"
```

PowerShell:

```powershell
python -m alphaforge.burnin_ops --db $DB resume --campaign-id $CAMPAIGN_ID
```

Resume sonrasında:

```bash
python -m alphaforge.burnin_ops --db "$DB" status --campaign-id "$CAMPAIGN_ID"
python -m alphaforge.burnin_ops --db "$DB" health --campaign-id "$CAMPAIGN_ID"
```

Release/config/strategy/universe/execution-cost identity değiştiyse devam etmeye zorlama. Yeni preflight ve yeni kampanya gerekir.

---

## 21. Recovery drill

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  recovery-drill \
  --campaign-id "$CAMPAIGN_ID"
```

PowerShell:

```powershell
python -m alphaforge.burnin_ops --db $DB recovery-drill --campaign-id $CAMPAIGN_ID
```

Bu komut gerçek bir recovery kanıtı üretir. Sırf status değiştirmek için kullanılmamalıdır.

---

## 22. Integrity audit

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  audit \
  --campaign-id "$CAMPAIGN_ID"
```

PowerShell:

```powershell
python -m alphaforge.burnin_ops --db $DB audit --campaign-id $CAMPAIGN_ID
```

Finalization öncesinde audit `PASS` olmalıdır.

---

## 23. Günlük rapor

```bash
REPORT_DIR="artifacts/burnin/$CAMPAIGN_ID/daily_$(date -u +%Y%m%dT%H%M%SZ)
"

python -m alphaforge.burnin_ops \
  --db "$DB" \
  report \
  --campaign-id "$CAMPAIGN_ID" \
  --output-dir "$REPORT_DIR"
```

PowerShell:

```powershell
$REPORT_DIR="artifacts\burnin\$CAMPAIGN_ID\daily_$(Get-Date -Format 'yyyyMMddTHHmmssZ')"
python -m alphaforge.burnin_ops --db $DB report --campaign-id $CAMPAIGN_ID --output-dir $REPORT_DIR
```

Rapor klasörü JSON, CSV ve Markdown günlük özet çıktıları üretir.

---

## 24. Finalize

Kampanya süresi tamamlanmadan, health/audit/recovery kanıtları oluşmadan finalize etmek `PAPER_BURNIN_INCOMPLETE` veya `PAPER_BURNIN_FAILED` sonucu verebilir. Bu fail-closed davranıştır.

macOS / Linux:

```bash
FINAL_DIR="artifacts/burnin/$CAMPAIGN_ID/final"

python -m alphaforge.burnin_ops \
  --db "$DB" \
  finalize \
  --campaign-id "$CAMPAIGN_ID" \
  --output-dir "$FINAL_DIR"
```

PowerShell:

```powershell
$FINAL_DIR="artifacts\burnin\$CAMPAIGN_ID\final"
python -m alphaforge.burnin_ops --db $DB finalize --campaign-id $CAMPAIGN_ID --output-dir $FINAL_DIR
```

Final paketinde en azından şu kanıtları incele:

- `release_decision.json`
- `final_manifest.json`
- `checksums.json`
- export edilen campaign evidence dosyaları

`PAPER_BURNIN_QUALIFIED_FOR_CANARY_REVIEW`, LIVE-ready veya gerçek emir izni değildir.

---

## 25. PAPER burn-in teşhis raporu

Kampanya operatöründen bağımsız, mevcut PAPER runtime DB için deterministik teşhis raporu:

```bash
python -m alphaforge.paper_burnin \
  --db "$DB" \
  --out reports/paper_burnin
```

Üretilen temel dosyalar:

- `paper_burnin_summary.csv`
- `paper_burnin_report.md`
- `paper_burnin_blockers.json`

---

# Legacy / Düşük Seviyeli Burn-in CLI

## 26. `burnin_cli` komutları

Yeni operasyonlarda `burnin_ops` tercih edilir. Aşağıdaki komutlar düşük seviyeli kampanya yönetimi ve teşhis içindir.

Yardım:

```bash
python -m alphaforge.burnin_cli --help
```

Kampanya oluştur:

```bash
python -m alphaforge.burnin_cli \
  --db "$DB" \
  create \
  --release-id "$RELEASE_ID" \
  --duration-days 3 \
  --symbols BTCUSDT,ETHUSDT \
  --intervals 1h
```

Detached başlat:

```bash
python -m alphaforge.burnin_cli \
  --db "$DB" \
  start \
  --campaign-id "$CAMPAIGN_ID" \
  --detach
```

Foreground başlat:

```bash
python -m alphaforge.burnin_cli \
  --db "$DB" \
  start \
  --campaign-id "$CAMPAIGN_ID" \
  --foreground
```

Status:

```bash
python -m alphaforge.burnin_cli --db "$DB" status --campaign-id "$CAMPAIGN_ID"
```

Pause:

```bash
python -m alphaforge.burnin_cli --db "$DB" pause --campaign-id "$CAMPAIGN_ID"
```

Resume detached:

```bash
python -m alphaforge.burnin_cli --db "$DB" resume --campaign-id "$CAMPAIGN_ID" --detach
```

Tek resolver tick:

```bash
python -m alphaforge.burnin_cli --db "$DB" worker --campaign-id "$CAMPAIGN_ID" --once
```

Qualification:

```bash
python -m alphaforge.burnin_cli --db "$DB" qualify --campaign-id "$CAMPAIGN_ID"
```

Evidence export:

```bash
python -m alphaforge.burnin_cli \
  --db "$DB" \
  export \
  --campaign-id "$CAMPAIGN_ID" \
  --output-dir "artifacts/burnin/$CAMPAIGN_ID/export"
```

---

# SQL Operasyon Sorguları

## 27. Kampanya özeti

```bash
sqlite3 "$DB" <<SQL
.headers on
.mode column
SELECT
    campaign_id,
    release_id,
    campaign_status,
    active_run_id,
    worker_pid,
    created_at,
    started_at,
    last_heartbeat_at,
    observed_duration_seconds,
    latest_qualification_id,
    last_error
FROM burnin_campaigns
WHERE campaign_id = '$CAMPAIGN_ID';
SQL
```

## 28. Son kampanya olayları

```bash
sqlite3 "$DB" <<SQL
.headers on
.mode column
.width 6 28 38 38 100
SELECT
    id,
    event_time,
    event_type,
    burnin_run_id,
    details_json
FROM burnin_campaign_events
WHERE campaign_id = '$CAMPAIGN_ID'
ORDER BY id DESC
LIMIT 50;
SQL
```

## 29. Run kayıtları

```bash
sqlite3 "$DB" <<SQL
.headers on
.mode column
SELECT *
FROM burnin_runs
WHERE campaign_id = '$CAMPAIGN_ID'
ORDER BY id DESC;
SQL
```

## 30. Health geçmişi

```bash
sqlite3 "$DB" <<SQL
.headers on
.mode column
SELECT
    id,
    generated_at,
    status,
    unhealthy_reasons_json
FROM burnin_health_history
WHERE campaign_id = '$CAMPAIGN_ID'
ORDER BY id DESC
LIMIT 30;
SQL
```

## 31. Incident geçmişi

```bash
sqlite3 "$DB" <<SQL
.headers on
.mode column
SELECT
    id,
    incident_type,
    severity,
    status,
    detected_at,
    details_json
FROM burnin_ops_incidents
WHERE campaign_id = '$CAMPAIGN_ID'
ORDER BY id DESC;
SQL
```

## 32. Recovery drill kayıtları

```bash
sqlite3 "$DB" <<SQL
.headers on
.mode column
SELECT
    id,
    drill_id,
    generated_at,
    status,
    checks_json
FROM burnin_recovery_drills
WHERE campaign_id = '$CAMPAIGN_ID'
ORDER BY id DESC;
SQL
```

## 33. Integrity audit kayıtları

```bash
sqlite3 "$DB" <<SQL
.headers on
.mode column
SELECT
    id,
    audit_id,
    generated_at,
    status,
    violations_json,
    aggregate_evidence_hash
FROM burnin_integrity_audits
WHERE campaign_id = '$CAMPAIGN_ID'
ORDER BY id DESC;
SQL
```

## 34. Final release decision

```bash
sqlite3 "$DB" <<SQL
.headers on
.mode column
SELECT
    id,
    decision_id,
    generated_at,
    decision,
    blockers_json,
    package_dir
FROM burnin_release_decisions
WHERE campaign_id = '$CAMPAIGN_ID'
ORDER BY id DESC;
SQL
```

## 35. Karar sayıları

```bash
sqlite3 "$DB" <<SQL
.headers on
.mode column
SELECT
    decision,
    COUNT(*) AS count
FROM burnin_observations
WHERE burnin_run_id IN (
    SELECT burnin_run_id
    FROM burnin_runs
    WHERE campaign_id = '$CAMPAIGN_ID'
)
GROUP BY decision
ORDER BY count DESC;
SQL
```

## 36. Reject nedenleri

```bash
sqlite3 "$DB" <<SQL
.headers on
.mode column
SELECT
    reject_reason,
    COUNT(*) AS count
FROM burnin_reject_outcomes
WHERE burnin_run_id IN (
    SELECT burnin_run_id
    FROM burnin_runs
    WHERE campaign_id = '$CAMPAIGN_ID'
)
GROUP BY reject_reason
ORDER BY count DESC;
SQL
```

## 37. Açık PAPER pozisyonları

Önce tablo şemasını doğrula:

```bash
sqlite3 "$DB" ".schema burnin_trade_outcomes"
```

Sonra açık kayıtları say:

```bash
sqlite3 "$DB" <<SQL
.headers on
.mode column
SELECT COUNT(*) AS open_trade_outcomes
FROM burnin_trade_outcomes
WHERE burnin_run_id IN (
    SELECT burnin_run_id
    FROM burnin_runs
    WHERE campaign_id = '$CAMPAIGN_ID'
)
AND closed_at IS NULL;
SQL
```

---

# Süreç ve Hata Teşhisi

## 38. Worker PID çalışıyor mu?

Kampanya PID'sini getir:

```bash
sqlite3 "$DB" "SELECT worker_pid FROM burnin_campaigns WHERE campaign_id='$CAMPAIGN_ID';"
```

macOS / Linux:

```bash
PID=$(sqlite3 "$DB" "SELECT worker_pid FROM burnin_campaigns WHERE campaign_id='$CAMPAIGN_ID';")
ps -p "$PID" -o pid,ppid,etime,state,command
```

PowerShell:

```powershell
$PID_FROM_DB=sqlite3 $DB "SELECT worker_pid FROM burnin_campaigns WHERE campaign_id='$CAMPAIGN_ID';"
Get-Process -Id $PID_FROM_DB
```

## 39. Python / Uvicorn süreçlerini listele

macOS / Linux:

```bash
ps aux | grep -E 'alphaforge|uvicorn|backtest_order' | grep -v grep
```

PowerShell:

```powershell
Get-CimInstance Win32_Process | Where-Object {
  $_.CommandLine -match 'alphaforge|uvicorn|backtest_order'
} | Select-Object ProcessId,Name,CommandLine
```

## 40. Acil zorla durdurma

Önce her zaman `burnin_ops pause` kullan. Yalnızca süreç cevap vermiyorsa ve operasyonel acil durum varsa PID seviyesinde sonlandır.

macOS / Linux:

```bash
kill -TERM "$PID"
```

PowerShell:

```powershell
Stop-Process -Id $PID_FROM_DB
```

Zorla sonlandırmadan sonra kampanyayı normal kabul etme. Aşağıdakileri çalıştır:

```bash
python -m alphaforge.burnin_ops --db "$DB" status --campaign-id "$CAMPAIGN_ID"
python -m alphaforge.burnin_ops --db "$DB" health --campaign-id "$CAMPAIGN_ID"
python -m alphaforge.burnin_ops --db "$DB" recovery-drill --campaign-id "$CAMPAIGN_ID"
python -m alphaforge.burnin_ops --db "$DB" audit --campaign-id "$CAMPAIGN_ID"
```

`UNCLEAN_SHUTDOWN_RECOVERY_REQUIRED` görülürse bunu status alanını elle değiştirerek gizleme. Recovery kanıtını tamamla veya fail-closed yeni kampanya başlat.

---

# Standart Operasyon Akışı

## 41. Yeni çok günlük PAPER burn-in kontrol listesi

```text
1. git switch dev && git pull origin dev
2. .venv aktive et
3. pip install -e '.[dev]'
4. PAPER ortam profilini ve DB yolunu doğrula
5. alembic upgrade head
6. pytest -q
7. burnin_ops preflight
8. preflight PASS ise burnin_ops launch --detach
9. campaign_id değerini kaydet
10. status + health + watch + worker loglarını izle
11. Kontrollü durdurma gerekiyorsa pause
12. Devam gerekiyorsa identity değişmeden resume
13. recovery-drill
14. audit
15. report
16. Kampanya tamamlanınca finalize
17. release_decision.json ve blocker'ları incele
```

## 42. Günlük kontrol komut seti

```bash
python -m alphaforge.burnin_ops --db "$DB" status --campaign-id "$CAMPAIGN_ID"
python -m alphaforge.burnin_ops --db "$DB" health --campaign-id "$CAMPAIGN_ID"
python -m alphaforge.burnin_ops --db "$DB" watch --campaign-id "$CAMPAIGN_ID"
tail -n 100 "artifacts/burnin/$CAMPAIGN_ID/worker.stderr.log"
tail -n 100 "artifacts/burnin/$CAMPAIGN_ID/worker.stdout.log"
```

## 43. Hata sonrası minimum teşhis paketi

```bash
python -m alphaforge.burnin_ops --db "$DB" --json status --campaign-id "$CAMPAIGN_ID"
python -m alphaforge.burnin_ops --db "$DB" --json health --campaign-id "$CAMPAIGN_ID"
tail -n 200 "artifacts/burnin/$CAMPAIGN_ID/worker.stderr.log"
tail -n 200 "artifacts/burnin/$CAMPAIGN_ID/worker.stdout.log"
sqlite3 "$DB" "PRAGMA integrity_check;"
```

Ardından son 30 kampanya olayını ve ilgili run kayıtlarını SQL ile çıkar.

---

## 44. Sık görülen fail-closed durumlar

### `PHASE8_CAMPAIGN_RELEASE_MISMATCH`

Persisted kampanya release kimliği ile process/runtime release kimliği farklıdır. Eski kampanyayı yeni release ile zorla devam ettirme.

### `PHASE8_CAMPAIGN_CONFIG_DRIFT`

Runtime config hash kampanya kimliğiyle eşleşmiyordur. `.env`, dashboard override veya process environment değişmiş olabilir.

### `PHASE8_CAMPAIGN_STRATEGY_DRIFT`

Stratejiye etki eden ayarlar kampanya oluşturulduktan sonra değişmiştir.

### `PHASE8_CAMPAIGN_UNIVERSE_DRIFT`

Sembol veya interval evreni değişmiştir.

### `PHASE8_CAMPAIGN_EXECUTION_COST_DRIFT`

Spread/slippage/latency/funding gibi execution-cost kimliği değişmiştir.

### `UNCLEAN_SHUTDOWN_RECOVERY_REQUIRED`

Önceki worker temiz kapanış kanıtı bırakmamıştır. Recovery drill ve audit yapılmadan normal resume/finalize varsayımı yapma.

### `WORKER_STARTUP_EXITED`

Detached worker başladıktan hemen sonra kapanmıştır. `worker.stderr.log`, `worker.stdout.log`, status ve campaign events incelenmelidir.

---

## 45. Komut yazım kuralları

- Dokümana terminalin continuation prompt karakteri olan `>` ekleme.
- Bash satır devamında `\`, PowerShell satır devamında backtick `` ` `` kullan.
- `--db` ve `--json`, alt komuttan önce yazılır.
- `campaign_id`, `release_id`, semboller ve interval seti çalışma boyunca kaydedilir.
- Başarısız guard'ı SQL ile elle PASS yapma.
- PAPER burn-in başarısını LIVE-ready olarak yorumlama.
- Zorla süreç öldürmek yerine önce uygulamanın `pause`/normal shutdown yolunu kullan.

.env terminale yükle
Get-Content .env | ForEach-Object {
    $line = $_.Trim()

    if (
        -not $line -or
        $line.StartsWith("#") -or
        -not $line.Contains("=")
    ) {
        return
    }

    $name, $value = $line -split "=", 2
    $name = $name.Trim()
    $value = $value.Trim()

    if ($value -match '\s+#') {
        $value = ($value -split '\s+#', 2)[0].Trim()
    }

    if (
        ($value.StartsWith('"') -and $value.EndsWith('"')) -or
        ($value.StartsWith("'") -and $value.EndsWith("'"))
    ) {
        $value = $value.Substring(1, $value.Length - 2)
    }

    [Environment]::SetEnvironmentVariable(
        $name,
        $value,
        "Process"
    )
}

#FINGERPRINT .ENV KARŞILAŞTIRMA
@'
import os
import hashlib
from pathlib import Path

def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()[:16] if value else None

env_values = {}

for raw in Path(".env").read_text(encoding="utf-8").splitlines():
    line = raw.strip()

    if not line or line.startswith("#") or "=" not in line:
        continue

    name, value = line.split("=", 1)
    name = name.strip()
    value = value.strip()

    if " #" in value:
        value = value.split(" #", 1)[0].strip()

    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in {'"', "'"}
    ):
        value = value[1:-1]

    env_values[name] = value

for name in ("BINANCE_API_KEY", "BINANCE_BASE_URL"):
    process_value = os.getenv(name)
    file_value = env_values.get(name)

    print(
        name,
        {
            "process_fingerprint": fingerprint(process_value),
            "dotenv_fingerprint": fingerprint(file_value),
            "match": process_value == file_value,
        },
    )
'@ | python
---

## Phase 9 operational acceptance (PAPER only, 2026-07-23)

`diagnose-db` is database read-only. `config_check` and the default `config_fix` dry-run do not mutate configuration. **Preflight is not read-only:** it may create or update local database evidence. None of these commands submit or cancel exchange orders. Keep `ALPHAFORGE_EXECUTION_MODE=PAPER`, `EXECUTION_MODE=PAPER`, and `ALPHAFORGE_ENABLE_LIVE_EXECUTION=false`. REST-only reconciliation does **not** require a websocket; runtime/streaming websocket requirements remain strict.

### PowerShell

```powershell
$env:ALPHAFORGE_EXECUTION_MODE = "PAPER"
$env:EXECUTION_MODE = "PAPER"
$env:ALPHAFORGE_ENABLE_LIVE_EXECUTION = "false"
$DB = "artifacts/burnin/phase9.db"
$RELEASE_ID = "phase9-$(git rev-parse --short HEAD)"
$CAMPAIGN_ID = "<campaign_id-from-launch-output>"

### A. Diagnose an existing database

python -m alphaforge.config_check
python -m alphaforge.config_fix --json
python -m alphaforge.burnin_ops --db $DB --json diagnose-db --max-heartbeat-age 120 | Tee-Object -FilePath artifacts/burnin/database_diagnosis.json

### B. Start a new clean campaign

python -m alphaforge.binance_reconciliation_check --symbols BTCUSDT ETHUSDT | Tee-Object -FilePath artifacts/burnin/reconciliation.json
python -m alphaforge.burnin_ops --db $DB --json preflight --release-id $RELEASE_ID --symbols BTCUSDT,ETHUSDT --intervals 1h --output-dir artifacts/burnin/preflight
python -m alphaforge.burnin_ops --db $DB --json launch --release-id $RELEASE_ID --duration-days 3 --symbols BTCUSDT,ETHUSDT --intervals 1h --detach
python -m alphaforge.burnin_ops --db $DB --json status --campaign-id $CAMPAIGN_ID
python -m alphaforge.burnin_ops --db $DB --json health --campaign-id $CAMPAIGN_ID
python -m alphaforge.burnin_ops --db $DB --json watch --campaign-id $CAMPAIGN_ID
python -m alphaforge.burnin_ops --db $DB --json audit --campaign-id $CAMPAIGN_ID
python -m alphaforge.burnin_ops --db $DB --json finalize --campaign-id $CAMPAIGN_ID --output-dir artifacts/burnin/final
```

### Bash (macOS/Linux)

```bash
export ALPHAFORGE_EXECUTION_MODE=PAPER
export EXECUTION_MODE=PAPER
export ALPHAFORGE_ENABLE_LIVE_EXECUTION=false
DB=artifacts/burnin/phase9.db
RELEASE_ID="phase9-$(git rev-parse --short HEAD)"
CAMPAIGN_ID='<campaign_id-from-launch-output>'

# A. Diagnose an existing database

python -m alphaforge.config_check
python -m alphaforge.config_fix --json
python -m alphaforge.burnin_ops --db "$DB" --json diagnose-db --max-heartbeat-age 120 | tee artifacts/burnin/database_diagnosis.json

# B. Start a new clean campaign

python -m alphaforge.binance_reconciliation_check --symbols BTCUSDT ETHUSDT | tee artifacts/burnin/reconciliation.json
python -m alphaforge.burnin_ops --db "$DB" --json preflight --release-id "$RELEASE_ID" --symbols BTCUSDT,ETHUSDT --intervals 1h --output-dir artifacts/burnin/preflight
python -m alphaforge.burnin_ops --db "$DB" --json launch --release-id "$RELEASE_ID" --duration-days 3 --symbols BTCUSDT,ETHUSDT --intervals 1h --detach
python -m alphaforge.burnin_ops --db "$DB" --json status --campaign-id "$CAMPAIGN_ID"
python -m alphaforge.burnin_ops --db "$DB" --json health --campaign-id "$CAMPAIGN_ID"
python -m alphaforge.burnin_ops --db "$DB" --json watch --campaign-id "$CAMPAIGN_ID"
python -m alphaforge.burnin_ops --db "$DB" --json audit --campaign-id "$CAMPAIGN_ID"
python -m alphaforge.burnin_ops --db "$DB" --json finalize --campaign-id "$CAMPAIGN_ID" --output-dir artifacts/burnin/final
```

Credential variables (`BINANCE_API_KEY` and `BINANCE_API_SECRET`) must be supplied through the normal environment/dotenv contract and must never be echoed. Accept reconciliation only when `evidence_status` is `COMPLETE`, `sanitized_errors` is empty, `unknown_unreconciled_symbols` is empty, and all endpoint statuses pass. A local diagnostic recovery is never authenticated exchange evidence. Run `recovery-drill` only after both the database diagnosis and authenticated reconciliation prove zero positions and zero pending orders.

## Phase A shadow agent graph

The graph is disabled by default and never owns an order decision. Replace the database path below with the configured runtime SQLite file.

### PowerShell

```powershell
# Enable/disable (restart runtime after changing configuration)
$env:ALPHAFORGE_AGENT_GRAPH_ENABLED = "true"
$env:ALPHAFORGE_AGENT_GRAPH_SHADOW = "true"
$env:ALPHAFORGE_AGENT_GRAPH_DATABASE_URL = "sqlite+pysqlite:///data/runtime/alphaforge_agent_shadow.db"
$env:ALPHAFORGE_AGENT_GRAPH_MAX_PENDING_RUNS = "64"
$env:ALPHAFORGE_AGENT_GRAPH_ENABLED = "false" # disable

$DB = "data/runtime/alphaforge_runtime.db"
sqlite3 $DB "SELECT correlation_id,decision_id,graph_status,shadow_only FROM agent_runs ORDER BY id DESC LIMIT 20;"
sqlite3 $DB "SELECT correlation_id,stage,status,primary_reason,skipped_reason FROM agent_stage_events ORDER BY id DESC LIMIT 40;"
# Confirm the shadow tables have no triggers and compare order/lifecycle counts before and after a shadow-only test.
sqlite3 $DB "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('agent_runs','agent_stage_events');"
sqlite3 $DB "SELECT (SELECT count(*) FROM orders) AS orders_count,(SELECT count(*) FROM trade_lifecycle_events) AS lifecycle_count;"
pytest -q tests/test_agent_contracts.py tests/test_agent_orchestrator.py tests/test_agent_persistence.py
pytest -q
```

### Bash (macOS/Linux)

```bash
export ALPHAFORGE_AGENT_GRAPH_ENABLED=true
export ALPHAFORGE_AGENT_GRAPH_SHADOW=true
export ALPHAFORGE_AGENT_GRAPH_DATABASE_URL=sqlite+pysqlite:///data/runtime/alphaforge_agent_shadow.db
export ALPHAFORGE_AGENT_GRAPH_MAX_PENDING_RUNS=64
export ALPHAFORGE_AGENT_GRAPH_ENABLED=false # disable

DB=data/runtime/alphaforge_runtime.db
sqlite3 "$DB" "SELECT correlation_id,decision_id,graph_status,shadow_only FROM agent_runs ORDER BY id DESC LIMIT 20;"
sqlite3 "$DB" "SELECT correlation_id,stage,status,primary_reason,skipped_reason FROM agent_stage_events ORDER BY id DESC LIMIT 40;"
sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('agent_runs','agent_stage_events');"
sqlite3 "$DB" "SELECT (SELECT count(*) FROM orders) AS orders_count,(SELECT count(*) FROM trade_lifecycle_events) AS lifecycle_count;"
pytest -q tests/test_agent_contracts.py tests/test_agent_orchestrator.py tests/test_agent_persistence.py
pytest -q
```

## PAPER Control Center backend

Canonical environment, PowerShell startup, read-first verification, and guarded pause/resume commands are documented in [`CONTROL_CENTER_RUNTIME_MAPPING.md`](CONTROL_CENTER_RUNTIME_MAPPING.md). The API is PAPER-only. It has no campaign stop endpoint because the burn-in CLI has no canonical stop command or STOPPED campaign state.
