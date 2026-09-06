#!/usr/bin/env python3
"""Strict root-owned policy for generic signed runner classes.

Production reads /etc/factory-runner/runner-policy.json.  The repository ships
schemas and inert fixtures, never an enrolled production policy, key, account,
capability, device, service, or executable identity.
"""
from __future__ import annotations
import json, os, re, stat
from pathlib import Path, PurePosixPath

POLICY_SCHEMA="factory-runner-policy/v3"
NAME=re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
SHA256=re.compile(r"^[0-9a-f]{64}$")
PIN_FIELDS={"path","sha256","device","inode","status"}
DEFAULT_POLICY_PATH=Path(os.environ.get("FACTORY_RUNNER_POLICY","/etc/factory-runner/runner-policy.json"))
class PolicyError(RuntimeError): pass

def _path(value: object,label: str)->str:
 if not isinstance(value,str) or not value.startswith("/") or value=="/" or ".." in PurePosixPath(value).parts or any(ord(c)<32 for c in value): raise PolicyError(f"{label} is not a safe absolute path")
 return value

def _names(value: object,label: str,*,empty=False)->list[str]:
 if not isinstance(value,list) or (not empty and not value) or len(value)!=len(set(value)) or any(not isinstance(x,str) or not NAME.fullmatch(x) for x in value): raise PolicyError(f"{label} must be a unique canonical identifier list")
 return value

def _argv(value: object,label: str)->list[str]:
 if not isinstance(value,list) or not value or len(value)>64 or any(not isinstance(x,str) or not x or len(x.encode())>512 or "\0" in x or any(ord(c)<32 and c not in "\t" for c in x) for x in value): raise PolicyError(f"{label} is invalid")
 return value

def _pin(value: object,label: str)->dict:
 if not isinstance(value,dict) or set(value)!=PIN_FIELDS or value.get("status")!="enrolled": raise PolicyError(f"{label} is not exactly enrolled")
 _path(value.get("path"),label+".path")
 if not SHA256.fullmatch(str(value.get("sha256",""))) or type(value.get("device")) is not int or value["device"]<0 or type(value.get("inode")) is not int or value["inode"]<=0: raise PolicyError(f"{label} identity is invalid")
 return value

def _resource(value: object,label: str)->dict:
 fields={"devices","dbus","collectors","dedicated_host"}
 if not isinstance(value,dict) or set(value)!=fields or not isinstance(value["dedicated_host"],bool): raise PolicyError(f"{label} fields are invalid")
 devices=value["devices"]
 if not isinstance(devices,list) or len(devices)!=len({json.dumps(x,sort_keys=True) for x in devices}): raise PolicyError(f"{label}.devices is invalid")
 for item in devices:
  if not isinstance(item,dict) or set(item)!={"path","access"} or item["access"] not in {"r","rw"}: raise PolicyError(f"{label}.devices entry is invalid")
  _path(item["path"],label+".devices.path")
 dbus=value["dbus"]
 if dbus is not None:
  if not isinstance(dbus,dict) or set(dbus)!={"bus","address","destination","calls"} or dbus["bus"] not in {"system","session"}: raise PolicyError(f"{label}.dbus is invalid")
  if not all(isinstance(dbus[k],str) and dbus[k] and "\0" not in dbus[k] for k in ("address","destination")): raise PolicyError(f"{label}.dbus endpoint is invalid")
  calls=dbus["calls"]
  if not isinstance(calls,list) or not calls or len(calls)!=len(set(calls)) or any(not isinstance(x,str) or not x or "*" in x for x in calls): raise PolicyError(f"{label}.dbus calls must be an exact nonempty allowlist")
 collectors=value["collectors"]
 if not isinstance(collectors,list): raise PolicyError(f"{label}.collectors is invalid")
 seen=set()
 for item in collectors:
  if not isinstance(item,dict) or set(item)!={"id","argv"} or not NAME.fullmatch(str(item.get("id",""))) or item["id"] in seen: raise PolicyError(f"{label}.collector is invalid")
  _argv(item["argv"],label+".collector.argv");seen.add(item["id"])
 return value

