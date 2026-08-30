# AlphaForge operasyon komutları (dev)

> PAPER/BACKTEST önceliklidir. LIVE, mevcut fail-closed yetkilendirme ve readiness kapıları nedeniyle production için yetkilendirilmiş değildir. Bu rehberdeki doğrulamalar emir göndermez, değiştirmez veya iptal etmez.

## Tek veritabanı sözleşmesi

Yeni kurulumun varsayılan SQLite dosyası `data/runtime/alphaforge_runtime.db`'dir. Repo kökündeki `alphaforge.db` legacy/non-canonical'dır ve AlphaForge varsayılan akışları tarafından yeni oluşturulmaz. Mevcut legacy veya özel DB otomatik taşınmaz/silinmez.

Öncelik: burn-in komutunda `--db`; `ALPHAFORGE_DATABASE_URL`; uyumluluk için `ALPHAFORGE_DB_PATH`; canonical varsayılan. Normal kullanımda birden fazla değişkeni eşitlemeyin. Alembic ve runtime aynı URL çözümleyicisini kullanır.

```bash
DB="data/runtime/alphaforge_runtime.db"
```

```powershell
$DB = "data/runtime/alphaforge_runtime.db"
```

## Temiz kurulum / ilk PAPER çalıştırma

### macOS/Linux (bash/zsh)

```bash
git clone https://github.com/werim/AlphaForge.git
cd AlphaForge
git switch dev
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.medium.example .env
# .env içinde yalnız READ-ONLY Binance anahtarlarını ayarlayın.
export ALPHAFORGE_EXECUTION_MODE=PAPER
export ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION=true
DB="data/runtime/alphaforge_runtime.db"
python - <<'PY'
import os
print("RECON=" + os.getenv("ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION", "").lower())
print("KEY=" + str(bool(os.getenv("BINANCE_API_KEY", "").strip())))
print("SECRET=" + str(bool(os.getenv("BINANCE_API_SECRET", "").strip())))
PY
alembic upgrade head
python -m alphaforge.db_doctor --db "$DB" diagnose
python -m alphaforge.burnin_ops --db "$DB" preflight --release-id dev-burnin --symbols BTCUSDT ETHUSDT --intervals 1h 4h
python -m alphaforge.burnin_ops --db "$DB" launch --release-id dev-burnin --symbols BTCUSDT ETHUSDT --intervals 1h 4h --duration-days 7
# launch çıktısındaki campaign_id (CID) değerini alın:
CID="camp_..."
python -m alphaforge.burnin_ops --db "$DB" status --campaign-id "$CID"
python -m alphaforge.burnin_ops --db "$DB" health --campaign-id "$CID"
tail -f "artifacts/burnin/$CID/worker.stderr.log"
```

### Windows PowerShell

```powershell
git clone https://github.com/werim/AlphaForge.git
Set-Location AlphaForge
git switch dev
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.medium.example .env
# .env içinde yalnız READ-ONLY Binance anahtarlarını ayarlayın.
$env:ALPHAFORGE_EXECUTION_MODE = "PAPER"
$env:ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION = "true"
$DB = "data/runtime/alphaforge_runtime.db"
python -c "import os; print('RECON='+os.getenv('ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION','').lower()); print('KEY='+str(bool(os.getenv('BINANCE_API_KEY','').strip()))); print('SECRET='+str(bool(os.getenv('BINANCE_API_SECRET','').strip())))"
alembic upgrade head
python -m alphaforge.db_doctor --db $DB diagnose
python -m alphaforge.burnin_ops --db $DB preflight `
  --release-id dev-burnin `
  --symbols BTCUSDT ETHUSDT `
  --intervals 1h 4h
python -m alphaforge.burnin_ops --db $DB launch `
  --release-id dev-burnin `
  --symbols BTCUSDT ETHUSDT `
  --intervals 1h 4h `
  --duration-days 7
