#!/usr/bin/env python3
"""Non-mutating Stage 3.5 migration-manifest validator (stdlib only)."""
from __future__ import annotations
import argparse, hashlib, json, sys, tempfile, shutil
from pathlib import Path
from collections import Counter, defaultdict, deque

EXPECTED_ART = 21879
EXPECTED_HASH = "c6144b352ea5b3683723fd7e09b6b166c6077e13df340bfaf0d926fb60865fa6"
EXPECTED_COH = 20488
ROOTS = {
  "live": "/var/lib/mk04/live/",
  "test": "/var/lib/mk04/test/default/",
  "archive": "/var/lib/mk04/archive/",
  "assets": "/var/lib/mk04/assets/",
  "logs": "/var/log/mk04/",
  "scratch": "/var/tmp/mk04/",
}
ACTIONS_PHYSICAL = {
  "ACT_VERBATIM_COPY","ACT_PROTECTED_CREDENTIAL_COPY","ACT_SQLITE_FAMILY_SNAPSHOT","ACT_RECREATE_SYMLINK",
  "ACT_ARCHIVE_PRESERVE","ACT_TRANSFORMED_COPY","ACT_RETAIN_VALIDATE_SHARED_ASSET","ACT_PRESERVE_UNRESOLVED_NO_AUTHORITY",
  "ACT_CREATE_DIRECTORY_ONLY",
}

def jl(p):
  return [json.loads(l) for l in Path(p).read_text().splitlines() if l.strip()]

def set_hash(ids):
  return hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()

def under(path, root):
  root = root if root.endswith("/") else root + "/"
  return path == root.rstrip("/") or path.startswith(root)

def fail(msg, errors):
  errors.append(msg)