def validate_policy(data: object)->dict:
 if not isinstance(data,dict) or set(data)!={"schema","namespace","classes"} or data.get("schema")!=POLICY_SCHEMA or data.get("namespace")!="factory-runner-receipt": raise PolicyError("runner policy schema/fields are invalid")
 classes=data["classes"]
 if not isinstance(classes,list) or not classes or len(classes)>64: raise PolicyError("runner policy must declare between 1 and 64 classes")
 names=set();uids=set()
 fields={"name","uid","account","workspace_root","allowed_capabilities","broker_helper","probe_authority","probe_authority_sha256","probe_authority_status","signer_key","signer_principal_file","nonce_ledger","systemd_run","systemctl","cgroup_root","approved_groups","executable_pins","resources"}
 for i,e in enumerate(classes):
  label=f"classes[{i}]"
  if not isinstance(e,dict) or set(e)!=fields: raise PolicyError(f"{label} fields are invalid")
  name=e["name"];uid=e["uid"]
  if not isinstance(name,str) or not NAME.fullmatch(name) or name in names: raise PolicyError(f"{label}.name is invalid or duplicated")
  if type(uid)is not int or uid<=0 or uid in uids: raise PolicyError(f"{label}.uid is invalid or duplicated")
  if not isinstance(e["account"],str) or not NAME.fullmatch(e["account"]): raise PolicyError(f"{label}.account is invalid")
  _path(e["workspace_root"],label+".workspace_root");_names(e["allowed_capabilities"],label+".allowed_capabilities");_names(e["approved_groups"],label+".approved_groups")
  for key in ("broker_helper","probe_authority","signer_key","signer_principal_file","nonce_ledger","systemd_run","systemctl","cgroup_root"): _path(e[key],label+"."+key)
  if e["broker_helper"]!="/usr/local/libexec/factory-runner-broker" or e["probe_authority_status"]!="enrolled" or not SHA256.fullmatch(str(e["probe_authority_sha256"])): raise PolicyError(f"{label} authority is not exactly enrolled")
  pins=e["executable_pins"]
  if not isinstance(pins,dict) or not pins or len(pins)>64 or any(not NAME.fullmatch(str(k)) for k in pins): raise PolicyError(f"{label}.executable_pins is invalid")
  for key,pin in pins.items(): _pin(pin,label+".executable_pins."+key)
  resources=e["resources"]
  if not isinstance(resources,dict) or set(resources)!=set(e["allowed_capabilities"]): raise PolicyError(f"{label}.resources must exactly cover capabilities")
  for cap,res in resources.items(): _resource(res,label+".resources."+cap)
  names.add(name);uids.add(uid)
 return data

def _secure_read(path:Path)->bytes:
 fixture="FACTORY_RUNNER_POLICY" in os.environ;owners={os.getuid()} if fixture else {0};current=Path("/")
 for part in path.parts[1:]:
  current/=part;i=os.lstat(current)
  if stat.S_ISLNK(i.st_mode) or i.st_uid not in owners or i.st_mode&0o022: raise PolicyError(f"unsafe policy ancestry: {current}")
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
 try:
  i=os.fstat(fd)
  if not stat.S_ISREG(i.st_mode) or i.st_nlink!=1 or i.st_uid not in owners or i.st_mode&0o022 or i.st_size>1024*1024: raise PolicyError("policy inode is unsafe")
  raw=os.read(fd,i.st_size+1)
 finally: os.close(fd)
 if len(raw)>1024*1024: raise PolicyError("policy exceeds size bound")
 return raw

def load_policy()->dict:
 try:return validate_policy(json.loads(_secure_read(DEFAULT_POLICY_PATH)))
 except PolicyError:raise
 except (OSError,ValueError,UnicodeError) as e:raise PolicyError(f"runner policy unavailable or malformed: {type(e).__name__}") from e

def class_for_uid(policy:dict,uid:int)->dict:
 matches=[x for x in policy["classes"] if x["uid"]==uid]
 if len(matches)!=1:raise PolicyError(f"executing uid {uid} is not bound to exactly one runner class")
 return matches[0]
def class_for_name(policy:dict,name:str)->dict:
 matches=[x for x in policy["classes"] if x["name"]==name]
 if len(matches)!=1:raise PolicyError(f"runner class {name!r} is not uniquely enrolled")
 return matches[0]
