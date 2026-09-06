#!/usr/bin/env python3
"""Harmless stdlib-only runner protocol fixture; never acceptance evidence."""
import json,os,pathlib,sys
if sys.argv[1:]==["gate"]:
 print("fixture gate passed")
elif sys.argv[1:]==["probe"]:
 out=pathlib.Path(os.environ["FACTORY_RUNNER_ARTIFACT_DIR"]);out.mkdir(parents=True,exist_ok=True)
 (out/"result.json").write_text(json.dumps({"fixture":True,"result":"pass"})+"\n")
 print("fixture probe passed")
else:raise SystemExit(2)
