import sqlite3
from alphaforge.db_doctor.diagnostics import diagnose
from alphaforge.db_doctor.targets import resolve_database_targets
def test_target_conflict(tmp_path): assert resolve_database_targets(tmp_path/"a",{"ALPHAFORGE_SQLITE_PATH":str(tmp_path/"b")})["conflict"]
def test_exposure_never_zero(tmp_path):
 p=tmp_path/"x.db"
 with sqlite3.connect(p) as c: c.executescript("CREATE TABLE positions(id,position_id,symbol,qty,status);CREATE TABLE orders(id,order_id,symbol,status);CREATE TABLE runtime_positions(id,symbol,qty,status);CREATE TABLE runtime_orders(id,order_id,symbol,status);INSERT INTO positions VALUES(1,'p','BTC',1,'OPEN');INSERT INTO runtime_positions VALUES(1,'E',2,'OPEN');")
 r=diagnose(p); assert r["exposure"]["unknown_is_zero"] is False; assert "EXPOSURE_MULTIPLE_ACTIVE_SOURCES" in {x["code"] for x in r["issues"]}
def test_read_only_unknown_evidence(tmp_path):
 p=tmp_path/"x.db"
 with sqlite3.connect(p) as c: c.execute("CREATE TABLE evidence(x)"); c.execute("INSERT INTO evidence VALUES('keep')")
 before=p.read_bytes(); diagnose(p); assert p.read_bytes()==before