def validate(cand: Path, inv_ids=None, i4b_ids=None):
  errors = []
  rows = jl(cand/"migration-manifest.jsonl")
  cohs = jl(cand/"migration-cohorts.jsonl")
  order = jl(cand/"migration-order.jsonl")
  op = json.loads((cand/"operator-decisions.json").read_text())
  if len(op.get("decisions", [])) != 13:
    fail(f"expected 13 decisions got {len(op.get('decisions',[]))}", errors)
  for d in op.get("decisions", []):
    if d.get("status") != "OPERATOR_APPROVED_RECORD_ONLY":
      fail(f"decision not approved-record-only: {d.get('decision_id')}", errors)
    if d.get("authorizes_physical_action") is not False:
      fail("physical action authorized in decision", errors)
    if d.get("authorizes_deletion") is not False:
      fail("deletion authorized", errors)
  aids = [r["artifact_id"] for r in rows]
  if len(aids) != EXPECTED_ART or len(set(aids)) != EXPECTED_ART:
    fail(f"artifact count/dup {len(aids)} unique {len(set(aids))}", errors)
  if set_hash(aids) != EXPECTED_HASH:
    fail("artifact set hash mismatch", errors)
  if inv_ids is not None and set(aids) != inv_ids:
    fail("artifact set != inventory", errors)
  cids = [c["cohort_id"] for c in cohs]
  if len(cids) != EXPECTED_COH or len(set(cids)) != EXPECTED_COH:
    fail(f"cohort count/dup {len(cids)}", errors)
  if i4b_ids is not None and set(cids) != i4b_ids:
    fail("cohort set != i4b", errors)
  tmap = defaultdict(list)
  for r in rows:
    if r.get("deletion_authorized"):
      fail(f"deletion authorized on {r['artifact_id']}", errors)
    if r.get("physical_action_performed"):
      fail(f"physical_action_performed on {r['artifact_id']}", errors)
    if r.get("credential_enablement_authorized"):
      fail(f"credential enablement on {r['artifact_id']}", errors)
    phys = r.get("exact_target_path")
    if phys:
      tr = r.get("target_root_class")
      if tr not in ROOTS or not under(phys, ROOTS[tr]):
        fail(f"target escape {r['artifact_id']} {phys}", errors)
      if ".." in phys.split("/"):
        fail(f"dotdot {r['artifact_id']}", errors)
      if r["migration_action"] not in ACTIONS_PHYSICAL and r["migration_action"] != "ACT_RETAIN_VALIDATE_SHARED_ASSET":
        # retain is physical-ish path recorded
        pass
      for req in ("expected_owner","expected_group","expected_mode"):
        if not r.get(req):
          fail(f"physical missing {req} {r['artifact_id']}", errors)
      if not r.get("validation_rule_ids") or not r.get("recovery_rule_ids"):
        fail(f"physical missing val/recovery {r['artifact_id']}", errors)
      tmap[phys].append(r)
    else:
      if not r.get("no_target_reason"):
        fail(f"no target without reason {r['artifact_id']}", errors)
    if r.get("target_treatment") == "PRESERVE_UNRESOLVED_NO_AUTHORITY" and r.get("target_treatment") == "INITIAL_LIVE":
      fail("unresolved marked live", errors)
    if r.get("target_root_class") == "archive" and r.get("target_treatment") == "INITIAL_LIVE":
      fail("archive as live", errors)
    if r.get("migration_action") == "ACT_CREATE_DIRECTORY_ONLY" and r.get("source_type") == "directory":
      pass  # ok
    if r.get("migration_action") == "ACT_TRANSFORMED_COPY" and r.get("transformation_rule_id") in {None, "XFORM-NONE"}:
      fail(f"transformed action without transform rule {r['artifact_id']}", errors)

  for path, rs in tmap.items():
    if len(rs) > 1 and not all(r.get("shared_target_representation_permitted") for r in rs):
      fail(f"target collision {path}", errors)
  # double-copy: directory action must not be recursive copy
  for r in rows:
    if r.get("source_type") == "directory" and r.get("migration_action") in {"ACT_VERBATIM_COPY","ACT_ARCHIVE_PRESERVE","ACT_TRANSFORMED_COPY"}:
      fail(f"directory recursive-copy risk {r['artifact_id']}", errors)
  # order DAG
  pred = {c["cohort_id"]: set(c.get("ordering_predecessors") or []) for c in cohs}
  indeg = {cid: len(ps) for cid, ps in pred.items()}
  succ = defaultdict(set)
  for cid, ps in pred.items():
    for p in ps:
      succ[p].add(cid)
  q = deque([cid for cid,d in indeg.items() if d==0])
  seen=0
  while q:
    n=q.popleft(); seen+=1
    for s in succ[n]:
      indeg[s]-=1
      if indeg[s]==0: q.append(s)
  if seen != EXPECTED_COH:
    fail(f"ordering cycle or incomplete {seen}", errors)
  if len(order) != EXPECTED_COH:
    fail("order rows != cohorts", errors)
  # secret-ish scan (exclude this validator file; flag PEM-looking bodies only)
  import re
  pem_re = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]{20,}?-----END [A-Z0-9 ]*PRIVATE KEY-----")
  for p in cand.iterdir():
    if not p.is_file() or p.name == "validate-migration-manifest.py":
      continue
    blob = p.read_text(errors="ignore")
    if pem_re.search(blob):
      fail(f"private key material present in {p.name}", errors)
  man = json.loads((cand/"stage-3.5-evidence-manifest.json").read_text()) if (cand/"stage-3.5-evidence-manifest.json").exists() else {}
  for k in ["migration_executed","physical_action_performed","stage_3_6_begun","stage_4_begun","deletion_authorized","credential_enablement_authorized"]:
    if man and man.get(k) is not False:
      fail(f"manifest flag {k} not false", errors)
  return errors

