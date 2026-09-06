#!/usr/bin/env python3
"""Descriptor-held loader for an externally installed root probe authority."""
from __future__ import annotations
import hashlib,json,os,re,stat
from dataclasses import dataclass
from pathlib import Path,PurePosixPath
SHA256=re.compile(r"^[0-9a-f]{64}$");NAME=re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
MAX_FILE=8*1024*1024;MAX_FILES=256;MAX_TOTAL=64*1024*1024
class AuthorityError(RuntimeError):pass

def _chain(path:Path,fixture=False):
 if not path.is_absolute():raise AuthorityError("authority path must be absolute")
 owners={0,os.getuid()} if fixture else {0};cur=Path("/")
 for part in path.parts[1:]:
  cur/=part;i=os.lstat(cur)
  if stat.S_ISLNK(i.st_mode) or i.st_uid not in owners or i.st_mode&0o022:raise AuthorityError(f"unsafe authority component: {cur}")
@dataclass
class AuthorityBundle:
 root:Path;document:dict;files:dict[str,tuple[int,bytes]];digest:str;fixture:bool=False
 def close(self):
  for fd,_ in self.files.values():
   try:os.close(fd)
   except OSError:pass
  self.files.clear()
 def bytes(self,relative):
  try:return self.files[relative][1]
  except KeyError as e:raise AuthorityError(f"authority file is not pinned: {relative}") from e
 def path(self,relative):
  if relative not in self.files:raise AuthorityError(f"authority file is not pinned: {relative}")
  path=self.root/relative;_chain(path,self.fixture);named=os.lstat(path);fd,data=self.files[relative];opened=os.fstat(fd)
  if (named.st_dev,named.st_ino)!=(opened.st_dev,opened.st_ino) or hashlib.sha256(data).hexdigest()!=self.document["files"][relative]:raise AuthorityError(f"authority file changed: {relative}")
  return path
 def revalidate(self):
  _chain(self.root,self.fixture)
  for name in self.files:self.path(name)
 def class_contract(self,name):
  try:return self.document["classes"][name]
  except KeyError as e:raise AuthorityError(f"authority has no class {name}") from e
 def analyzer(self,semantic_id):
  try:return self.document["analyzers"][semantic_id]
  except KeyError as e:raise AuthorityError(f"unknown externally enrolled semantic analyzer: {semantic_id}") from e

def _argv(value,held):
 if not isinstance(value,list) or not value or any(not isinstance(x,str) or not x or "\0" in x for x in value):raise AuthorityError("authority argv is invalid")
 if value[0].startswith("@/") and value[0][2:] not in held:raise AuthorityError("authority argv references an unpinned file")
def load_authority(root:Path,expected_digest:str,*,fixture=False)->AuthorityBundle:
 if not root.is_absolute() or not SHA256.fullmatch(expected_digest):raise AuthorityError("authority root/digest is invalid")
 _chain(root,fixture);mp=root/"authority.json";fd=os.open(mp,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
 try:
  i=os.fstat(fd)
  if not stat.S_ISREG(i.st_mode) or i.st_nlink!=1 or i.st_size>MAX_FILE:raise AuthorityError("authority manifest inode is unsafe")
  raw=os.read(fd,i.st_size+1)
 finally:os.close(fd)
 if hashlib.sha256(raw).hexdigest()!=expected_digest:raise AuthorityError("authority manifest digest mismatch")
 try:data=json.loads(raw)
 except Exception as e:raise AuthorityError("authority manifest is invalid JSON") from e
 if not isinstance(data,dict) or set(data)!={"schema","version","files","classes","trusted_path","analyzers"} or data.get("schema")!="factory-probe-authority/v1" or type(data.get("version"))is not int or data["version"]<1:raise AuthorityError("authority manifest schema/fields are invalid")
 pins=data["files"]
 if not isinstance(pins,dict) or not pins or len(pins)>MAX_FILES:raise AuthorityError("authority file table is invalid")
 held={};total=0
 try:
  for rel,want in sorted(pins.items()):
   p=PurePosixPath(rel)
   if not isinstance(rel,str) or p.is_absolute() or any(x in ("",".","..") for x in p.parts) or not SHA256.fullmatch(str(want)):raise AuthorityError("authority file descriptor is invalid")
   path=root.joinpath(*p.parts);_chain(path,fixture);f=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC);i=os.fstat(f);body=os.read(f,i.st_size+1);total+=len(body)
   if not stat.S_ISREG(i.st_mode) or i.st_nlink!=1 or i.st_size>MAX_FILE or total>MAX_TOTAL or hashlib.sha256(body).hexdigest()!=want:os.close(f);raise AuthorityError(f"authority file is unsafe or mismatched: {rel}")
   held[rel]=(f,body)
  analyzers=data["analyzers"]
  if not isinstance(analyzers,dict):raise AuthorityError("analyzer registry is invalid")
  for aid,desc in analyzers.items():
   if not NAME.fullmatch(aid) or not isinstance(desc,dict) or set(desc)!={"argv","sha256"}:raise AuthorityError("analyzer descriptor is invalid")
   _argv(desc["argv"],held)
   if not SHA256.fullmatch(str(desc["sha256"])):raise AuthorityError("analyzer pin is invalid")
   executable=desc["argv"][0][2:] if desc["argv"][0].startswith("@/") else None
   if executable and hashlib.sha256(held[executable][1]).hexdigest()!=desc["sha256"]:raise AuthorityError("analyzer executable digest mismatch")
  classes=data["classes"]
  if not isinstance(classes,dict) or not classes:raise AuthorityError("authority classes are invalid")
  for cname,entry in classes.items():
   if not NAME.fullmatch(cname) or not isinstance(entry,dict) or set(entry)!={"capabilities","gate"}:raise AuthorityError("authority class descriptor is invalid")
   for cap,desc in entry["capabilities"].items():
    if not NAME.fullmatch(cap) or not isinstance(desc,dict) or set(desc)!={"argv","artifacts","semantic_id","probe_id","descriptor_sha256"}:raise AuthorityError("capability descriptor is invalid")
    _argv(desc["argv"],held)
    if desc["semantic_id"] not in analyzers:raise AuthorityError("capability names unknown semantic analyzer")
    core={k:desc[k] for k in ("argv","artifacts","semantic_id")};digest=hashlib.sha256(json.dumps(core,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    if desc["probe_id"]!=f"factory-root-probe:{cname}:{cap}:v1" or desc["descriptor_sha256"]!=digest:raise AuthorityError("capability probe binding is invalid")
   gate=entry["gate"]
   if not isinstance(gate,dict) or set(gate)!={"argv","probe_id","descriptor_sha256"}:raise AuthorityError("gate descriptor is invalid")
   _argv(gate["argv"],held);core={"argv":gate["argv"]};digest=hashlib.sha256(json.dumps(core,sort_keys=True,separators=(",",":")).encode()).hexdigest()
   if gate["probe_id"]!=f"factory-root-probe:{cname}:gate:v1" or gate["descriptor_sha256"]!=digest:raise AuthorityError("gate binding is invalid")
  trusted=data["trusted_path"]
  if not isinstance(trusted,list) or not trusted:raise AuthorityError("trusted path is invalid")
  for value in trusted:_chain(Path(value),fixture)
  return AuthorityBundle(root,data,held,expected_digest,fixture)
 except Exception:
  for f,_ in held.values():
   try:os.close(f)
   except OSError:pass
  raise
