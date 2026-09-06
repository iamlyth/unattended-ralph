#!/usr/bin/env python3
"""Fixture analyzer: validates harmless fixture JSON only."""
import json,pathlib,sys
root=pathlib.Path(sys.argv[1]);value=json.loads((root/"fixture-capability/result.json").read_text())
if value!={"fixture":True,"result":"pass"}:raise SystemExit(1)
print("fixture semantics passed")
