## LIVE readiness observer v1 (read-only)

```bash
python -m alphaforge.live_readiness --db data/campaign/G6MANUAL01.db --campaign-id camp_xxx --json --output-dir artifacts/live_readiness/camp_xxx
```

This is observational only. It is not a LIVE authorization mechanism and not an execution controller; it opens the target SQLite database read-only and never starts, stops, or modifies a campaign.

# AlphaForge Komut Rehberi

> **Rehber bakım tarihi:** 2026-09-25, `dev` branch. Campaign/PAPER komutlarında `burnin_ops`; SQL tablo/kolon/ilişki ve audit sorgularında `docs/SQLcheat.md` kanonik kaynaktır.

Bu sayfa AlphaForge'u kurmak, güncellemek, test etmek, BACKTEST/PAPER çalıştırmak, dashboard açmak ve çok günlük PAPER burn-in kampanyasını yönetmek için doğrulanmış komutları tek yerde toplar.

> **Güvenlik:** AlphaForge varsayılan olarak LIVE-ready değildir. Bu rehber PAPER ve BACKTEST işletimine odaklanır. LIVE modu veya gerçek emir yolu, yerel readiness kanıtları ve bütün fail-closed güvenlik kapıları geçmeden açılmamalıdır.
>
> **SQL güvenlik kuralı:** `docs/SQLcheat.md` SQL için source of truth'tur. Bu dosyadaki gömülü SQL örnekleri yalnızca operasyon kolaylığı içindir; tablo/kolon/ilişki veya sorgu `SQLcheat.md` ile çelişirse `SQLcheat.md` esas alınır. Aktif PAPER DB incelemelerinde `sqlite3 -readonly` kullan; `no such table/column` hatasında önce `SQLcheat.md`, sonra `PRAGMA table_info(...)` ile doğrula.

---

## 1. Repo köküne geç

Bütün komutları repository kökünden çalıştır.

### macOS / Linux

```bash
cd AlphaForge
```

### Windows PowerShell

```powershell
Set-Location AlphaForge
```

Konumu doğrula:

```bash
pwd
git rev-parse --show-toplevel
git remote get-url origin
git branch --show-current
git status
```

PowerShell:

```powershell
Get-Location
git rev-parse --show-toplevel
git remote get-url origin
git branch --show-current
git status
```

---

## 2. `dev` branch'i güncelle

```bash
git switch dev
git pull origin dev
git status
```
```powershell
git switch dev
git fetch --no-auto-maintenance origin dev
git status
git log HEAD..origin/dev --oneline
```

Geçerli commit:

```bash
git rev-parse --short HEAD
git log -1 --oneline
```

Burn-in preflight temiz çalışma ağacı bekler. `git status` çıktısında commitlenmemiş değişiklik bırakma. Birden fazla AlphaForge çalışma kopyası varsa (`Public`, `Documents` vb.) DB veya campaign komutundan önce `git rev-parse --show-toplevel` ile doğru kopyada olduğunu doğrula.

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

Bu profil yalnızca BACKTEST içindir; PAPER burn-in veya preflight için kullanma.

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
cp .env.example .env
```

PowerShell:

```powershell
Copy-Item .env.example .env
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

Geriye uyumluluk alias'ı yalnızca eski kurulumlar içindir:

```text
EXECUTION_MODE=BACKTEST|PAPER|LIVE
```

Yeni kurulumda yalnızca canonical değişkeni kullan; stale alias'ı kaldır. `RUNTIME_LIMITS_ACTIVE` PAPER modundan otomatik olarak `true` türetilir ve `.env` içine ayrıca yazılmaz.

macOS / Linux:

```bash
export ALPHAFORGE_EXECUTION_MODE=PAPER
unset EXECUTION_MODE
```

PowerShell:

```powershell
$env:ALPHAFORGE_EXECUTION_MODE="PAPER"
Remove-Item Env:EXECUTION_MODE -ErrorAction SilentlyContinue
```

---

## 4.1 Temiz kurulum / ilk PAPER çalıştırma

Repo klonlama ve `dev` seçimi, sanal ortam kurulumu ve `.env.example` kopyalamasından sonra `.env` içine Binance **READ-ONLY** API credentials gir. Reconciliation açık olmalıdır. Secret değerlerini yazdırmadan, preflight/runtime ile aynı canonical dotenv/config yüklemesini doğrula. Şablondaki placeholder'lar secret değildir ve preflight bunlar değiştirilene kadar bilerek fail closed olur.

macOS / Linux:

```bash
python - <<'PY'
from alphaforge.config import load_config_from_env, load_reconciliation_settings
cfg = load_config_from_env()
recon = load_reconciliation_settings()
print(f"RECON={str(cfg.runtime.enable_binance_readonly_reconciliation).lower()}")
print(f"KEY={bool(recon.api_key.strip())}")
print(f"SECRET={bool(recon.api_secret.strip())}")
PY
```

PowerShell:

```powershell
@'
from alphaforge.config import load_config_from_env, load_reconciliation_settings
cfg = load_config_from_env()
recon = load_reconciliation_settings()
print(f"RECON={str(cfg.runtime.enable_binance_readonly_reconciliation).lower()}")
print(f"KEY={bool(recon.api_key.strip())}")
print(f"SECRET={bool(recon.api_secret.strip())}")
'@ | python
```

Beklenen çıktı yalnızca durum bilgisidir:

```text
RECON=true
KEY=True
SECRET=True
```

Sonra canonical schema ve PAPER akışını hazırla:

```bash
DB="data/runtime/alphaforge_runtime.db"
alembic upgrade head
python -m alphaforge.burnin_ops --db "$DB" db-doctor --check-only
```

PowerShell karşılığı:

```powershell
$DB = "data/runtime/alphaforge_runtime.db"
alembic upgrade head
python -m alphaforge.burnin_ops --db $DB db-doctor --check-only
```

Ardından preflight, `launch --detach`, launch çıktısından CID, status, health ve worker log kontrolü yap. Preflight authenticated signed read-only reconciliation tamamlanamazsa fail closed olur; hiçbir preflight adımı emir submit/cancel/amend etmez.

## 5. Veritabanını tanımla

Yeni kurulum varsayılan olarak `data/runtime/alphaforge_runtime.db` oluşturur/kullanır. Repo kökündeki `alphaforge.db` legacy ve non-canonicaldır; varsayılan akış onu yeni oluşturmaz, taşımaz veya silmez. Normal işletimde DB environment değişkenlerini elle eşitlemek gerekmez. Öncelik `--db`, `ALPHAFORGE_DATABASE_URL`, uyumluluk amaçlı `ALPHAFORGE_DB_PATH`, ardından canonical default sırasındadır.

### macOS / Linux

```bash
DB="data/runtime/alphaforge_runtime.db"
export DB
```

### Windows PowerShell

```powershell
$DB="data/runtime/alphaforge_runtime.db"
```

Dosyayı kontrol et. Özellikle kopyala-yapıştır sonrası `DB=\<...>` gibi literal kaçış/angle-bracket karakterlerinin değişkene girmediğini doğrula:

macOS / Linux:

```bash
printf 'DB shell-escaped: %q\n' "$DB"
ls -lh "$DB"
```

PowerShell:

```powershell
Get-Item $DB
```

SQLite bütünlük kontrolü:

```bash
sqlite3 -readonly "$DB" "PRAGMA integrity_check;"
```

PowerShell:

```powershell
sqlite3 -readonly $DB "PRAGMA integrity_check;"
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

## 7.1 Güvenli autonomous qualification: FAST + SOAK

`alphaforge.autonomous_qualification` normal PAPER campaign'ına bağlanan bir komut değildir. Her çalıştırmada seçilen output root altında yeni bir `alphaforge-qualification-*` klasörü, izole `qualification.sqlite3` ve ayrı artifacts dizini oluşturur. Harness süreç içinde PAPER modunu zorlar, LIVE order submission'ı kapatır ve production DB keşfi/aktif runtime reuse yapmaz.

> **Kural:** Aktif PAPER DB yolunu bu harness'e verme. CLI'da `--db` seçeneği yoktur. Public SOAK dış market-data erişilebilirliğini doğrular; `--market-data synthetic` yalnız offline/synthetic stabilite kanıtıdır ve public-feed release gate'inin yerine geçmez.

### 7.1.1 Exact SHA + test + FAST önkoşulu

SOAK'ı release edeceğin exact commit üzerinde çalıştır. Commit/config değişirse eski FAST/SOAK sonucu yeni SHA'yı qualify etmez.

```bash
git status --short
git rev-parse HEAD
pytest -q

OUT="/private/tmp/alphaforge-autonomous-qualification"
mkdir -p "$OUT"

.venv/bin/python -m alphaforge.autonomous_qualification \
  --mode fast \
  --output-root "$OUT"
FAST_RC=$?
echo "FAST_RC=$FAST_RC"
```

Exit code: `0=PASS`, `1=NEEDS_FIX`, `2=BLOCKED`. FAST `PASS` olmadan release SOAK'a geçme.

### 7.1.2 6 saatlik public SOAK — güvenli detached starter (macOS)

`--soak-hours` yalnız `6..24` kabul eder. Release-quality dış feed kanıtı için `public` kullan. Aşağıdaki starter exact SHA'yı kaydeder, dirty working tree'yi reddeder, SOAK'ı `nohup` ile terminalden ayırır, `caffeinate -w` ile yalnız SOAK PID yaşadığı sürece Mac'in uyumasını engeller ve başlangıçta process/artifact oluşumunu fail-fast doğrular.

> **Önkoşul:** Bu starter'ı yalnız aynı exact SHA üzerinde full test suite ve FAST `PASS` sonrasında çalıştır. SOAK devam ederken checkout/branch değiştirme, repo dosyalarını düzenleme veya aynı output root altında manuel dosya değiştirme.

```bash
set -euo pipefail

SHA="$(git rev-parse HEAD)"

if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: working tree dirty; final SOAK başlatılmadı"
  git status --short
  exit 1
fi

test -x .venv/bin/python || { echo "ERROR: .venv/bin/python bulunamadı"; exit 1; }
command -v caffeinate >/dev/null 2>&1 || { echo "ERROR: caffeinate bulunamadı"; exit 1; }

OUTROOT="/private/tmp/alphaforge-final-soak-${SHA:0:8}"
LOG="/private/tmp/alphaforge-final-soak-${SHA:0:8}.log"
CAFFEINE_LOG="/private/tmp/alphaforge-final-caffeinate-${SHA:0:8}.log"

mkdir -p "$OUTROOT"

echo "=== START FINAL 6H PUBLIC SOAK ==="
echo "SHA=$SHA"
echo "OUTROOT=$OUTROOT"
echo "LOG=$LOG"

nohup .venv/bin/python -u -m alphaforge.autonomous_qualification \
  --mode soak \
  --soak-hours 6 \
  --market-data public \
  --output-root "$OUTROOT" \
  >"$LOG" 2>&1 < /dev/null &

SOAK_PID=$!

nohup caffeinate -w "$SOAK_PID" \
  >"$CAFFEINE_LOG" 2>&1 < /dev/null &

CAFFEINE_PID=$!

ROOT=""
for _ in {1..30}; do
  if ! kill -0 "$SOAK_PID" 2>/dev/null; then
    echo "ERROR: SOAK process erken öldü"
    tail -100 "$LOG" || true
    exit 1
  fi
  ROOT="$(ls -td "$OUTROOT"/alphaforge-qualification-* 2>/dev/null | head -n 1 || true)"
  [ -n "$ROOT" ] && break
  sleep 1
done

if [ -z "$ROOT" ]; then
  echo "ERROR: qualification run directory 30 saniye içinde oluşmadı"
  tail -100 "$LOG" || true
  exit 1
fi

if ! kill -0 "$CAFFEINE_PID" 2>/dev/null; then
  echo "ERROR: caffeinate process erken öldü"
  cat "$CAFFEINE_LOG" || true
  exit 1
fi

echo
echo "=== FINAL SOAK IDENTITY ==="
echo "SOAK_PID=$SOAK_PID"
echo "CAFFEINE_PID=$CAFFEINE_PID"
echo "SHA=$SHA"
echo "ROOT=$ROOT"
echo "LOG=$LOG"
echo "CAFFEINE_LOG=$CAFFEINE_LOG"

echo
echo "=== PROCESS CHECK ==="
ps -p "$SOAK_PID" -o pid=,etime=,state=,command=
ps -p "$CAFFEINE_PID" -o pid=,etime=,state=,command=

echo
echo "=== FILES ==="
find "$ROOT" -maxdepth 2 -type f -print 2>/dev/null

echo
echo "=== LOG ==="
tail -30 "$LOG"
```

Harness 30 saniyede bir safety/resource sample alır; public market-data scan her 10 sample'da bir, yani yaklaşık 5 dakikada bir çalışır. Starter terminale geri döndükten sonra SOAK arka planda devam eder; izleme sorgularını ikinci terminalden çalıştır.

SOAK PID ve logları sonradan tekrar bulmak için starter çıktısındaki `SOAK_PID`, `ROOT` ve `LOG` değerlerini sakla. SOAK process'i bittiğinde `caffeinate -w` de kendiliğinden sona erer.

Offline karşılaştırma gerekiyorsa:

```bash
.venv/bin/python -m alphaforge.autonomous_qualification \
  --mode soak \
  --soak-hours 6 \
  --market-data synthetic \
  --output-root "$OUT"
