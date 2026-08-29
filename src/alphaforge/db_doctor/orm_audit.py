from __future__ import annotations

def _norm_default(value):
    if value is None:return None
    return str(value).strip("()'\"").lower()
def audit_orm(inspected):
    from alphaforge.db.base import Base
    import alphaforge.models.schema, alphaforge.models.ai_schema  # noqa:F401
    mismatches=[]
    for table in Base.metadata.sorted_tables:
        deployed=inspected.get(table.name)
        if not deployed:
            mismatches.append({"table":table.name,"kind":"TABLE_MISSING","orm_columns":sorted(c.name for c in table.columns),"deployed_columns":[]}); continue
        actual={c["name"]:c for c in deployed["columns"]}; expected={c.name:c for c in table.columns}; details={}
        if set(actual)!=set(expected): details["columns"]={"orm":sorted(expected),"deployed":sorted(actual)}
        pk_orm=sorted(c.name for c in table.primary_key.columns); pk_db=sorted(n for n,c in actual.items() if c["pk"])
        if pk_orm!=pk_db: details["primary_key"]={"orm":pk_orm,"deployed":pk_db}
        nullable=[]; defaults=[]
        for name in set(actual)&set(expected):
            if bool(expected[name].nullable)==bool(actual[name]["notnull"]): nullable.append({"column":name,"orm":expected[name].nullable,"deployed":not bool(actual[name]["notnull"])})
            od=_norm_default(expected[name].server_default.arg if expected[name].server_default is not None else None); dd=_norm_default(actual[name]["default"])
            if od is not None and dd is not None and od!=dd: defaults.append({"column":name,"orm":od,"deployed":dd})
        if nullable: details["nullable"]=nullable
        if defaults: details["defaults"]=defaults
        orm_unique={tuple(sorted(c.name for c in u.columns)) for u in table.constraints if getattr(u,"unique",False) or u.__class__.__name__=="UniqueConstraint"}|{(c.name,) for c in table.columns if c.unique}
        db_unique={tuple(sorted(i["columns"])) for i in deployed["indexes"] if i["unique"]}
        if not orm_unique.issubset(db_unique): details["unique"]={"orm":sorted(orm_unique),"deployed":sorted(db_unique)}
        if details:mismatches.append({"table":table.name,"kind":"CONTRACT_MISMATCH",**details})
    return {"tables_audited":sorted(t.name for t in Base.metadata.sorted_tables),"mismatches":mismatches,"autogenerate_safe":not mismatches}