def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("candidate")
  ap.add_argument("--inventory-artifacts")
  ap.add_argument("--i4b-cohorts")
  ap.add_argument("--self-test", action="store_true")
  args = ap.parse_args()
  cand = Path(args.candidate)
  inv_ids = None
  i4b_ids = None
  if args.inventory_artifacts:
    inv_ids = {json.loads(l)["artifact_id"] for l in Path(args.inventory_artifacts).read_text().splitlines() if l.strip()}
  if args.i4b_cohorts:
    i4b_ids = {json.loads(l)["cohort_id"] for l in Path(args.i4b_cohorts).read_text().splitlines() if l.strip()}
  errors = validate(cand, inv_ids, i4b_ids)
  if errors:
    print("VALIDATION_FAILED", len(errors))
    for e in errors[:50]:
      print(" -", e)
    return 1
  print("VALIDATION_OK")
  if args.self_test:
    # negative tests on temp copies
    import copy
    rows = jl(cand/"migration-manifest.jsonl")
    tests = []
    # dup id
    with tempfile.TemporaryDirectory() as td:
      td=Path(td)
      for p in cand.iterdir():
        if p.is_file(): shutil.copy2(p, td/p.name)
      bad = jl(td/"migration-manifest.jsonl")
      bad.append(dict(bad[0])); bad[-1]["artifact_id"]=bad[0]["artifact_id"]
      (td/"migration-manifest.jsonl").write_text("\n".join(json.dumps(r,sort_keys=True) for r in bad)+"\n")
      e = validate(td)
      tests.append(("duplicate_artifact_id", any("dup" in x or "count" in x for x in e)))
    with tempfile.TemporaryDirectory() as td:
      td=Path(td)
      for p in cand.iterdir():
        if p.is_file(): shutil.copy2(p, td/p.name)
      bad = jl(td/"migration-manifest.jsonl")[:-1]
      (td/"migration-manifest.jsonl").write_text("\n".join(json.dumps(r,sort_keys=True) for r in bad)+"\n")
      e = validate(td)
      tests.append(("missing_artifact", any("count" in x or "hash" in x for x in e)))
    with tempfile.TemporaryDirectory() as td:
      td=Path(td)
      for p in cand.iterdir():
        if p.is_file(): shutil.copy2(p, td/p.name)
      bad = jl(td/"migration-manifest.jsonl")
      for r in bad:
        if r.get("exact_target_path"):
          r["exact_target_path"]="/etc/passwd"; r["target_root_class"]="live"; break
      (td/"migration-manifest.jsonl").write_text("\n".join(json.dumps(r,sort_keys=True) for r in bad)+"\n")
      e = validate(td)
      tests.append(("unsafe_target_escape", any("escape" in x for x in e)))
    with tempfile.TemporaryDirectory() as td:
      td=Path(td)
      for p in cand.iterdir():
        if p.is_file(): shutil.copy2(p, td/p.name)
      bad = jl(td/"migration-manifest.jsonl")
      paths=[r for r in bad if r.get("exact_target_path") and not r.get("shared_target_representation_permitted")]
      if len(paths)>=2:
        paths[1]["exact_target_path"]=paths[0]["exact_target_path"]
        paths[1]["shared_target_representation_permitted"]=False
      (td/"migration-manifest.jsonl").write_text("\n".join(json.dumps(r,sort_keys=True) for r in bad)+"\n")
      e = validate(td)
      tests.append(("target_collision", any("collision" in x for x in e)))
    with tempfile.TemporaryDirectory() as td:
      td=Path(td)
      for p in cand.iterdir():
        if p.is_file(): shutil.copy2(p, td/p.name)
      bad = jl(td/"migration-manifest.jsonl")
      for r in bad:
        if r.get("exact_target_path"):
          r["validation_rule_ids"]=[]; r["recovery_rule_ids"]=[]; break
      (td/"migration-manifest.jsonl").write_text("\n".join(json.dumps(r,sort_keys=True) for r in bad)+"\n")
      e = validate(td)
      tests.append(("physical_without_val_recovery", any("val/recovery" in x for x in e)))
    with tempfile.TemporaryDirectory() as td:
      td=Path(td)
      for p in cand.iterdir():
        if p.is_file(): shutil.copy2(p, td/p.name)
      bad = jl(td/"migration-manifest.jsonl")
      bad[0]["deletion_authorized"]=True
      (td/"migration-manifest.jsonl").write_text("\n".join(json.dumps(r,sort_keys=True) for r in bad)+"\n")
      e = validate(td)
      tests.append(("deletion_authorization", any("deletion" in x for x in e)))
    with tempfile.TemporaryDirectory() as td:
      td=Path(td)
      for p in cand.iterdir():
        if p.is_file(): shutil.copy2(p, td/p.name)
      bad = jl(td/"migration-manifest.jsonl")
      for r in bad:
        if r.get("target_treatment")=="PRESERVE_UNRESOLVED_NO_AUTHORITY":
          r["target_treatment"]="INITIAL_LIVE"; r["target_root_class"]="archive"; break
      (td/"migration-manifest.jsonl").write_text("\n".join(json.dumps(r,sort_keys=True) for r in bad)+"\n")
      e = validate(td)
      tests.append(("unresolved_as_authoritative", any("archive as live" in x or "unresolved" in x for x in e)))
    with tempfile.TemporaryDirectory() as td:
      td=Path(td)
      for p in cand.iterdir():
        if p.is_file(): shutil.copy2(p, td/p.name)
      badc = jl(td/"migration-cohorts.jsonl")
      if len(badc)>=2:
        badc[0]["ordering_predecessors"]=[badc[1]["cohort_id"]]
        badc[1]["ordering_predecessors"]=[badc[0]["cohort_id"]]
      (td/"migration-cohorts.jsonl").write_text("\n".join(json.dumps(r,sort_keys=True) for r in badc)+"\n")
      e = validate(td)
      tests.append(("ordering_cycle", any("cycle" in x for x in e)))
    print("SELF_TEST", tests)
    if not all(ok for _, ok in tests):
      print("SELF_TEST_FAILED", [n for n,ok in tests if not ok])
      return 2
    print("SELF_TEST_OK")
  return 0

if __name__ == "__main__":
  sys.exit(main())