```

Synthetic SOAK external exchange availability kanıtı değildir.

### 7.1.3 İzole SOAK DB/artifact yollarını bul

```bash
SHA="$(git rev-parse HEAD)"
OUTROOT="/private/tmp/alphaforge-final-soak-${SHA:0:8}"
RUN_DIR="$(ls -td "$OUTROOT"/alphaforge-qualification-* 2>/dev/null | head -n 1)"
[ -n "$RUN_DIR" ] || { echo "qualification run bulunamadı"; exit 1; }

QDB="$RUN_DIR/qualification.sqlite3"
SAMPLES="$RUN_DIR/artifacts/soak-resource-samples.jsonl"
REPORT="$RUN_DIR/artifacts/qualification-report.json"

printf 'RUN_DIR=%s\nQDB=%s\nSAMPLES=%s\nREPORT=%s\n' \
  "$RUN_DIR" "$QDB" "$SAMPLES" "$REPORT"
test -f "$QDB"
```

SOAK campaign kimliğini tahmin etme; izole DB'den al:

```bash
CID="$(sqlite3 -readonly "$QDB" "
SELECT campaign_id
FROM burnin_campaigns
WHERE release_id GLOB 'AQH-*-soak_normal_market_data'
ORDER BY created_at DESC
LIMIT 1;
")"

[ -n "$CID" ] || { echo "SOAK campaign henüz oluşmadı"; exit 1; }
echo "CID=$CID"
```

### 7.1.4 SOAK canlı durum — yalnız read-only

Aşağıdaki üç blok yalnız izole `QDB` üzerinde `sqlite3 -readonly` kullanır. Çalışan SOAK'a write, attach, migration, pause/resume veya runtime müdahalesi yapmaz.

#### SOAK STATUS — tek atımlık özet

`scans`, `empty_scans`, açık PAPER pozisyonu, canonical reject ve reject-label backlog sayaçlarını tek satırda gör:

```bash
sqlite3 -readonly -header -column "$QDB" "
WITH campaign_run_ids AS (
  SELECT burnin_run_id
  FROM burnin_campaign_runs
  WHERE campaign_id='$CID'
),
canonical AS (
  SELECT o.*
  FROM burnin_observations o
  JOIN campaign_run_ids r
    ON r.burnin_run_id=o.burnin_run_id
  WHERE json_valid(COALESCE(o.metrics_json,''))
    AND UPPER(COALESCE(json_extract(o.metrics_json,'$.observation_kind'),'CANONICAL_DECISION'))='CANONICAL_DECISION'
    AND NOT EXISTS (
      SELECT 1
      FROM burnin_observations newer
      WHERE newer.burnin_run_id=o.burnin_run_id
        AND newer.id < o.id
        AND COALESCE(
              json_extract(newer.metrics_json,'$.reject_decision_id'),
              json_extract(newer.metrics_json,'$.signal_id'),
              newer.observation_id
            ) = COALESCE(
              json_extract(o.metrics_json,'$.reject_decision_id'),
              json_extract(o.metrics_json,'$.signal_id'),
              o.observation_id
            )
        AND UPPER(COALESCE(json_extract(newer.metrics_json,'$.observation_kind'),'CANONICAL_DECISION'))='CANONICAL_DECISION'
    )
),
probes AS (
  SELECT
    CAST(json_extract(details_json,'$.probe_index') AS INTEGER) AS probe_index,
    CAST(json_extract(details_json,'$.row_count') AS INTEGER) AS row_count,
    json_extract(details_json,'$.status') AS status
  FROM burnin_campaign_events
  WHERE campaign_id='$CID'
    AND event_type='QUALIFICATION_MARKET_DATA_PROBE'
)
SELECT
  (SELECT campaign_status FROM burnin_campaigns WHERE campaign_id='$CID') AS campaign_status,
  (SELECT active_run_id FROM burnin_campaigns WHERE campaign_id='$CID') AS active_run_id,
  (SELECT COUNT(*) FROM probes) AS scans,
  COALESCE((SELECT SUM(CASE WHEN row_count=0 THEN 1 ELSE 0 END) FROM probes),0) AS empty_scans,
  COALESCE((SELECT status FROM probes ORDER BY probe_index DESC LIMIT 1),'NO_SCAN_YET') AS last_scan_status,
  COALESCE((SELECT row_count FROM probes ORDER BY probe_index DESC LIMIT 1),0) AS last_scan_rows,
  (SELECT COUNT(*) FROM burnin_pending_position_outcomes
   WHERE campaign_id='$CID' AND UPPER(status)='OPEN') AS open_positions,
  (SELECT COUNT(*) FROM canonical
   WHERE UPPER(COALESCE(decision,''))='REJECTED') AS rejects,
  (SELECT COUNT(*) FROM burnin_pending_reject_labels
   WHERE campaign_id='$CID') AS reject_labels_total,
  (SELECT COUNT(*) FROM burnin_pending_reject_labels
   WHERE campaign_id='$CID'
     AND UPPER(status) IN ('PENDING','READY','RESOLVING')) AS reject_pending;
"
```

#### SOAK WATCH — 30 saniyede bir

Aynı sayaçları 30 saniyede bir yenile. Durdurmak için `Ctrl+C`:

```bash
while true; do
  clear
  date
  echo "SOAK: $CID"
  echo

  sqlite3 -readonly -header -column "$QDB" "
WITH campaign_run_ids AS (
  SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id='$CID'
),
canonical AS (
  SELECT o.*
  FROM burnin_observations o
  JOIN campaign_run_ids r ON r.burnin_run_id=o.burnin_run_id
  WHERE json_valid(COALESCE(o.metrics_json,''))
    AND UPPER(COALESCE(json_extract(o.metrics_json,'$.observation_kind'),'CANONICAL_DECISION'))='CANONICAL_DECISION'
    AND NOT EXISTS (
      SELECT 1
      FROM burnin_observations newer
      WHERE newer.burnin_run_id=o.burnin_run_id
        AND newer.id < o.id
        AND COALESCE(json_extract(newer.metrics_json,'$.reject_decision_id'),json_extract(newer.metrics_json,'$.signal_id'),newer.observation_id)
          = COALESCE(json_extract(o.metrics_json,'$.reject_decision_id'),json_extract(o.metrics_json,'$.signal_id'),o.observation_id)
        AND UPPER(COALESCE(json_extract(newer.metrics_json,'$.observation_kind'),'CANONICAL_DECISION'))='CANONICAL_DECISION'
    )
),
probes AS (
  SELECT
    CAST(json_extract(details_json,'$.probe_index') AS INTEGER) AS probe_index,
    CAST(json_extract(details_json,'$.row_count') AS INTEGER) AS row_count,
    json_extract(details_json,'$.status') AS status
  FROM burnin_campaign_events
  WHERE campaign_id='$CID'
    AND event_type='QUALIFICATION_MARKET_DATA_PROBE'
)
SELECT
  (SELECT COUNT(*) FROM probes) AS scans,
  COALESCE((SELECT SUM(CASE WHEN row_count=0 THEN 1 ELSE 0 END) FROM probes),0) AS empty_scans,
  COALESCE((SELECT row_count FROM probes ORDER BY probe_index DESC LIMIT 1),0) AS last_scan_rows,
  COALESCE((SELECT status FROM probes ORDER BY probe_index DESC LIMIT 1),'NO_SCAN_YET') AS last_scan_status,
  (SELECT COUNT(*) FROM burnin_pending_position_outcomes
   WHERE campaign_id='$CID' AND UPPER(status)='OPEN') AS open_positions,
  (SELECT COUNT(*) FROM canonical
   WHERE UPPER(COALESCE(decision,''))='REJECTED') AS rejects,
  (SELECT COUNT(*) FROM burnin_pending_reject_labels
   WHERE campaign_id='$CID') AS reject_labels_total,
  (SELECT COUNT(*) FROM burnin_pending_reject_labels
   WHERE campaign_id='$CID'
     AND UPPER(status) IN ('PENDING','READY','RESOLVING')) AS reject_pending;
"

  sleep 30
done
```

`scans` yaklaşık 5 dakikada bir artar. `open_positions` bu qualification SOAK'ında normalde `0` kalmalıdır; harness decision probe rejection-biased ve no-submit tasarlanmıştır.

#### SOAK HEALTH — DB-backed güvenlik özeti

Campaign/run lineage, son runtime snapshot, heartbeat/reconciliation durumu, backlog ve persistence-failure sayısını tek satırda kontrol et:

```bash
sqlite3 -readonly -header -column "$QDB" "
WITH latest_state AS (
  SELECT *
  FROM runtime_state_snapshots
  WHERE campaign_id='$CID'
  ORDER BY id DESC
  LIMIT 1
),
probe AS (
  SELECT
    CAST(json_extract(details_json,'$.probe_index') AS INTEGER) AS probe_index,
    CAST(json_extract(details_json,'$.row_count') AS INTEGER) AS row_count,
    json_extract(details_json,'$.status') AS status,
    event_time
  FROM burnin_campaign_events
  WHERE campaign_id='$CID'
    AND event_type='QUALIFICATION_MARKET_DATA_PROBE'
  ORDER BY id DESC
  LIMIT 1
)
SELECT
  c.campaign_status,
  cr.status AS campaign_run_status,
  br.status AS burnin_run_status,
  c.last_heartbeat_at AS campaign_heartbeat_at,
  ls.runtime_status,
  ROUND(ls.heartbeat_age_sec,1) AS heartbeat_age_sec,
  ls.exchange_read_only_status,
  ls.reconciliation_status,
  ls.reconciliation_mismatch_count,
  ls.kill_switch_active,
  ls.recovery_action_required,
  ls.fail_closed_reason,
  ls.last_error AS runtime_last_error,
  COALESCE((SELECT status FROM probe),'NO_SCAN_YET') AS last_scan_status,
  COALESCE((SELECT row_count FROM probe),0) AS last_scan_rows,
  (SELECT COUNT(*) FROM burnin_pending_position_outcomes
   WHERE campaign_id='$CID' AND UPPER(status)='OPEN') AS open_positions,
  (SELECT COUNT(*) FROM burnin_pending_reject_labels
   WHERE campaign_id='$CID'
     AND UPPER(status) IN ('PENDING','READY','RESOLVING')) AS reject_backlog,
  (SELECT COUNT(*) FROM exchange_reconciliation_events e
   WHERE e.instance_id=ls.instance_id
     AND e.status='PERSISTENCE_FAILED') AS soak_persistence_failures,
  c.last_error AS campaign_last_error
FROM burnin_campaigns c
LEFT JOIN burnin_campaign_runs cr
  ON cr.campaign_id=c.campaign_id AND cr.burnin_run_id=c.active_run_id
LEFT JOIN burnin_runs br
  ON br.burnin_run_id=c.active_run_id
LEFT JOIN latest_state ls ON 1=1
WHERE c.campaign_id='$CID';
"
```

Sağlıklı çalışan SOAK sırasında beklenen ana durum: campaign/run/mapping `RUNNING`, runtime `OPERATING`, heartbeat fresh, `exchange_read_only_status` available/healthy, reconciliation `CLEAN`, mismatch `0`, `open_positions=0`, SOAK runtime'ına scoped `soak_persistence_failures=0`, fail-closed/recovery alanları boş olmalı. Public probe geçici olarak empty olabilir; final gate ayrıca recovery, empty-rate ve consecutive-empty kurallarını uygular.

> **Önemli:** 30 saniyelik resource/safety invariant'ları SQLite'ta authoritative değildir. RSS/DB/artifact growth, queue-depth ve bütün sample invariant'ları için `soak-resource-samples.jsonl`; final verdict için `qualification-report.json` esas alınır.


Campaign/run durumu:

```bash
sqlite3 -readonly -header -column "$QDB" "
SELECT
  c.campaign_id,
  c.release_id,
  c.campaign_status,
  c.active_run_id,
  c.last_heartbeat_at,
  c.last_error,
  cr.continuation_sequence,
  cr.status AS campaign_run_status,
  br.status AS burnin_run_status
FROM burnin_campaigns c
LEFT JOIN burnin_campaign_runs cr
  ON cr.campaign_id=c.campaign_id AND cr.burnin_run_id=c.active_run_id
LEFT JOIN burnin_runs br
  ON br.burnin_run_id=c.active_run_id
WHERE c.campaign_id='$CID';
"
```

Public market-data probe özeti:

```bash
sqlite3 -readonly -header -column "$QDB" "
WITH p AS (
  SELECT
    CAST(json_extract(details_json,'$.probe_index') AS INTEGER) AS probe_index,
    CAST(json_extract(details_json,'$.row_count') AS INTEGER) AS row_count,
    json_extract(details_json,'$.status') AS status,
    event_time
  FROM burnin_campaign_events
  WHERE campaign_id='$CID'
    AND event_type='QUALIFICATION_MARKET_DATA_PROBE'
), x AS (
  SELECT
    *,
    LAG(CASE WHEN row_count=0 THEN 1 ELSE 0 END)
      OVER (ORDER BY probe_index) AS previous_empty
  FROM p
)
SELECT
  COUNT(*) AS probes,
  SUM(CASE WHEN row_count=0 THEN 1 ELSE 0 END) AS empty_probes,
  MAX(1, CAST(COUNT(*)/20 AS INTEGER)) AS allowed_empty_probes,
  ROUND(100.0*SUM(CASE WHEN row_count=0 THEN 1 ELSE 0 END)
        / NULLIF(COUNT(*),0), 2) AS empty_pct,
  SUM(CASE WHEN row_count=0 AND previous_empty=1 THEN 1 ELSE 0 END)
    AS consecutive_empty_pairs,
  (SELECT status FROM p ORDER BY probe_index DESC LIMIT 1) AS last_status,
  (SELECT row_count FROM p ORDER BY probe_index DESC LIMIT 1) AS last_row_count
