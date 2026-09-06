#!/usr/bin/env python3
"""Operator-only, campaign-scoped archive/prune for terminal factory state.

The command never scans outside the named campaign.  It takes the same
repository-directory flock used by the coordinator, validates every member by
no-follow descriptor walk, writes a digest manifest and tar archive atomically,
and only then removes that one campaign through dirfd-relative operations.
"""
from __future__ import annotations
import argparse,fcntl,hashlib,io,json,os,re,stat,tarfile,tempfile,time
from pathlib import Path,PurePosixPath

NAME=re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
TEMP=re.compile(r"(?:^|/)(?:\.[^/]*\.tmp(?:\..*)?|factory-(?:home|loop-session)-.*)$")
MAX_FILES=4096;MAX_BYTES=256*1024*1024;RETENTION_DEFAULT=20

def die(s): raise SystemExit("archive-factory-campaign: "+s)
def fsync_dir(p):
 fd=os.open(p,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
 try: os.fsync(fd)
 finally: os.close(fd)
def inspect(root:Path,uid:int):
 out=[];total=0
 for current,dirs,files in os.walk(root,topdown=True,followlinks=False):
  dirs.sort();files.sort()
  for n in [*dirs,*files]:
   p=Path(current)/n;rel=p.relative_to(root).as_posix();q=PurePosixPath(rel)
   if any(x in ("",".","..") for x in q.parts) or TEMP.search(rel):die(f"noncanonical campaign entry: {rel}")
   i=os.lstat(p)
   if stat.S_ISLNK(i.st_mode) or i.st_uid!=uid:die(f"unsafe owner or symlink: {rel}")
   if stat.S_ISDIR(i.st_mode):
    if stat.S_IMODE(i.st_mode)!=0o700:die(f"unsafe directory mode: {rel}")
   elif stat.S_ISREG(i.st_mode):
    if i.st_nlink!=1 or stat.S_IMODE(i.st_mode)&0o077:die(f"unsafe file mode/link count: {rel}")
    fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC);h=hashlib.sha256();size=0;chunks=[]
    try:
     before=os.fstat(fd)
     while True:
      b=os.read(fd,65536)
      if not b:break
      size+=len(b);total+=len(b)
      if size>32*1024*1024 or total>MAX_BYTES:die("campaign archive byte bound exceeded")
      chunks.append(b);h.update(b)
     after=os.fstat(fd)
     if (before.st_dev,before.st_ino,before.st_size)!=(after.st_dev,after.st_ino,after.st_size):die(f"member raced: {rel}")
    finally:os.close(fd)
    out.append({"path":rel,"size":size,"mode":stat.S_IMODE(i.st_mode),"sha256":h.hexdigest(),"_bytes":b"".join(chunks)})
   else:die(f"noncanonical campaign member type: {rel}")
   if len(out)+sum(1 for _ in dirs)>MAX_FILES:die("campaign archive member bound exceeded")
 return out

def remove_tree(parent_fd:int,name:str):
 fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent_fd)
 try:
  for entry in os.listdir(fd):
   i=os.stat(entry,dir_fd=fd,follow_symlinks=False)
   if stat.S_ISDIR(i.st_mode):remove_tree(fd,entry)
   elif stat.S_ISREG(i.st_mode):os.unlink(entry,dir_fd=fd)
   else:die(f"member changed type during prune: {entry}")
  os.fsync(fd)
 finally:os.close(fd)
 os.rmdir(name,dir_fd=parent_fd);os.fsync(parent_fd)