$CID = "camp_..."
python -m alphaforge.burnin_ops --db $DB status --campaign-id $CID
python -m alphaforge.burnin_ops --db $DB health --campaign-id $CID
Get-Content "artifacts/burnin/$CID/worker.stderr.log" -Wait
```

`ALPHAFORGE_EXECUTION_MODE` canonical değişkendir. `EXECUTION_MODE` yalnız deprecated compatibility alias'ıdır. `ALPHAFORGE_MODE` kullanılmaz.

## Mevcut tanı/operasyon komutları

Komutları sürümünüzde doğrulayın:

```bash
python -m alphaforge.burnin_ops --help
python -m alphaforge.db_doctor --help
python -m alphaforge.burnin_ops --db "$DB" diagnose-db
python -m alphaforge.burnin_ops --db "$DB" db-doctor diagnose
python -m alphaforge.db_doctor --db "$DB" diagnose
python -m alphaforge.db_doctor --db "$DB" plan --json
alembic current
alembic heads
```

`repair` mutasyon yapar; önce `plan` çıktısını inceleyin ve yedek politikanızı uygulayın. Runtime/backtest/dashboard giriş noktalarını kurulu sürümde `python -m alphaforge.runtime --help`, `python backtest_order.py --help` ve `python -m alphaforge.dashboard.app --help` ile doğrulayın; desteklenmeyen seçenek üretmeyin.

Burn-in yaşam döngüsü komutları aynı `$DB` ile çalıştırılmalıdır:

```bash
python -m alphaforge.burnin_ops --db "$DB" audit --campaign-id "$CID"
python -m alphaforge.burnin_ops --db "$DB" report --campaign-id "$CID"
python -m alphaforge.burnin_ops --db "$DB" recovery-drill --campaign-id "$CID"
python -m alphaforge.burnin_ops --db "$DB" finalize --campaign-id "$CID"
```

PowerShell'de `"$DB"` yerine `$DB`, `"$CID"` yerine `$CID` kullanın.

## BACKTEST

BACKTEST signed reconciliation credential gerektirmez; PAPER'a ait reconciliation bayrağının BACKTEST kararı üzerinde davranışsal etkisi yoktur.

```bash
export ALPHAFORGE_EXECUTION_MODE=BACKTEST
python backtest_order.py --help
```

## Sorun giderme

### `EXCHANGE_RECONCILIATION_UNAVAILABLE`

Runtime gerekli authenticated, signed, read-only Binance snapshot'ını alamamıştır. Muhtemel nedenler: bayrak kapalı, eksik/placeholder/partial credentials, yanlış Binance environment/base URL, izin veya ağ/auth hatası. Yukarıdaki boolean komutla secret değerlerini yazdırmadan kontrol edin ve preflight'ı tekrar çalıştırın. Preflight artık aynı capability probe tamamlanmadan PASS vermez. Probe yalnız `positionRisk`, `openOrders` ve gerektiğinde `userTrades` gibi non-mutating GET uçlarını kullanır; emir mutasyonu yapmaz.

### DB path ayrışması

```bash
python - <<'PY'
from alphaforge.config import load_config_from_env
from alphaforge.database_defaults import sqlite_path_from_url
u=load_config_from_env().persistence.database_url
print("URL="+u); print("PATH="+str(sqlite_path_from_url(u)))
PY
test ! -e ./alphaforge.db && echo "legacy root DB absent"
alembic current
alembic heads
python -m alphaforge.burnin_ops --db "$DB" status --campaign-id "$CID"
```

PowerShell dosya kontrolü: `Test-Path .\alphaforge.db`; canonical kontrol: `Resolve-Path $DB`. Root legacy dosya varsa otomatik silmeyin/taşımayın; önce hangi operator override'ın onu kullandığını araştırın. Özel `ALPHAFORGE_DATABASE_URL` kullanan operatörlerin değişiklik yapması gerekmez.