FROM x;
"
```

Son probe/recovery kayıtları:

```bash
sqlite3 -readonly -header -column "$QDB" "
SELECT
  id, event_time, event_type,
  json_extract(details_json,'$.probe_index') AS probe_index,
  json_extract(details_json,'$.row_count') AS row_count,
  json_extract(details_json,'$.status') AS status,
  json_extract(details_json,'$.latency_seconds') AS latency_seconds,
  json_extract(details_json,'$.empty_probe_count') AS recovered_empty_count,
  json_extract(details_json,'$.cause') AS cause,
  json_extract(details_json,'$.error_class') AS error_class,
  json_extract(details_json,'$.http_status') AS http_status
FROM burnin_campaign_events
WHERE campaign_id='$CID'
  AND event_type IN ('QUALIFICATION_MARKET_DATA_PROBE','QUALIFICATION_MARKET_DATA_RECOVERED')
ORDER BY id DESC
LIMIT 20;
"
```

Resource/safety samples SQL tablosunda değildir; canonical canlı örnek dosyası `soak-resource-samples.jsonl`'dır. Son sample'ı kompakt gör:

```bash
python - "$SAMPLES" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
rows = [line for line in path.read_text().splitlines() if line.strip()]
if not rows:
    raise SystemExit("henüz SOAK sample yok")
s = json.loads(rows[-1])
failed = [name for name, passed in s.get("invariants", {}).items() if not passed]
print("at=", s.get("at"))
print("elapsed_seconds=", s.get("elapsed_seconds"))
print("heartbeat_age_seconds=", s.get("heartbeat_age_seconds"))
print("reconciliation_status=", s.get("reconciliation_status"))
print("resolver_status=", s.get("resolver_status"))
print("pending_resolver_backlog=", s.get("pending_resolver_backlog"))
print("pending_reject_backlog=", s.get("pending_reject_backlog"))
print("queue_depths=", s.get("queue_depths"))
print("paper_executions=", s.get("paper_executions"))
print("failed_invariants=", failed)
PY
```

### 7.1.5 SOAK tamamlanınca final raporu oku

```bash
test -f "$REPORT" || { echo "final report henüz yok"; exit 1; }

python - "$REPORT" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    r = json.load(f)

soak = next((x for x in r.get("faults_injected", [])
             if x.get("name") == "soak_normal_market_data"), {})
obs = soak.get("observed_behavior", {})
resources = r.get("soak_resources") or {}

print("overall_verdict=", r.get("overall_verdict"))
print("mode=", r.get("mode"))
print("duration_seconds=", r.get("actual_wall_clock_duration_seconds"))
print("isolation=", r.get("isolation"))
print("soak_verdict=", soak.get("verdict"))
print("market_data_probes=", len(obs.get("market_data_probes") or []))
print("empty_market_data_probes=", obs.get("empty_market_data_probes"))
print("max_consecutive_empty_market_data_probes=",
      obs.get("max_consecutive_empty_market_data_probes"))
print("growth_flags=", resources.get("growth_flags"))
print("resource_sample_count=", resources.get("sample_count"))
print("invariant_failures=", r.get("invariant_failures"))
print("soak_sample_invariant_failures=", r.get("soak_sample_invariant_failures"))
print("persistence_gaps=", r.get("persistence_gaps"))
print("unexplained_state_transitions=", r.get("unexplained_state_transitions"))
print("campaign_run_lineage_consistency=", r.get("campaign_run_lineage_consistency"))
print("soak_checks=")
for name, passed in sorted((soak.get("invariant_checks") or {}).items()):
    print(f"  {name}={passed}")