def main():
 ap=argparse.ArgumentParser();ap.add_argument("--root",default=".");ap.add_argument("--campaign-id",required=True);ap.add_argument("--retention",type=int,default=RETENTION_DEFAULT);ap.add_argument("--archive-only",action="store_true");a=ap.parse_args()
 if not NAME.fullmatch(a.campaign_id):die("invalid campaign ID")
 if not 1<=a.retention<=100:die("retention must be between 1 and 100")
 root=Path(a.root).resolve();uid=os.getuid();ri=os.lstat(root)
 if not stat.S_ISDIR(ri.st_mode) or ri.st_uid!=uid:die("repository root owner/type is unsafe")
 lock=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
 try:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:die("active campaign lock is held")
  state=root/".factory-state";campaigns=state/"campaigns";campaign=campaigns/a.campaign_id
  for p in (state,campaigns,campaign):
   i=os.lstat(p)
   if not stat.S_ISDIR(i.st_mode) or stat.S_ISLNK(i.st_mode) or i.st_uid!=uid or stat.S_IMODE(i.st_mode)!=0o700:die(f"unsafe campaign path: {p}")
  members=inspect(campaign,uid)
  # Runner evidence is campaign-namespaced by protocol. Include it without
  # assuming any runner/class/capability names; archival never promotes it.
  runner_campaign=state/"runner-evidence"/a.campaign_id
  if runner_campaign.exists():
   ri=os.lstat(runner_campaign)
   if not stat.S_ISDIR(ri.st_mode) or stat.S_ISLNK(ri.st_mode) or ri.st_uid!=uid or stat.S_IMODE(ri.st_mode)!=0o700:die("unsafe campaign runner-evidence namespace")
   for item in inspect(runner_campaign,uid):
    item["path"]="runner-evidence/"+item["path"]
    members.append(item)
  members.sort(key=lambda item:item["path"])
  state_member=next((x for x in members if x["path"]=="factory-loop.json"),None)
  if state_member is None:die("campaign has no canonical resumable/final state")
  try:control=json.loads(state_member["_bytes"])
  except (ValueError,UnicodeError):die("campaign state is malformed")
  terminal={"success","findings","blocked","failed","interrupted","infrastructure_failure"}
  phase=control.get("current_phase",control.get("outcome")) if isinstance(control,dict) else None
  if phase not in terminal:die("active/resumable campaign cannot be archived or pruned")
  if control.get("campaign_id",a.campaign_id)!=a.campaign_id:die("campaign state identity mismatch")
  archives=state/"campaign-archives";archives.mkdir(mode=0o700,exist_ok=True)
  ai=os.lstat(archives)
  if ai.st_uid!=uid or stat.S_IMODE(ai.st_mode)!=0o700 or stat.S_ISLNK(ai.st_mode):die("unsafe archive directory")
  stamp=time.strftime("%Y%m%dT%H%M%SZ",time.gmtime());base=f"{a.campaign_id}-{stamp}"
  manifest_members=[{k:v for k,v in item.items() if k!="_bytes"} for item in members]
  manifest={"schema":"factory-campaign-archive/v1","campaign_id":a.campaign_id,"created_at":stamp,"members":manifest_members}
  with tempfile.NamedTemporaryFile(dir=archives,prefix=".archive.",delete=False) as tf:
   tmp=Path(tf.name)
  try:
   with tarfile.open(tmp,"w") as tar:
    for item in members:
     info=tarfile.TarInfo(f"{a.campaign_id}/{item['path']}");info.size=item["size"];info.mode=item["mode"];info.uid=uid;info.gid=os.getgid();info.mtime=0
     tar.addfile(info,io.BytesIO(item["_bytes"]))
   with open(tmp,"rb") as f:
    os.fsync(f.fileno());archive_sha256=hashlib.sha256(f.read()).hexdigest()
   manifest["archive_sha256"]=archive_sha256
   raw=(json.dumps(manifest,sort_keys=True,separators=(",",":"))+"\n").encode()
   archive=archives/(base+".tar");os.link(tmp,archive);os.unlink(tmp)
   if hashlib.sha256(archive.read_bytes()).hexdigest()!=archive_sha256:die("published archive digest mismatch")
   mtmp=archives/("."+base+".manifest.tmp");fd=os.open(mtmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600);os.write(fd,raw);os.fsync(fd);os.close(fd)
   os.rename(mtmp,archives/(base+".manifest.json"));fsync_dir(archives)
  finally:
   try:tmp.unlink()
   except FileNotFoundError:pass
  if not a.archive_only:
   pfd=os.open(campaigns,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
   try:remove_tree(pfd,a.campaign_id)
   finally:os.close(pfd)
  # Bounded retention is explicit operator policy.  Never auto-delete here:
  # excess archives are reported for a later campaign-ID-scoped invocation.
  existing=sorted(archives.glob(a.campaign_id+"-*.manifest.json"))
  if len(existing)>a.retention:print(f"archive-factory-campaign: retention exceeded ({len(existing)}/{a.retention}); no automatic evidence loss")
  print(archive)
 finally:os.close(lock)
if __name__=="__main__":main()
