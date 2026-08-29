import json
def render(payload: dict, as_json: bool) -> str:
    if as_json: return json.dumps(payload,indent=2,sort_keys=True,default=str)
    lines=[f"status: {payload.get('status','UNKNOWN')}"]
    diagnosis=payload.get("diagnosis",payload)
    for item in diagnosis.get("issues",[]): lines.append(f"- {item['severity']} {item['code']}: {item['recommended_action']}")
    if payload.get("backup_path"): lines.append(f"backup: {payload['backup_path']}")
    return "\n".join(lines)