PY
```

### 7.1.6 Public SOAK PASS gate'i

Harness'in mevcut gate mantığında release-quality public SOAK için en az şunlar birlikte sağlanmalıdır:

- full wall-clock süre tamamlanmış olmalı;
- normal market data görülmüş ve final probe available olmalı;
- `max_consecutive_empty_market_data_probes <= 1` olmalı; yani boş bir 5 dakikalık probe bir sonraki probe'da toparlanmalı;
- empty probe sayısı `<= max(1, probes // 20)` olmalı;
- bütün 30 saniyelik safety invariant sample'ları geçmeli;
- reconciliation her sample arasında CLEAN kalmalı, resolver sağlıklı olmalı ve worker canlı kalmalı;
- harness SOAK decision probe canonical PAPER evidence üretmeli fakat order submit/PAPER execution oluşturmamalı;
- resource `growth_flags` boş olmalı ve monitor error olmamalı;
- persistence gap, unexplained worker transition veya campaign/run lineage drift olmamalı.

Resource gate ayrıca RSS growth `>128 MiB`, artifact growth `>64 MiB`, DB growth `>max(64 MiB, 160 KiB × sample_count)`, son 12 sample'da `>8 MiB` sürekli artan RSS ve queue/backlog `>32` durumlarını flag eder.

Full repository suite + FAST + full **public** SOAK aynı exact code/config üzerinde `PASS` olmadan bunu release qualification olarak kullanma. Bu gate LIVE trading yetkisi vermez; yalnız yeni gerçek PAPER burn-in'e geçiş için kanıttır.

### 7.1.7 SOAK'ı erken durdurma

Foreground harness için terminalde:

```text
Ctrl+C
```

Harness `finally` cleanup yolunda aktif qualification context'lerini terminalize eder ve process-local environment'ı geri yükler. Ancak erken kesilmiş run tam wall-clock gate'ini karşılamaz ve geçerli `PASS` olarak kullanılamaz; exact SHA/config üzerinde baştan tam SOAK çalıştır.

SOAK SQL sorgularının daha ayrıntılı ve kanonik kopyası için `docs/SQLcheat.md` içindeki **Autonomous qualification / SOAK isolated database** bölümünü kullan.

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
ALPHAFORGE_EXECUTION_MODE=PAPER python -m alphaforge.runtime
```

PowerShell:

```powershell
$env:ALPHAFORGE_EXECUTION_MODE="PAPER"
python -m alphaforge.runtime
```

### Güvenli placeholder scanner ile deterministik smoke

macOS / Linux:

```bash
ALPHAFORGE_EXECUTION_MODE=PAPER ALPHAFORGE_RUNTIME_SAFE_SCANNER=1 python -m alphaforge.runtime
```

PowerShell:

```powershell
$env:ALPHAFORGE_EXECUTION_MODE="PAPER"
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


## M0 fresh PAPER qualification baseline

Bu bölüm, fresh M0 PAPER qualification kampanyasını current validated source üzerinde başlatmak için tek parça operator akışıdır. Amaç trade sayısını artırmak veya threshold gevşetmek değil; current strategy/runtime davranışını, execution-cost gerçekçiliğini, reject attribution'ı ve qualification blocker'larını temiz bir campaign lineage üzerinde ölçmektir.

> **Güvenlik sınırı:** PAPER dışında çalıştırma. `ALPHAFORGE_ENABLE_LIVE_TRADING=false` ve `ALPHAFORGE_ALLOW_LIVE_ORDERS=false` kalmalıdır. Accepted trade sayısını artırmak için score/RR/execution threshold'larını değiştirme.
>
> **Source branch kontratı:** Current `burnin_ops preflight` yalnız temiz `dev` veya release ID'ye worktree fingerprint'i bağlanmış `feature/*` branch'ini kabul eder. `main` aynı tree'yi içerse bile doğrudan M0 preflight source branch'i olarak kullanılmaz. Bu kontrat değişirse bu bölüm güncellenmelidir.
>
> **SQL:** Campaign/decision/reject/qualification sorgularında `docs/SQLcheat.md` kanonik kaynaktır.
>
> **SQLite path:** macOS'ta operator shell'de relative path ile `sqlite3 -readonly "$DB"` açılış hatası görülürse DB'yi tahmin etme veya `-readonly` kaldırma. Bu akış baştan absolute DB path kullanır.

### M0.1 Exact source ve PAPER ortamı

Current M0 baseline her launch öncesinde **o anda remote `origin/dev` üzerinde bulunan exact SHA'ya** pinlenir. Dokümana sabit SHA yazılmaz; aksi halde yalnız docs veya safety-fix commit'i bile runbook'u sessizce eski source'a sabitleyebilir.

```bash
source .venv/bin/activate

git fetch origin
git switch dev
git pull --ff-only origin dev

EXPECTED_SHA="$(git rev-parse origin/dev)"

echo "=== REPO ==="
pwd
git branch --show-current
git rev-parse HEAD
git status --short
echo "EXPECTED_SHA=$EXPECTED_SHA"

test "$(git branch --show-current)" = "dev" || {
  echo "ERROR: M0 baseline yalnız temiz dev branch'inden başlatılır"
  exit 1
}

test "$(git rev-parse HEAD)" = "$EXPECTED_SHA" || {
  echo "ERROR: local HEAD origin/dev exact SHA ile eşleşmiyor"
  exit 1
}

test -z "$(git status --porcelain)" || {
  echo "ERROR: working tree temiz değil"
  git status --short
  exit 1
}

export ALPHAFORGE_EXECUTION_MODE=PAPER
unset EXECUTION_MODE
export ALPHAFORGE_ENABLE_LIVE_TRADING=false
export ALPHAFORGE_ALLOW_LIVE_ORDERS=false

REPO_ROOT="$(git rev-parse --show-toplevel)"
SHA8="$(git rev-parse --short=8 HEAD)"
STAMP="$(date -u +%Y%m%d)"
RELEASE_ID="M0_${STAMP}_${SHA8}"
DB="$REPO_ROOT/data/campaign/${RELEASE_ID}.db"

export EXPECTED_SHA RELEASE_ID DB

echo "EXPECTED_SHA=$EXPECTED_SHA"
echo "RELEASE_ID=$RELEASE_ID"
echo "DB=$DB"
```

Bu akışta `main` ile tree eşitliği M0 başlangıç şartı değildir. M0 issue'sunun otoritesi current `dev`dir; preflight ayrıca source branch/worktree identity'yi doğrular. Promotion state ayrıca release/promotion sürecinde değerlendirilir.

Fresh M0 release kimliği dinamik olarak `M0_<UTC tarih>_<dev SHA8>` biçimindedir. Böylece her yeni `dev` source identity eski campaign evidence'inden ayrılır.

İlk preflight öncesinde aynı DB zaten varsa fresh evidence ile historical evidence'i karıştırma:

```bash
if [ -e "$DB" ]; then
  echo "ERROR: Fresh M0 DB zaten var: $DB"
  exit 1
fi
```

Yeni bir M0 baseline başlatırken `EXPECTED_SHA` doğrudan `origin/dev`den türetilir; tarih ve `RELEASE_ID` bu exact source identity ile birlikte yenilenir. Eski DB/release ID reuse edilmez.

### M0.2 Config ve read-only reconciliation doğrulaması

Secret değerlerini yazdırmadan canonical config'i kontrol et:

```bash
python - <<'PY'
from alphaforge.config import load_config_from_env, load_reconciliation_settings

cfg = load_config_from_env()
recon = load_reconciliation_settings()

print("EXECUTION_MODE =", cfg.runtime.execution_mode)
print("READONLY_RECON =", cfg.runtime.enable_binance_readonly_reconciliation)
print("API_KEY_PRESENT =", bool(recon.api_key.strip()))
print("API_SECRET_PRESENT =", bool(recon.api_secret.strip()))
print("LIVE_EXECUTION =", getattr(cfg.runtime, "enable_live_execution", False))
PY
```

Ardından:

```bash
python -m alphaforge.config_check
```

M0 için beklenen davranış:

- execution mode `PAPER`
- signed read-only reconciliation enabled
- API key/secret present, secret değerleri loglanmıyor
- LIVE execution disabled
- Binance production read-only market/reconciliation endpoints consistent
- threshold/config identity current runtime ile aynı

### M0.3 Preflight, schema ve SQLite integrity

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  preflight \
  --release-id "$RELEASE_ID" \
  --symbols BTCUSDT,ETHUSDT \
  --intervals 1h \
  --output-dir "artifacts/burnin/preflight_${RELEASE_ID}"
```

**Preflight `PASS` olmadan launch yapma.**

Current M0 baseline'ta özellikle aşağıdaki gate'ler PASS olmalıdır:

- `release_id_reserved_namespace_free`
- `env_contract_valid`
- `signed_readonly_reconciliation_available`
- `git_commit_known`
- `source_branch_allowed`
- `source_worktree_identity_bound`
- `execution_mode_paper`
- `live_mutation_path_disabled`
- `schema_current`
- `runtime_identity_matches_campaign_identity`
- `execution_cost_identity_complete`
- `source_provenance_present`
- `market_data_endpoint_consistent`
- `no_duplicate_active_campaign`
- `no_stale_worker_occupying_campaign`
- `runtime_recovery_scope`
- `binance_readonly_klines_reachable`
- `clock_skew_acceptable`

Schema kontrolü:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  db-doctor \
  --check-only
```

SQLite integrity:

```bash
sqlite3 -readonly "$DB" "PRAGMA integrity_check;"
```

Beklenen:

```text
ok
```

`sqlite3` database open error verirse önce absolute path'i doğrula; write mode'a geçme:

```bash
printf 'DB=%s\n' "$DB"
test -f "$DB" || { echo "ERROR: DB bulunamadı"; exit 1; }
ls -lh "$DB"
sqlite3 -readonly "$DB" "PRAGMA integrity_check;"
```

### M0.4 Fresh 7-day campaign launch

M0 campaign identity interval'i `1h` olarak tutulur. Guided MTF runtime config içinde `1h regime -> 15m setup -> 1m execution` olarak çalışır; campaign CLI'ya sırf MTF aktif diye `1h,15m,1m` yazılmaz.

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  launch \
  --release-id "$RELEASE_ID" \
  --duration-days 7 \
  --symbols BTCUSDT,ETHUSDT \
  --intervals 1h \
  --detach \
  --attach-timeout-seconds 120
```

Campaign ID'yi tahmin etme; DB'den al:

```bash
CID="$(sqlite3 -readonly "$DB" "
SELECT campaign_id
FROM burnin_campaigns
WHERE release_id='$RELEASE_ID'
ORDER BY created_at DESC
LIMIT 1;
")"

RUN_ID="$(sqlite3 -readonly "$DB" "
SELECT active_run_id
FROM burnin_campaigns
WHERE campaign_id='$CID';
")"

export CID RUN_ID

echo "RELEASE_ID=$RELEASE_ID"
echo "DB=$DB"
echo "CID=$CID"
echo "RUN_ID=$RUN_ID"
```

### M0.5 İlk status / health / worker log kontrolü

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  status \
  --campaign-id "$CID"

python -m alphaforge.burnin_ops \
  --db "$DB" \
  health \
  --campaign-id "$CID"
```

JSON gerekiyorsa:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  --json \
  status \
  --campaign-id "$CID"

python -m alphaforge.burnin_ops \
  --db "$DB" \
  --json \
  health \
  --campaign-id "$CID"
```

Worker logları:

```bash
echo "=== STDERR ==="
tail -n 100 "artifacts/burnin/$CID/worker.stderr.log"

echo
echo "=== STDOUT ==="
tail -n 100 "artifacts/burnin/$CID/worker.stdout.log"
```

Canlı stderr:

```bash
tail -f "artifacts/burnin/$CID/worker.stderr.log"
```

### M0.6 Initial evidence snapshot

Campaign lineage ve temel sayaçlar:

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT
    c.campaign_id,
    c.release_id,
    c.campaign_status,
    c.active_run_id,
    c.last_heartbeat_at,
    c.worker_pid,
    c.qualification_status,
    c.evidence_completeness_status,
    (
      SELECT COUNT(*)
      FROM burnin_observations o
      JOIN burnin_campaign_runs cr
        ON cr.burnin_run_id=o.burnin_run_id
      WHERE cr.campaign_id=c.campaign_id
    ) AS observations,
    (
      SELECT COUNT(*)
      FROM burnin_pending_reject_labels p
      WHERE p.campaign_id=c.campaign_id
    ) AS reject_labels,
    (
      SELECT COUNT(*)
      FROM burnin_pending_position_outcomes p
      WHERE p.campaign_id=c.campaign_id
    ) AS positions,
    (
      SELECT COUNT(*)
      FROM burnin_qualification_snapshots q
      WHERE q.campaign_id=c.campaign_id
    ) AS qualification_snapshots,
    c.last_error
FROM burnin_campaigns c
WHERE c.campaign_id='$CID';
"
```

Canonical decision funnel:

```bash
sqlite3 -readonly -header -column "$DB" "
WITH campaign_run_ids AS (
  SELECT burnin_run_id
  FROM burnin_campaign_runs
  WHERE campaign_id='$CID'
),
canonical AS (
  SELECT o.*
  FROM burnin_observations o
  JOIN campaign_run_ids r
    ON r.burnin_run_id=o.burnin_run_id
  WHERE json_valid(COALESCE(o.metrics_json,''))
    AND UPPER(
      COALESCE(
        json_extract(o.metrics_json,'$.observation_kind'),
        'CANONICAL_DECISION'
      )
    )='CANONICAL_DECISION'
    AND NOT EXISTS (
      SELECT 1
      FROM burnin_observations newer
      WHERE newer.burnin_run_id=o.burnin_run_id
        AND newer.id < o.id
        AND COALESCE(
              json_extract(newer.metrics_json,'$.reject_decision_id'),
              json_extract(newer.metrics_json,'$.signal_id'),
              newer.observation_id
            )
          =
            COALESCE(
              json_extract(o.metrics_json,'$.reject_decision_id'),
              json_extract(o.metrics_json,'$.signal_id'),
              o.observation_id
            )
        AND UPPER(
          COALESCE(
            json_extract(newer.metrics_json,'$.observation_kind'),
            'CANONICAL_DECISION'
          )
        )='CANONICAL_DECISION'
    )
)
SELECT
  decision,
  COUNT(*) AS n
FROM canonical
GROUP BY decision
ORDER BY n DESC;
"
```

Score / raw RR / effective RR / drag:

```bash
sqlite3 -readonly -header -column "$DB" "
WITH ids AS (
  SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') AS signal_id
  FROM burnin_observations o
  JOIN burnin_campaign_runs cr
    ON cr.burnin_run_id=o.burnin_run_id
  WHERE cr.campaign_id='$CID'
)
SELECT
  COUNT(*) AS rows,
  COUNT(DISTINCT d.decision_id) AS decisions,
  ROUND(MIN(d.score),4) AS min_score,
  ROUND(AVG(d.score),4) AS avg_score,
  ROUND(MAX(d.score),4) AS max_score,
  ROUND(MIN(d.rr),4) AS min_raw_rr,
  ROUND(AVG(d.rr),4) AS avg_raw_rr,
  ROUND(MAX(d.rr),4) AS max_raw_rr,
  ROUND(MIN(d.effective_rr),4) AS min_effective_rr,
  ROUND(AVG(d.effective_rr),4) AS avg_effective_rr,
  ROUND(MAX(d.effective_rr),4) AS max_effective_rr,
  ROUND(AVG(d.rr-d.effective_rr),4) AS avg_rr_drag
FROM order_decisions d
JOIN ids ON ids.signal_id=d.signal_id;
"
```

Reject reason distribution:

```bash
sqlite3 -readonly -header -column "$DB" "
WITH r AS (
  SELECT
    COALESCE(
      d.reject_reason,
      json_extract(o.metrics_json,'$.primary_reject_reason'),
      json_extract(o.metrics_json,'$.reject_reason'),
      'UNKNOWN'
    ) AS reject_reason
  FROM burnin_observations o
  JOIN burnin_campaign_runs cr
    ON cr.burnin_run_id=o.burnin_run_id
  LEFT JOIN order_decisions d
    ON d.decision_id=json_extract(o.metrics_json,'$.reject_decision_id')
  WHERE cr.campaign_id='$CID'
    AND UPPER(COALESCE(o.decision,''))='REJECTED'
),
g AS (
  SELECT reject_reason,COUNT(*) AS n
  FROM r
  GROUP BY reject_reason
)
SELECT
  reject_reason,
  n,
  ROUND(100.0*n/SUM(n) OVER (),2) AS pct
FROM g
ORDER BY n DESC,reject_reason;
"
```

Reject resolver/integrity status:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  reject-label-status \
  --campaign-id "$CID"
```

### M0.7 Qualification blocker snapshot

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT
  qualification_id,
  burnin_run_id,
  release_id,
  generated_at,
  status,
  sample_status,
  expectancy_status,
  execution_status,
  regime_status,
  reject_quality_status,
  calibration_status,
  drawdown_status,
  concentration_status,
  reconciliation_status,
  evidence_completeness_status,
  blockers_json,
  warnings_json
FROM burnin_qualification_snapshots
WHERE campaign_id='$CID'
   OR burnin_run_id IN (
       SELECT burnin_run_id
       FROM burnin_campaign_runs
       WHERE campaign_id='$CID'
   )
ORDER BY generated_at DESC,id DESC
LIMIT 1;
"
```

Bu snapshot'ta minimum-duration, decision, accepted, closed-trade ve mature reject outcome blocker'larının başlangıçta bulunması normaldir. Bunları kaldırmak için threshold gevşetme. Operasyonel/plumbing blocker ile salt sample insufficiency blocker'ını ayrı değerlendir.

### M0.7a Final qualification release-evidence refresh

#461 sonrası Phase 6/7 release gate kanıtları doğrudan SQL ile "PASS" yazılarak üretilemez. Final M0 qualification penceresinde canonical ölçüm/yazıcılar kullanılmalıdır.

> **Zamanlama:** Bu adımları campaign daha yeni başlarken sırf blocker listesini temizlemek için çalıştırma. Özellikle rollback evidence bounded-freshness taşır ve operator acknowledgement en fazla 240 dakika geçerlidir. Campaign sample/duration gereksinimleri tamamlanmaya yaklaştığında, final qualification/finalize öncesinde çalıştır.

Önce local checkout hâlâ campaign source identity ile birebir eşleşiyor mu doğrula:

```bash
test "$(git branch --show-current)" = "dev" || {
  echo "ERROR: release evidence yalnız pinned dev checkout üzerinde üretilir"
  exit 1
}

test "$(git rev-parse HEAD)" = "$EXPECTED_SHA" || {
  echo "ERROR: checkout campaign EXPECTED_SHA ile eşleşmiyor"
  exit 1
}

test -z "$(git status --porcelain)" || {
  echo "ERROR: release evidence için working tree temiz olmalı"
  exit 1
}
```

Deterministic rollback readiness evidence üret:

```bash
python -m alphaforge.rollback_evidence   --database-url "sqlite+pysqlite:///$DB"
```

Bu komut gerçek order göndermeden kill-switch/no-submit/reconciliation/non-mutating-repair davranışını deterministic harness ile ölçer. `evidence_status=COMPLETE` olmadan devam etme.

Rollback verification + tracked runbook evidence'ını campaign/release identity'ye bağla:

```bash
python -m alphaforge.release_safety_evidence   --db "$DB"   --campaign-id "$CID"   --phase PHASE6   --runbook RUNBOOK.md   --rollback-max-age-sec 900   --repo .
```

Measured mutation-trap canary evidence üret:

```bash
python -m alphaforge.release_canary_evidence   --db "$DB"   --campaign-id "$CID"   --phase PHASE6   --repo .
```

Exact campaign commit için GitHub Actions **push** full-test evidence'ını doğrula ve release snapshot'a yaz:

```bash
python -m alphaforge.release_ci_evidence   --db "$DB"   --campaign-id "$CID"   --phase PHASE6   --repository werim/AlphaForge
```

Bu gate yalnız campaign'in exact `git_commit` SHA'sına ait canonical push workflow'da aşağıdaki adımlar gerçekten `success` ise PASS verir:

- Full regression suite
- Protected safety mutation gate
- Run offline backtest
- Verify backtest outputs

PR workflow'unda full regression `skipped` ise bu kanıt olarak kullanılamaz.

Operator acknowledgement **otomatik üretilemez**. Önce required statement'ı göster:

```bash
python -m alphaforge.release_operator_ack   --db "$DB"   --campaign-id "$CID"   --phase PHASE6   --show-required-text
```

Statement'ı gerçekten okuyup kabul ediyorsan, çıktıyı bilinçli olarak `--acknowledgement-text` ile gir. Örnek kabuk değişkeni:

```bash
ACK_TEXT="$(python -m alphaforge.release_operator_ack   --db "$DB"   --campaign-id "$CID"   --phase PHASE6   --show-required-text)"

printf '%s\n' "$ACK_TEXT"
```

Burada durup metni insan olarak doğrula. Kabul ettikten sonra:

```bash
python -m alphaforge.release_operator_ack   --db "$DB"   --campaign-id "$CID"   --phase PHASE6   --ttl-minutes 240   --acknowledgement-text "$ACK_TEXT"
```

Bu acknowledgement final qualification penceresine yakın üretilmelidir; 240 dakika sonrası expire olur. Script kendi kendine acknowledgement oluşturmaz veya submit etmez.

Ardından yeni qualification snapshot'ında release-gate blocker'larının canonical evidence ile kalktığını doğrula. Direct SQL ile rollback/runbook/full-test/operator-ack/canary PASS satırı eklemek yasaktır; read-path provenance doğrulaması bu tür optimistic satırları zaten `UNVERIFIED` olarak bloke eder.

### M0.8 Safe pause / resume

Normal operator pause:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  pause \
  --campaign-id "$CID"
```

Doğrula:

```bash
python -m alphaforge.burnin_ops --db "$DB" status --campaign-id "$CID"
python -m alphaforge.burnin_ops --db "$DB" health --campaign-id "$CID"
```

Aynı release/config/strategy/universe/execution-cost identity korunuyorsa resume:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  resume \
  --campaign-id "$CID"
```

Identity değiştiyse resume zorlama; fresh preflight + fresh campaign oluştur.

### M0.9 Audit ve rapor

Campaign ilerlerken:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  audit \
  --campaign-id "$CID"
```

Daily report:

```bash
REPORT_DIR="artifacts/burnin/$CID/daily_$(date -u +%Y%m%dT%H%M%SZ)"

python -m alphaforge.burnin_ops \
  --db "$DB" \
  report \
  --campaign-id "$CID" \
  --output-dir "$REPORT_DIR"
```

Finalize yalnız campaign süresi/evidence gereksinimleri gerçekten tamamlandığında çalıştırılır; blocker'ları bypass etmek için kullanılmaz.


---


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
python -m alphaforge.burnin_ops diagnose-db --help
python -m alphaforge.burnin_ops db-doctor --help
python -m alphaforge.burnin_ops recover-runtime --help
python -m alphaforge.burnin_ops reject-label-status --help
python -m alphaforge.db_doctor --help
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
RELEASE_ID="POSHARNESS01"
DB="data/campaign/$RELEASE_ID.db"
python -m alphaforge.burnin_ops \
  --db "$DB" \
  preflight \
  --release-id "$RELEASE_ID" \
  --symbols BTCUSDT,ETHUSDT \
  --intervals 1h
```

PowerShell:

```powershell
$RELEASE_ID="2908T01"

python -m alphaforge.burnin_ops `
  --db $DB `
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

Canonical `.env.example` kullanıldığında PAPER mode, runtime-limit türetimi ve candidate/runtime config hash aynı kaynaktan gelir. Gerçek read-only Binance credential girilmemişse yalnızca credential/reconciliation kapılarının fail-closed kalması beklenir. `ALPHAFORGE_ENABLE_LIVE_TRADING=false` ve `ALPHAFORGE_ALLOW_LIVE_ORDERS=false` değerlerini değiştirme; PAPER preflight hiçbir submit/cancel/amend çağrısı yapmaz.

---

## 13. Çok günlük kampanyayı başlat

### Detached worker ile önerilen çalıştırma

macOS / Linux:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  launch \
  --release-id "$RELEASE_ID" \
  --duration-days 7 \
  --symbols BTCUSDT,ETHUSDT \
  --intervals 1h \
  --detach \
  --attach-timeout-seconds 60
```

PowerShell:
```
python -m alphaforge.burnin_ops `
  --db $DB `
  launch `
  --release-id $RELEASE_ID `
  --symbols BTCUSDT `
  --intervals 1h `
  --duration-days 7 `
  --detach

```

Komut çıktısındaki gerçek `campaign_id` değerini kaydet.

macOS / Linux örneği:

```bash
CID="camp_xxxxxxxxxxxxxxxx"
export CID
```

PowerShell örneği:

```powershell
$CID="camp_xxxxxxxxxxxxxxxx"
```

> `cid` tahmin edilmez. Launch çıktısından veya SQL sorgusundan alınır.

---

## 14. Son kampanya ID'sini SQL'den bul

```bash
sqlite3 -readonly "$DB" <<'SQL'
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
sqlite3 -readonly $DB "SELECT campaign_id,release_id,campaign_status,active_run_id,worker_pid,created_at,last_heartbeat_at,last_error FROM burnin_campaigns ORDER BY created_at DESC LIMIT 10;"
```

---

## 15. Kampanya status

```bash
python -m alphaforge.burnin_ops \
  --db $DB \
  status \
  --campaign-id "$CID"
```

JSON:

```bash
python -m alphaforge.burnin_ops \
  --db $DB \
  --json \
  status \
  --campaign-id "$CID"
```

PowerShell:

```powershell
python -m alphaforge.burnin_ops --db $DB status --campaign-id $CID
```

SQL:

Current campaign için tek sorguda accepted closed PAPER trades vs resolved rejected forward outcomes:
sqlite3 -readonly -header -column "$DB" <<'SQL'
WITH accepted AS (
    SELECT
        'ACCEPTED_CLOSED' AS cohort,
        COUNT(*) AS n,
        SUM(CASE WHEN net_r > 0 THEN 1 ELSE 0 END) AS wins,
        SUM(CASE WHEN net_r < 0 THEN 1 ELSE 0 END) AS losses,
        ROUND(
            100.0 * SUM(CASE WHEN net_r > 0 THEN 1 ELSE 0 END)
            / NULLIF(COUNT(*), 0),
            1
        ) AS win_pct,
        ROUND(AVG(gross_r), 4) AS avg_gross_r,
        ROUND(AVG(net_r), 4) AS avg_net_r,
        ROUND(SUM(net_r), 4) AS total_net_r,
        ROUND(AVG(total_execution_cost), 6) AS avg_execution_cost
    FROM burnin_trade_outcomes
    WHERE burnin_run_id = '"$RUN_ID"'
      AND closed_at IS NOT NULL
      AND evidence_complete = 1
),
rejected AS (
    SELECT
        'REJECTED_RESOLVED' AS cohort,
        COUNT(*) AS n,
        SUM(CASE WHEN hypothetical_net_r_after_costs > 0 THEN 1 ELSE 0 END) AS wins,
        SUM(CASE WHEN hypothetical_net_r_after_costs < 0 THEN 1 ELSE 0 END) AS losses,
        ROUND(
            100.0 * SUM(CASE WHEN hypothetical_net_r_after_costs > 0 THEN 1 ELSE 0 END)
            / NULLIF(COUNT(*), 0),
            1
        ) AS win_pct,
        ROUND(AVG(hypothetical_gross_r), 4) AS avg_gross_r,
        ROUND(AVG(hypothetical_net_r_after_costs), 4) AS avg_net_r,
        ROUND(SUM(hypothetical_net_r_after_costs), 4) AS total_net_r,
        NULL AS avg_execution_cost
    FROM burnin_reject_outcomes
    WHERE burnin_run_id = '"$RUN_ID"'
      AND evidence_complete = 1
      AND hypothetical_net_r_after_costs IS NOT NULL
)
SELECT * FROM accepted
UNION ALL
SELECT * FROM rejected;
SQL
---
Rejectleri ayrıca TP / SL / timeout / ambiguous görmek için:
sqlite3 -readonly -header -column "$DB" <<'SQL'
SELECT
    reject_reason,
    COUNT(*) AS n,
    SUM(CASE WHEN would_tp = 1 THEN 1 ELSE 0 END) AS tp,
    SUM(CASE WHEN would_sl = 1 THEN 1 ELSE 0 END) AS sl,
    SUM(CASE WHEN timeout = 1 THEN 1 ELSE 0 END) AS timeout,
    SUM(CASE WHEN ambiguous = 1 THEN 1 ELSE 0 END) AS ambiguous,
    ROUND(AVG(hypothetical_net_r_after_costs), 4) AS avg_net_r,
    ROUND(SUM(hypothetical_net_r_after_costs), 4) AS total_net_r
FROM burnin_reject_outcomes
WHERE burnin_run_id = '"$RUN_ID"'
  AND evidence_complete = 1
GROUP BY reject_reason
ORDER BY n DESC;
SQL
---

## 16. Health kontrolü

Tek kontrol:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  health \
  --campaign-id "$CID"
```

JSON:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  --json \
  health \
  --campaign-id "$CID"
```

---

## 17. Watch

Tek operasyon kontrol çevrimi:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  watch \
  --campaign-id "$CID"
```

JSON:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  --json \
  watch \
  --campaign-id "$CID"
```

`watch` tek bir operasyon kontrol çevrimi çalıştırır; sürekli terminal ekranı varsayımı yapma. Periyodik izleme gerekiyorsa komutu scheduler veya kontrollü shell döngüsüyle çağır.

macOS / Linux örneği, 60 saniyede bir:

```bash
while true; do
  date
  python -m alphaforge.burnin_ops --db "$DB" status --campaign-id "$CID"
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
tail -n 200 "artifacts/burnin/$CID/worker.stdout.log"
tail -n 200 "artifacts/burnin/$CID/worker.stderr.log"
```

Canlı takip:

```bash
tail -f "artifacts/burnin/$CID/worker.stdout.log"
```

Hata logunu canlı takip:

```bash
tail -f "artifacts/burnin/$CID/worker.stderr.log"
```

PowerShell:

```powershell
Get-Content "artifacts\burnin\$CID\worker.stdout.log" -Tail 200
Get-Content "artifacts\burnin\$CID\worker.stderr.log" -Tail 200
Get-Content "artifacts\burnin\$CID\worker.stderr.log" -Wait -Tail 50
```

---

## 19. Kampanyayı normal şekilde duraklat

Detached burn-in'i durdurmak için ilk tercih `pause` olmalıdır:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  pause \
  --campaign-id "$CID"
```

PowerShell:

```powershell
python -m alphaforge.burnin_ops --db $DB pause --campaign-id $CID
```

Ardından doğrula:

```bash
python -m alphaforge.burnin_ops --db "$DB" status --campaign-id "$CID"
python -m alphaforge.burnin_ops --db "$DB" health --campaign-id "$CID"
```

`pause`, kampanyayı silmez ve tamamlanmış gibi işaretlemez.

---

## 20. Kampanyayı devam ettir

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  resume \
  --campaign-id "$CID"
```

PowerShell:

```powershell
python -m alphaforge.burnin_ops --db $DB resume --campaign-id $CID
```

Resume sonrasında:

```bash
python -m alphaforge.burnin_ops --db "$DB" status --campaign-id "$CID"
python -m alphaforge.burnin_ops --db "$DB" health --campaign-id "$CID"
```

Release/config/strategy/universe/execution-cost identity değiştiyse devam etmeye zorlama. Yeni preflight ve yeni kampanya gerekir.

---

## 21. Recovery drill

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  recovery-drill \
  --campaign-id "$CID"
```

PowerShell:

```powershell
python -m alphaforge.burnin_ops --db $DB recovery-drill --campaign-id $CID
```

Bu komut gerçek bir recovery kanıtı üretir. Sırf status değiştirmek için kullanılmamalıdır.

---

## 22. Integrity audit

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  audit \
  --campaign-id "$CID"
```

PowerShell:

```powershell
python -m alphaforge.burnin_ops --db $DB audit --campaign-id $CID
```

Finalization öncesinde audit `PASS` olmalıdır.

---

## 23. Günlük rapor

```bash
REPORT_DIR="artifacts/burnin/$CID/daily_$(date -u +%Y%m%dT%H%M%SZ)
"

python -m alphaforge.burnin_ops \
  --db "$DB" \
  report \
  --campaign-id "$CID" \
  --output-dir "$REPORT_DIR"
```

PowerShell:

```powershell
$REPORT_DIR="artifacts\burnin\$CID\daily_$(Get-Date -Format 'yyyyMMddTHHmmssZ')"
python -m alphaforge.burnin_ops --db $DB report --campaign-id $CID --output-dir $REPORT_DIR
```

Rapor klasörü JSON, CSV ve Markdown günlük özet çıktıları üretir.

---

## 24. Finalize

Kampanya süresi tamamlanmadan, health/audit/recovery kanıtları oluşmadan finalize etmek `PAPER_BURNIN_INCOMPLETE` veya `PAPER_BURNIN_FAILED` sonucu verebilir. Bu fail-closed davranıştır.

macOS / Linux:

```bash
FINAL_DIR="artifacts/burnin/$CID/final"

python -m alphaforge.burnin_ops \
  --db "$DB" \
  finalize \
  --campaign-id "$CID" \
  --output-dir "$FINAL_DIR"
```

PowerShell:

```powershell
$FINAL_DIR="artifacts\burnin\$CID\final"
python -m alphaforge.burnin_ops --db $DB finalize --campaign-id $CID --output-dir $FINAL_DIR
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
  --campaign-id "$CID" \
  --detach
```

Foreground başlat:

```bash
python -m alphaforge.burnin_cli \
  --db "$DB" \
  start \
  --campaign-id "$CID" \
  --foreground
```

Status:

```bash
python -m alphaforge.burnin_cli --db "$DB" status --campaign-id "$CID"
```

Pause:

```bash
python -m alphaforge.burnin_cli --db "$DB" pause --campaign-id "$CID"
```

Resume detached:

```bash
python -m alphaforge.burnin_cli --db "$DB" resume --campaign-id "$CID" --detach
```

Tek resolver tick:

```bash
python -m alphaforge.burnin_cli --db "$DB" worker --campaign-id "$CID" --once
```

Qualification:

```bash
python -m alphaforge.burnin_cli --db "$DB" qualify --campaign-id "$CID"
```

Evidence export:

```bash
python -m alphaforge.burnin_cli \
  --db "$DB" \
  export \
  --campaign-id "$CID" \
  --output-dir "artifacts/burnin/$CID/export"
```

---

# SQL Operasyon Sorguları

## 27. Kampanya özeti

```bash
sqlite3 -readonly "$DB" <<SQL
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
WHERE campaign_id = '$CID';
SQL
```

## 28. Son kampanya olayları

```bash
sqlite3 -readonly "$DB" <<SQL
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
WHERE campaign_id = '$CID'
ORDER BY id DESC
LIMIT 50;
SQL
```

## 29. Run kayıtları

```bash
sqlite3 -readonly "$DB" <<SQL
.headers on
.mode column
SELECT *
FROM burnin_campaign_runs
WHERE campaign_id = '$CID'
ORDER BY continuation_sequence DESC, id DESC;
SQL
```

## 30. Health geçmişi

```bash
sqlite3 -readonly "$DB" <<SQL
.headers on
.mode column
SELECT
    id,
    generated_at,
    status,
    unhealthy_reasons_json
FROM burnin_health_history
WHERE campaign_id = '$CID'
ORDER BY id DESC
LIMIT 30;
SQL
```

## 31. Incident geçmişi

```bash
sqlite3 -readonly "$DB" <<SQL
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
WHERE campaign_id = '$CID'
ORDER BY id DESC;
SQL
```

## 32. Recovery drill kayıtları

```bash
sqlite3 -readonly "$DB" <<SQL
.headers on
.mode column
SELECT
    id,
    drill_id,
    generated_at,
    status,
    checks_json
FROM burnin_recovery_drills
WHERE campaign_id = '$CID'
ORDER BY id DESC;
SQL
```

## 33. Integrity audit kayıtları

```bash
sqlite3 -readonly "$DB" <<SQL
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
WHERE campaign_id = '$CID'
ORDER BY id DESC;
SQL
```

## 34. Final release decision

```bash
sqlite3 -readonly "$DB" <<SQL
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
WHERE campaign_id = '$CID'
ORDER BY id DESC;
SQL
```

## 35. Karar sayıları

```bash
sqlite3 -readonly "$DB" <<SQL
.headers on
.mode column
SELECT
    decision,
    COUNT(*) AS count
FROM burnin_observations
WHERE burnin_run_id IN (
    SELECT burnin_run_id
    FROM burnin_campaign_runs
    WHERE campaign_id = '$CID'
)
GROUP BY decision
ORDER BY count DESC;
SQL
```

## 36. Reject nedenleri

```bash
sqlite3 -readonly "$DB" <<SQL
.headers on
.mode column
SELECT
    reject_reason,
    COUNT(*) AS count
FROM burnin_reject_outcomes
WHERE burnin_run_id IN (
    SELECT burnin_run_id
    FROM burnin_campaign_runs
    WHERE campaign_id = '$CID'
)
GROUP BY reject_reason
ORDER BY count DESC;
SQL
```

## 37. Açık PAPER pozisyonları

Önce tablo şemasını doğrula:

```bash
sqlite3 -readonly "$DB" ".schema burnin_trade_outcomes"
```

Sonra açık kayıtları say:

```bash
sqlite3 -readonly "$DB" <<SQL
.headers on
.mode column
SELECT COUNT(*) AS open_trade_outcomes
FROM burnin_trade_outcomes
WHERE burnin_run_id IN (
    SELECT burnin_run_id
    FROM burnin_campaign_runs
    WHERE campaign_id = '$CID'
)
AND closed_at IS NULL;
SQL
```

## SQL tüm kampanya

> **Kaldırılan legacy örnek:** Bu bölümde daha önce `POSTDBFX.db` ve `camp_bd9dc5aeadac0b59` gibi sabit DB/campaign kimlikleri içeren çalıştırılabilir bir SQL bloğu vardı. Yanlış kampanyaya sorgu çalıştırma ve stale şema kullanma riski nedeniyle kaldırıldı. Güncel campaign/reject/accepted-trade sorguları için `docs/SQLcheat.md` kullan; aşağıdaki B–H bölümleri de yalnız read-only operasyon örnekleri olarak tutulur.

---

# Süreç ve Hata Teşhisi

## 38. Worker PID çalışıyor mu?

Kampanya PID'sini getir:

```bash
sqlite3 -readonly "$DB" "SELECT worker_pid FROM burnin_campaigns WHERE campaign_id='$CID';"
```

macOS / Linux:

```bash
PID=$(sqlite3 -readonly "$DB" "SELECT worker_pid FROM burnin_campaigns WHERE campaign_id='$CID';")
ps -p "$PID" -o pid,ppid,etime,state,command
```

PowerShell:

```powershell
$PID_FROM_DB=sqlite3 -readonly $DB "SELECT worker_pid FROM burnin_campaigns WHERE campaign_id='$CID';"
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
python -m alphaforge.burnin_ops --db "$DB" status --campaign-id "$CID"
python -m alphaforge.burnin_ops --db "$DB" health --campaign-id "$CID"
python -m alphaforge.burnin_ops --db "$DB" recovery-drill --campaign-id "$CID"
python -m alphaforge.burnin_ops --db "$DB" audit --campaign-id "$CID"
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
python -m alphaforge.burnin_ops --db "$DB" status --campaign-id "$CID"
python -m alphaforge.burnin_ops --db "$DB" health --campaign-id "$CID"
python -m alphaforge.burnin_ops --db "$DB" watch --campaign-id "$CID"
tail -n 100 "artifacts/burnin/$CID/worker.stderr.log"
tail -n 100 "artifacts/burnin/$CID/worker.stdout.log"
```

## 43. Hata sonrası minimum teşhis paketi

```bash
python -m alphaforge.burnin_ops --db "$DB" --json status --campaign-id "$CID"
python -m alphaforge.burnin_ops --db "$DB" --json health --campaign-id "$CID"
tail -n 200 "artifacts/burnin/$CID/worker.stderr.log"
tail -n 200 "artifacts/burnin/$CID/worker.stdout.log"
sqlite3 -readonly "$DB" "PRAGMA integrity_check;"
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


### `EXCHANGE_RECONCILIATION_UNAVAILABLE`

Normal PAPER runtime gerekli signed read-only Binance reconciliation provider/snapshot kanıtını alamamıştır. Bayrağın kapalı olması; eksik, partial veya placeholder credentials; yanlış Binance environment/base URL; auth/izin/ağ hatası olası nedenlerdir. Bölüm 4.1'deki canonical config-loader komutu yalnız `RECON`, `KEY`, `SECRET` durumlarını basar; secret değerlerini basmaz. Preflight artık aynı authenticated capability tamamlanmadan PASS vermez.

### DB path ayrışması

Effective runtime DB'yi secret olmadan yazdır ve burn-in komutlarında aynı `$DB` kullanıldığını doğrula:

```bash
python - <<'PY'
from alphaforge.config import load_config_from_env
from alphaforge.database_defaults import sqlite_path_from_url
u = load_config_from_env().persistence.database_url
print(u)
print(sqlite_path_from_url(u))
PY
test ! -e ./alphaforge.db && echo "legacy root DB absent"
alembic current
alembic heads
python -m alphaforge.burnin_ops --db "$DB" status --campaign-id "$CID"
```

PowerShell'de root legacy kontrolü `Test-Path .\alphaforge.db`, canonical dosya kontrolü `Resolve-Path $DB` ile yapılır. Mevcut legacy dosyayı otomatik silme/taşıma; explicit operator override'ını araştır.

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

`diagnose-db` is database read-only. `config_check` and the default `config_fix` dry-run do not mutate configuration. **Preflight is not read-only:** it may create or update local database evidence. None of these commands submit or cancel exchange orders. Keep canonical `ALPHAFORGE_EXECUTION_MODE=PAPER`, remove the deprecated `EXECUTION_MODE` alias, and keep `ALPHAFORGE_ENABLE_LIVE_TRADING=false`. REST-only reconciliation does **not** require a websocket; runtime/streaming websocket requirements remain strict.

### PowerShell

```powershell
$env:ALPHAFORGE_EXECUTION_MODE = "PAPER"
Remove-Item Env:EXECUTION_MODE -ErrorAction SilentlyContinue
$env:ALPHAFORGE_ENABLE_LIVE_TRADING = "false"
$DB = "data/runtime/alphaforge_runtime.db"
$RELEASE_ID = "phase9-$(git rev-parse --short HEAD)"
$CID = "<campaign_id-from-launch-output>"

### A. Diagnose an existing database

python -m alphaforge.config_check
python -m alphaforge.config_fix --json
python -m alphaforge.burnin_ops --db $DB --json diagnose-db --max-heartbeat-age 120 | Tee-Object -FilePath artifacts/burnin/database_diagnosis.json

### B. Start a new clean campaign

python -m alphaforge.binance_reconciliation_check --symbols BTCUSDT ETHUSDT | Tee-Object -FilePath artifacts/burnin/reconciliation.json
python -m alphaforge.burnin_ops --db $DB --json preflight --release-id $RELEASE_ID --symbols BTCUSDT,ETHUSDT --intervals 1h --output-dir artifacts/burnin/preflight
python -m alphaforge.burnin_ops --db $DB --json launch --release-id $RELEASE_ID --duration-days 3 --symbols BTCUSDT,ETHUSDT --intervals 1h --detach
python -m alphaforge.burnin_ops --db $DB --json status --campaign-id $CID
python -m alphaforge.burnin_ops --db $DB --json health --campaign-id $CID
python -m alphaforge.burnin_ops --db $DB --json watch --campaign-id $CID
python -m alphaforge.burnin_ops --db $DB --json audit --campaign-id $CID
python -m alphaforge.burnin_ops --db $DB --json finalize --campaign-id $CID --output-dir artifacts/burnin/final
```

### Bash (macOS/Linux)

```bash
export ALPHAFORGE_EXECUTION_MODE=PAPER
unset EXECUTION_MODE
export ALPHAFORGE_ENABLE_LIVE_TRADING=false
DB="data/runtime/alphaforge_runtime.db"
RELEASE_ID="phase9-$(git rev-parse --short HEAD)"
CID='<campaign_id-from-launch-output>'

# A. Diagnose an existing database

python -m alphaforge.config_check
python -m alphaforge.config_fix --json
python -m alphaforge.burnin_ops --db "$DB" --json diagnose-db --max-heartbeat-age 120 | tee artifacts/burnin/database_diagnosis.json

# B. Start a new clean campaign

python -m alphaforge.binance_reconciliation_check --symbols BTCUSDT ETHUSDT | tee artifacts/burnin/reconciliation.json
python -m alphaforge.burnin_ops --db "$DB" --json preflight --release-id "$RELEASE_ID" --symbols BTCUSDT,ETHUSDT --intervals 1h --output-dir artifacts/burnin/preflight
python -m alphaforge.burnin_ops --db "$DB" --json launch --release-id "$RELEASE_ID" --duration-days 3 --symbols BTCUSDT,ETHUSDT --intervals 1h --detach
python -m alphaforge.burnin_ops --db "$DB" --json status --campaign-id "$CID"
python -m alphaforge.burnin_ops --db "$DB" --json health --campaign-id "$CID"
python -m alphaforge.burnin_ops --db "$DB" --json watch --campaign-id "$CID"
python -m alphaforge.burnin_ops --db "$DB" --json audit --campaign-id "$CID"
python -m alphaforge.burnin_ops --db "$DB" --json finalize --campaign-id "$CID" --output-dir artifacts/burnin/final
```

Credential variables (`BINANCE_API_KEY` and `BINANCE_API_SECRET`) must be supplied through the normal environment/dotenv contract and must never be echoed. Accept reconciliation only when `evidence_status` is `COMPLETE`, `sanitized_errors` is empty, `unknown_unreconciled_symbols` is empty, and all endpoint statuses pass. A local diagnostic recovery is never authenticated exchange evidence. Run `recovery-drill` only after both the database diagnosis and authenticated reconciliation prove zero positions and zero pending orders.

## Phase A shadow agent graph

Phase B keeps the same disabled-by-default, shadow-only controls. Inspect the
normalized Market/Signal/Quality and parity evidence from PowerShell without
changing runtime state:

```powershell
sqlite3 data/runtime/alphaforge_agent_shadow.db "SELECT symbol,regime,score,raw_rr,quality_status,primary_reject_reason,parity_status FROM agent_phase_b_evidence ORDER BY created_at DESC LIMIT 50;"
sqlite3 data/runtime/alphaforge_agent_shadow.db "SELECT primary_reject_reason,COUNT(*) FROM agent_phase_b_evidence GROUP BY primary_reject_reason ORDER BY COUNT(*) DESC;"
```

SQL `NULL` and JSON `null` mean unavailable; they must not be interpreted as
zero. The legacy decision remains authoritative and no cutover has occurred.

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
sqlite3 -readonly $DB "SELECT correlation_id,decision_id,graph_status,shadow_only FROM agent_runs ORDER BY id DESC LIMIT 20;"
sqlite3 -readonly $DB "SELECT correlation_id,stage,status,primary_reason,skipped_reason FROM agent_stage_events ORDER BY id DESC LIMIT 40;"
# Confirm the shadow tables have no triggers and compare order/lifecycle counts before and after a shadow-only test.
sqlite3 -readonly $DB "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('agent_runs','agent_stage_events');"
sqlite3 -readonly $DB "SELECT (SELECT count(*) FROM orders) AS orders_count,(SELECT count(*) FROM trade_lifecycle_events) AS lifecycle_count;"
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
sqlite3 -readonly "$DB" "SELECT correlation_id,decision_id,graph_status,shadow_only FROM agent_runs ORDER BY id DESC LIMIT 20;"
sqlite3 -readonly "$DB" "SELECT correlation_id,stage,status,primary_reason,skipped_reason FROM agent_stage_events ORDER BY id DESC LIMIT 40;"
sqlite3 -readonly "$DB" "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('agent_runs','agent_stage_events');"
sqlite3 -readonly "$DB" "SELECT (SELECT count(*) FROM orders) AS orders_count,(SELECT count(*) FROM trade_lifecycle_events) AS lifecycle_count;"
pytest -q tests/test_agent_contracts.py tests/test_agent_orchestrator.py tests/test_agent_persistence.py
pytest -q
```

## PAPER Control Center backend

Canonical environment, PowerShell startup, read-first verification, and guarded pause/resume commands are documented in [`CONTROL_CENTER_RUNTIME_MAPPING.md`](CONTROL_CENTER_RUNTIME_MAPPING.md). The API is PAPER-only. It has no campaign stop endpoint because the burn-in CLI has no canonical stop command or STOPPED campaign state.

### Windows PowerShell: Control Center backend entry point

```powershell
$env:ALPHAFORGE_DB_PATH = "data/runtime/alphaforge_runtime.db"
$env:ALPHAFORGE_PROJECT_ROOT = (Get-Location).Path
$env:ALPHAFORGE_EXECUTION_MODE = "PAPER"
$env:ALPHAFORGE_CONTROL_CORS_ORIGINS = "http://127.0.0.1:5173" # optional explicit cross-origin opt-in

python -m alphaforge.control_center `
  --host 127.0.0.1 `
  --port 8000

Invoke-RestMethod http://127.0.0.1:8000/api/health
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/status
```

Use repeatable `--cors-origin <origin>` options, or `ALPHAFORGE_CONTROL_CORS_ORIGINS` as a comma-separated exact-origin allowlist, when the frontend does not use a default localhost development origin.
## PAPER reject forward-outcome integrity gate

Run the schema upgrade/doctor once after deploying new code, then use the canonical read-only gate repeatedly. Campaign PAPER:

```powershell
python -m alphaforge.burnin_ops --db "$DB" db-doctor --apply
python -m alphaforge.burnin_ops --db "$DB" --json reject-label-status --campaign-id $CID
```

Standalone PAPER uses its persisted runtime identity; do not create a campaign:

```powershell
python -m alphaforge.burnin_ops --db "$DB" --json reject-label-status --runtime-identity "standalone:$BURNIN_RUN_ID"
```

`PASS` means the complete scoped PAPER reject denominator is consistent: every label-eligible reject owns exactly one pending identity, every RESOLVED label owns one canonical outcome, no stale/overdue resolver work exists, and at least one accuracy-eligible result is mature. Inspect the `coverage` object for total rejects, reviews, eligible/pending/unlabeled rejects, incomplete geometry, missing costs, resolved/failed/ambiguous/execution-invalidated labels, eligible accuracy labels, and label/mature coverage ratios.

`INCOMPLETE` means the database is not structurally contradictory but maturity/provider evidence is insufficient. Incomplete geometry or costs and FAILED, AMBIGUOUS, execution-invalidated, immature, overdue, stale-claim, or no-outcome populations are explicit reason codes, so one good row cannot hide them. `FAIL` means a structural violation such as missing eligible label ownership, duplicate ownership/identity/outcome, ambiguous linkage, orphan evidence, impossible correctness state, invalid finalized evidence, or RESOLVED without a canonical outcome. Both results block Phase C. The command scopes by explicit campaign continuation membership or the exact `standalone:<burnin_run_id>`; it never infers unrelated history. It never repairs, deletes, updates, or fabricates evidence.

Future-due `PENDING`, `READY`, and `RESOLVING` labels emit `IMMATURE_LABELS_PRESENT`; PASS requires `mature_coverage_ratio` to equal `1.0`. `coverage.legacy_unattributed_observations` reports pre-identity observations separately from `total_rejected_decisions`: these rows block Phase C as INCOMPLETE but are not guessed to be distinct rejects. Pending/outcome contradictions and unknown statuses are structural FAIL results.
---

# Güncel Campaign / PAPER Operasyon Paketi — 2026-09-15

Bu bölüm mevcut `dev` branch'teki güncel campaign şemasına göre hazırlanmıştır.
Campaign continuation üyeliği için kanonik tablo `burnin_campaign_runs`'dır.
PAPER pozisyon sonuçları campaign-native olarak `burnin_pending_position_outcomes`,
reject forward-outcome kimlikleri ise `burnin_pending_reject_labels` ve
`burnin_reject_outcomes` üzerinden izlenir.

> **SQL güvenliği:** Aşağıdaki sorgular salt-okuma içindir. Campaign durumunu,
> qualification sonucunu veya evidence kayıtlarını SQL ile elle değiştirme.

## A. Güncel yeni `burnin_ops` fonksiyonları

### A.1 Database diagnosis — READ ONLY

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  --json \
  diagnose-db \
  --max-heartbeat-age 120
```

Bu komut mevcut kampanya, continuation, worker, runtime snapshot, open position,
pending reject label ve reconciliation durumunu değiştirmeden teşhis eder.

### A.2 DB doctor

Sadece kontrol:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  db-doctor \
  --check-only
```

Gerekli additive schema düzeltmelerini uygula:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  db-doctor \
  --apply
```

`--apply` öncesi çalışan kampanyanın durumunu ve backup politikasını doğrula.

### A.3 Reject forward-outcome integrity gate

Campaign PAPER:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  --json \
  reject-label-status \
  --campaign-id "$CID"
```

Standalone PAPER:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  --json \
  reject-label-status \
  --runtime-identity "standalone:$BURNIN_RUN_ID"
```

İsteğe bağlı stale claim eşiği:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  --json \
  reject-label-status \
  --campaign-id "$CID" \
  --stale-claim-seconds 300
```

Yorum:

- `PASS`: scoped reject denominator ve mature outcome bütünlüğü tutarlı.
- `INCOMPLETE`: yapısal çelişki yok, fakat maturity/provider/evidence eksik.
- `FAIL`: duplicate/orphan/identity/finalized-evidence gibi yapısal ihlal var.

### A.4 Runtime recovery

Önce teşhis:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  --json \
  diagnose-db \
  --max-heartbeat-age 120
```

Sonra yalnız gerçekten recovery gereken campaign için:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  --json \
  recover-runtime \
  --campaign-id "$CID"
```

Dead `RECOVERY_REQUIRED` continuation'ı ancak complete zero-exposure doğrulaması
varsa terminalize etmek için explicit seçenek:

```bash
python -m alphaforge.burnin_ops \
  --db "$DB" \
  --json \
  recover-runtime \
  --campaign-id "$CID" \
  --terminalize-zero-exposure
```

Bu seçenek normal campaign stop komutu değildir.

### A.5 Config / reconciliation hızlı kontrolleri

```bash
python -m alphaforge.config_check
python -m alphaforge.config_fix --json
python -m alphaforge.binance_reconciliation_check --symbols BTCUSDT ETHUSDT
```

`config_fix` varsayılan dry-run davranışını koruyorsa çıktıyı incelemeden mutation
varsayma. Binance credential değerlerini terminale yazdırma.

---

# B. Campaign çalışma durumu — SQL

Aşağıdaki örneklerde:

```bash
DB="data/campaign/POST_AUTONOMOUS.db"
CID="camp_xxxxxxxxxxxxxxxx"
```

kendi gerçek değerlerinle değiştir.

## B.1 Campaign + active continuation tek satır özet

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT
    c.campaign_id,
    c.release_id,
    c.campaign_status,
    c.active_run_id,
    c.worker_pid,
    c.restart_count,
    c.created_at,
    c.started_at,
    c.completed_at,
    c.expected_duration_seconds,
    c.observed_duration_seconds,
    c.last_heartbeat_at,
    c.last_error,
    c.qualification_status,
    c.latest_qualification_id,
    c.evidence_completeness_status,
    r.continuation_sequence,
    r.status AS active_run_status,
    r.started_at AS active_run_started_at,
    r.ended_at AS active_run_ended_at
FROM burnin_campaigns c
LEFT JOIN burnin_campaign_runs r
  ON r.campaign_id = c.campaign_id
 AND r.burnin_run_id = c.active_run_id
WHERE c.campaign_id = '$CID';
"
```

## B.2 Tüm continuation geçmişi

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT
    continuation_sequence,
    burnin_run_id,
    status,
    started_at,
    ended_at,
    created_at
FROM burnin_campaign_runs
WHERE campaign_id = '$CID'
ORDER BY continuation_sequence;
"
```

Beklenti:

- `continuation_sequence` unique olmalı.
- Aynı anda birden fazla `RUNNING` continuation olmamalı.
- `active_run_id`, çalışan continuation ile eşleşmeli.

## B.3 Son 50 campaign event

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT
    id,
    event_time,
    event_type,
    burnin_run_id,
    details_json
FROM burnin_campaign_events
WHERE campaign_id = '$CID'
ORDER BY id DESC
LIMIT 50;
"
```

## B.4 Resolver / provider / failure event özeti

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT
    event_type,
    COUNT(*) AS n,
    MIN(event_time) AS first_seen,
    MAX(event_time) AS last_seen
FROM burnin_campaign_events
WHERE campaign_id = '$CID'
  AND (
       event_type LIKE '%RESOLVER%'
       OR event_type LIKE '%PROVIDER%'
       OR event_type LIKE '%FAIL%'
       OR event_type LIKE '%ERROR%'
  )
GROUP BY event_type
ORDER BY n DESC, event_type;
"
```

## B.5 Resolver batch hata metinleri

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT
    COALESCE(json_extract(details_json, '$.error'), 'NO_ERROR_FIELD') AS error,
    COUNT(*) AS n,
    MIN(event_time) AS first_seen,
    MAX(event_time) AS last_seen
FROM burnin_campaign_events
WHERE campaign_id = '$CID'
  AND event_type = 'RESOLVER_BATCH_FAILED'
GROUP BY error
ORDER BY n DESC;
"
```

---

# C. PAPER accepted trade çalışma ve sonuç istatistikleri

## C.1 Açık / kapanmış campaign PAPER pozisyonları

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT
    status,
    COUNT(*) AS n,
    ROUND(SUM(COALESCE(notional,0)), 2) AS notional_sum,
    ROUND(SUM(COALESCE(net_pnl,0)), 6) AS net_pnl_sum,
    ROUND(SUM(COALESCE(net_r,0)), 6) AS net_r_sum
FROM burnin_pending_position_outcomes
WHERE campaign_id = '$CID'
GROUP BY status
ORDER BY status;
"
```

## C.2 Açık pozisyon detayları

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT
    pending_position_id,
    trade_id,
    burnin_run_id,
    symbol,
    side,
    regime,
    entry_time,
    planned_entry,
    simulated_fill,
    stop,
    target,
    quantity,
    notional,
    entry_spread,
    entry_slippage,
    entry_fee,
    status
FROM burnin_pending_position_outcomes
WHERE campaign_id = '$CID'
  AND status = 'OPEN'
ORDER BY entry_time;
"
```

## C.3 Kapanmış PAPER sonuç özeti

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT
    COUNT(*) AS closed_trades,
    SUM(CASE WHEN net_r > 0 THEN 1 ELSE 0 END) AS winners,
    SUM(CASE WHEN net_r < 0 THEN 1 ELSE 0 END) AS losers,
    ROUND(
      100.0 * SUM(CASE WHEN net_r > 0 THEN 1 ELSE 0 END)
      / NULLIF(COUNT(*),0),
      1
    ) AS win_pct,
    ROUND(AVG(net_r), 4) AS avg_net_r,
    ROUND(SUM(net_r), 4) AS total_net_r,
    ROUND(AVG(net_pnl), 6) AS avg_net_pnl,
    ROUND(SUM(net_pnl), 6) AS total_net_pnl,
    ROUND(AVG(total_execution_cost), 6) AS avg_execution_cost,
    ROUND(SUM(total_execution_cost), 6) AS total_execution_cost,
    ROUND(AVG(entry_spread + COALESCE(exit_spread,0)), 6) AS avg_roundtrip_spread,
    ROUND(AVG(entry_slippage + COALESCE(exit_slippage,0)), 6) AS avg_roundtrip_slippage,
    ROUND(AVG(hold_duration_seconds), 1) AS avg_hold_seconds
FROM burnin_pending_position_outcomes
WHERE campaign_id = '$CID'
  AND status <> 'OPEN'
  AND resolved_at IS NOT NULL;
"
```

## C.4 Sonuçları sembol / side / regime bazında

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT
    symbol,
    side,
    COALESCE(regime,'UNKNOWN') AS regime,
    COUNT(*) AS n,
    SUM(CASE WHEN net_r > 0 THEN 1 ELSE 0 END) AS winners,
    SUM(CASE WHEN net_r < 0 THEN 1 ELSE 0 END) AS losers,
    ROUND(AVG(net_r), 4) AS avg_net_r,
    ROUND(SUM(net_r), 4) AS total_net_r,
    ROUND(AVG(total_execution_cost), 6) AS avg_execution_cost,
    ROUND(AVG(mfe), 6) AS avg_mfe,
    ROUND(AVG(mae), 6) AS avg_mae
FROM burnin_pending_position_outcomes
WHERE campaign_id = '$CID'
  AND status <> 'OPEN'
  AND resolved_at IS NOT NULL
GROUP BY symbol, side, COALESCE(regime,'UNKNOWN')
ORDER BY total_net_r DESC;
"
```

## C.5 Exit reason dağılımı

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT
    COALESCE(exit_reason,'UNKNOWN') AS exit_reason,
    COUNT(*) AS n,
    ROUND(AVG(net_r),4) AS avg_net_r,
    ROUND(SUM(net_r),4) AS total_net_r
FROM burnin_pending_position_outcomes
WHERE campaign_id = '$CID'
  AND status <> 'OPEN'
GROUP BY COALESCE(exit_reason,'UNKNOWN')
ORDER BY n DESC;
"
```

## C.6 Execution-cost drag

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT
    COUNT(*) AS n,
    ROUND(AVG(entry_spread), 8) AS avg_entry_spread,
    ROUND(AVG(entry_slippage), 8) AS avg_entry_slippage,
    ROUND(AVG(entry_fee), 8) AS avg_entry_fee,
    ROUND(AVG(exit_spread), 8) AS avg_exit_spread,
    ROUND(AVG(exit_slippage), 8) AS avg_exit_slippage,
    ROUND(AVG(exit_fee), 8) AS avg_exit_fee,
    ROUND(AVG(funding), 8) AS avg_funding,
    ROUND(AVG(latency_impact_penalty), 8) AS avg_latency_penalty,
    ROUND(AVG(total_execution_cost), 8) AS avg_total_execution_cost,
    ROUND(SUM(total_execution_cost), 8) AS total_execution_cost
FROM burnin_pending_position_outcomes
WHERE campaign_id = '$CID'
  AND status <> 'OPEN'
  AND resolved_at IS NOT NULL;
"
```

---

# D. Reject forward-outcome istatistikleri

## D.1 Label durumları

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT
    status,
    COUNT(*) AS n,
    SUM(CASE WHEN evidence_complete = 1 THEN 1 ELSE 0 END) AS evidence_complete,
    MIN(due_at) AS oldest_due_at,
    MAX(due_at) AS newest_due_at
FROM burnin_pending_reject_labels
WHERE campaign_id = '$CID'
GROUP BY status
ORDER BY n DESC;
"
```

## D.2 Reject reason + label maturity

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT
    COALESCE(reject_reason,'UNKNOWN') AS reject_reason,
    status,
    COUNT(*) AS n,
    SUM(CASE WHEN evidence_complete = 1 THEN 1 ELSE 0 END) AS complete
FROM burnin_pending_reject_labels
WHERE campaign_id = '$CID'
GROUP BY COALESCE(reject_reason,'UNKNOWN'), status
ORDER BY n DESC, reject_reason;
"
```

## D.3 Reject attribution / provenance

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT
    COALESCE(
      json_extract(source_provenance_json, '$.forward_label_subject'),
      'UNKNOWN'
    ) AS subject,
    COALESCE(
      json_extract(source_provenance_json, '$.reject_quality_attributable'),
      0
    ) AS attributable,
    status,
    COUNT(*) AS n
FROM burnin_pending_reject_labels
WHERE campaign_id = '$CID'
GROUP BY subject, attributable, status
ORDER BY attributable DESC, subject, status;
"
```

## D.4 Reject forward-outcome sonuçları — bütün continuation'lar

```bash
sqlite3 -readonly -header -column "$DB" "
WITH campaign_runs AS (
    SELECT burnin_run_id
    FROM burnin_campaign_runs
    WHERE campaign_id = '$CID'
)
SELECT
    COALESCE(reject_reason,'UNKNOWN') AS reject_reason,
    symbol,
    COALESCE(regime,'UNKNOWN') AS regime,
    COUNT(*) AS n,
    SUM(CASE WHEN would_tp = 1 THEN 1 ELSE 0 END) AS tp,
    SUM(CASE WHEN would_sl = 1 THEN 1 ELSE 0 END) AS sl,
    SUM(CASE WHEN ambiguous = 1 THEN 1 ELSE 0 END) AS ambiguous,
    SUM(CASE WHEN timeout = 1 THEN 1 ELSE 0 END) AS timeout,
    SUM(CASE WHEN execution_invalidated = 1 THEN 1 ELSE 0 END) AS execution_invalidated,
    ROUND(AVG(hypothetical_net_r_after_costs), 4) AS avg_net_r,
    ROUND(SUM(hypothetical_net_r_after_costs), 4) AS total_net_r
FROM burnin_reject_outcomes
WHERE burnin_run_id IN (SELECT burnin_run_id FROM campaign_runs)
GROUP BY COALESCE(reject_reason,'UNKNOWN'), symbol, COALESCE(regime,'UNKNOWN')
ORDER BY total_net_r DESC;
"
```

## D.5 Reject reason top-level kalite özeti

```bash
sqlite3 -readonly -header -column "$DB" "
WITH campaign_runs AS (
    SELECT burnin_run_id
    FROM burnin_campaign_runs
    WHERE campaign_id = '$CID'
)
SELECT
    COALESCE(reject_reason,'UNKNOWN') AS reject_reason,
    COUNT(*) AS n,
    SUM(CASE WHEN would_tp = 1 THEN 1 ELSE 0 END) AS tp,
    SUM(CASE WHEN would_sl = 1 THEN 1 ELSE 0 END) AS sl,
    SUM(CASE WHEN ambiguous = 1 THEN 1 ELSE 0 END) AS ambiguous,
    SUM(CASE WHEN execution_invalidated = 1 THEN 1 ELSE 0 END) AS execution_invalidated,
    ROUND(AVG(hypothetical_net_r_after_costs), 4) AS avg_net_r,
    ROUND(SUM(hypothetical_net_r_after_costs), 4) AS total_net_r
FROM burnin_reject_outcomes
WHERE burnin_run_id IN (SELECT burnin_run_id FROM campaign_runs)
GROUP BY COALESCE(reject_reason,'UNKNOWN')
ORDER BY total_net_r DESC;
"
```

> Reject outcome'da `total_net_r > 0`, otomatik olarak “bu reject yanlıştı” anlamına gelmez.
> Execution invalidation, ambiguity, attribution ve sample size birlikte değerlendirilmelidir.

---

# E. Identity / lineage / coverage kontrolleri

## E.1 Pending label → rejected review / core decision bağlantısı

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT
    COUNT(*) AS labels,
    SUM(
      CASE WHEN EXISTS (
        SELECT 1
        FROM rejected_signal_reviews r
        WHERE r.reject_decision_id = p.reject_decision_id
      ) THEN 1 ELSE 0 END
    ) AS linked_reviews,
    SUM(
      CASE WHEN EXISTS (
        SELECT 1
        FROM order_decisions d
        WHERE d.decision_id = p.reject_decision_id
      ) THEN 1 ELSE 0 END
    ) AS linked_core_decisions
FROM burnin_pending_reject_labels p
WHERE p.campaign_id = '$CID';
"
```

## E.2 Duplicate reject label identity

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT
    reject_decision_id,
    COUNT(*) AS n
FROM burnin_pending_reject_labels
WHERE campaign_id = '$CID'
GROUP BY reject_decision_id
HAVING COUNT(*) > 1
ORDER BY n DESC;
"
```

Beklenen: **0 satır**.

## E.3 Duplicate continuation sequence / run identity

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT 'continuation_sequence' AS kind, continuation_sequence AS identity, COUNT(*) AS n
FROM burnin_campaign_runs
WHERE campaign_id = '$CID'
GROUP BY continuation_sequence
HAVING COUNT(*) > 1

UNION ALL

SELECT 'burnin_run_id' AS kind, burnin_run_id AS identity, COUNT(*) AS n
FROM burnin_campaign_runs
WHERE campaign_id = '$CID'
GROUP BY burnin_run_id
HAVING COUNT(*) > 1;
"
```

Beklenen: **0 satır**.

---

# F. Qualification snapshot

Önce şemayı gör:

```bash
sqlite3 -readonly "$DB" ".schema burnin_qualification_snapshots"
```

Son snapshot:

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT *
FROM burnin_qualification_snapshots
WHERE campaign_id = '$CID'
ORDER BY id DESC
LIMIT 1;
"
```

Campaign tablosundaki pointer ile karşılaştır:

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT
    campaign_id,
    campaign_status,
    qualification_status,
    latest_qualification_id,
    evidence_completeness_status,
    observed_duration_seconds
FROM burnin_campaigns
WHERE campaign_id = '$CID';
"
```

Qualification snapshot'ın eski olması tek başına runtime hatası değildir. Status çıktısındaki
snapshot age/fresh/stale alanlarıyla birlikte yorumla.

---

# G. Runtime snapshot / reconciliation

Önce şema:

```bash
sqlite3 -readonly "$DB" ".schema runtime_state_snapshots"
```

Son campaign runtime snapshot:

```bash
sqlite3 -readonly -header -column "$DB" "
SELECT
    id,
    timestamp,
    campaign_id,
    burnin_run_id,
    release_id,
    active_position_count,
    pending_order_count,
    orphan_position_count,
    orphan_order_count,
    unknown_exchange_state,
    recovery_action_required,
    reconciliation_status,
    exchange_read_only_status
FROM runtime_state_snapshots
WHERE campaign_id = '$CID'
ORDER BY id DESC
LIMIT 5;
"
```

---

# H. Tek komutluk campaign istatistik paketi

Günlük operasyon için en kullanışlı özet:

```bash
sqlite3 -readonly -header -column "$DB" "
-- CAMPAIGN
SELECT
    campaign_id,
    release_id,
    campaign_status,
    active_run_id,
    worker_pid,
    restart_count,
    observed_duration_seconds,
    last_heartbeat_at,
    last_error,
    qualification_status,
    evidence_completeness_status
FROM burnin_campaigns
WHERE campaign_id = '$CID';

-- CONTINUATIONS
SELECT
    continuation_sequence,
    burnin_run_id,
    status,
    started_at,
    ended_at
FROM burnin_campaign_runs
WHERE campaign_id = '$CID'
ORDER BY continuation_sequence;

-- PAPER POSITIONS
SELECT
    status,
    COUNT(*) AS n,
    ROUND(SUM(COALESCE(net_r,0)),4) AS total_net_r
FROM burnin_pending_position_outcomes
WHERE campaign_id = '$CID'
GROUP BY status;

-- CLOSED PAPER RESULT
SELECT
    COUNT(*) AS closed_n,
    SUM(CASE WHEN net_r > 0 THEN 1 ELSE 0 END) AS wins,
    SUM(CASE WHEN net_r < 0 THEN 1 ELSE 0 END) AS losses,
    ROUND(AVG(net_r),4) AS avg_net_r,
    ROUND(SUM(net_r),4) AS total_net_r,
    ROUND(SUM(total_execution_cost),6) AS execution_cost
FROM burnin_pending_position_outcomes
WHERE campaign_id = '$CID'
  AND status <> 'OPEN'
  AND resolved_at IS NOT NULL;

-- REJECT LABELS
SELECT
    status,
    COUNT(*) AS n,
    SUM(CASE WHEN evidence_complete=1 THEN 1 ELSE 0 END) AS complete
FROM burnin_pending_reject_labels
WHERE campaign_id = '$CID'
GROUP BY status;

-- FAILURES
SELECT
    event_type,
    COUNT(*) AS n,
    MAX(event_time) AS last_seen
FROM burnin_campaign_events
WHERE campaign_id = '$CID'
  AND (
       event_type LIKE '%FAIL%'
       OR event_type LIKE '%ERROR%'
       OR event_type LIKE '%RESOLVER%'
       OR event_type LIKE '%PROVIDER%'
  )
GROUP BY event_type
ORDER BY n DESC;

PRAGMA quick_check;
"
```

---

# I. POST autonomous organization harness sonrası önerilen kontrol sırası

Harness `HEALTHY` döndükten sonra:

```bash
python -m alphaforge.burnin_ops --db "$DB" --json status --campaign-id "$CID"
python -m alphaforge.burnin_ops --db "$DB" --json health --campaign-id "$CID"
python -m alphaforge.burnin_ops --db "$DB" --json watch --campaign-id "$CID"
python -m alphaforge.burnin_ops --db "$DB" --json reject-label-status --campaign-id "$CID"
sqlite3 -readonly "$DB" "PRAGMA quick_check;"
```

İstatistiksel yorum sırası:

1. Worker/runtime canlı mı?
2. Config drift / contamination / evidence regression var mı?
3. Accepted ve rejected kararlar persist ediliyor mu?
4. Accepted trade gerçekten PAPER pozisyona dönüşüyor mu?
5. Closed trade sonuçları net-R ve execution cost ile oluşuyor mu?
6. Reject label'lar `PENDING -> RESOLVED` olgunlaşıyor mu?
7. Resolver/provider failures sıfır mı?
8. Qualification snapshot fresh olduğunda campaign aggregate ile eşleşiyor mu?
9. Ancak yeterli sample sonrasında expectancy / reject quality / concentration yorumlanır.

`n=2`, `n=4` gibi çok küçük örneklerde acceptance rate, win rate veya reject quality
üzerinden threshold tuning yapılmaz.

---

# J. SQLcheat-first — şema keşfi yalnızca uyuşmazlıkta

Normal akışta önce `docs/SQLcheat.md` kullan. Aşağıdaki discovery komutları yalnızca seçtiğin DB'nin canonical dokümandan farklı olduğundan şüpheleniyorsan veya `no such table/column` hatası alıyorsan kullanılmalıdır. Aktif campaign DB'yi discovery amacıyla açarken read-only kal.

Tablolar:

```bash
sqlite3 -readonly "$DB" ".tables"
```

Campaign tabloları:

```bash
sqlite3 -readonly "$DB" "
SELECT name
FROM sqlite_master
WHERE type='table'
  AND (
       name LIKE 'burnin_%'
       OR name LIKE 'runtime_%'
       OR name IN ('order_decisions','rejected_signal_reviews','closed_trade_reviews')
  )
ORDER BY name;
"
```

Kolonlar:

```bash
sqlite3 -readonly "$DB" "PRAGMA table_info(burnin_campaigns);"
sqlite3 -readonly "$DB" "PRAGMA table_info(burnin_campaign_runs);"
sqlite3 -readonly "$DB" "PRAGMA table_info(burnin_pending_position_outcomes);"
sqlite3 -readonly "$DB" "PRAGMA table_info(burnin_pending_reject_labels);"
sqlite3 -readonly "$DB" "PRAGMA table_info(burnin_reject_outcomes);"
sqlite3 -readonly "$DB" "PRAGMA table_info(burnin_qualification_snapshots);"
sqlite3 -readonly "$DB" "PRAGMA table_info(runtime_state_snapshots);"
```

Bir sorgu eski branch veya eski DB şeması nedeniyle `no such column/table` verirse,
SQL'i zorla uyarlamadan önce bu `PRAGMA table_info(...)` çıktısını kontrol et.
