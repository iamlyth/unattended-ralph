#!/usr/bin/env python3
"""Privileged ForcedCommand broker for generic signed runner evidence.

Candidate bytes are only source.  Root policy supplies classes, probes,
analyzers, executable/resource pins and signer identity.  Each operation uses a
separate bounded writable area and systemd cgroup/mount/PID containment.
"""
from __future__ import annotations
import base64,fcntl,grp,hashlib,io,json,os,pwd,re,resource,selectors,shutil,signal,stat,subprocess,sys,tarfile,tempfile,time
from pathlib import Path,PurePosixPath
BUNDLE="/usr/local/libexec/factory-runner-v2.bundle";sys.path.insert(0,BUNDLE if os.path.isdir(BUNDLE) else os.path.dirname(os.path.abspath(__file__)))
from factory_runner_policy import PolicyError,class_for_uid,load_policy
from factory_runner_authority import AuthorityError,load_authority
from factory_runner_artifacts import ArtifactError,PROTOCOL,MAX_ARTIFACTS,MAX_ARTIFACT_FILE,MAX_ARTIFACT_BYTES,collect,descriptors_digest,hold,validate_descriptors
SHA1=re.compile(r"^[0-9a-f]{40}$");SHA256=re.compile(r"^[0-9a-f]{64}$");NAME=re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
MAX_HEADER=65536;MAX_ARCHIVE=128*1024*1024;MAX_FILES=10000;MAX_CONTENT=256*1024*1024;MAX_LOG=4*1024*1024
NONCE_TTL=900;NONCE_OUTSTANDING=32;READ_HEADER=15;READ_ARCHIVE=120;SIGNER="/usr/local/libexec/factory-runner-signer"
_PIN_FDS=[]
def _pass_fds(cmd):return tuple(sorted({int(x.rsplit('/',1)[1]) for x in cmd if isinstance(x,str) and x.startswith('/proc/self/fd/') and x.rsplit('/',1)[1].isdigit()}))
class BrokerError(RuntimeError):pass
def emit(v):print(json.dumps(v,sort_keys=True,separators=(",",":")),flush=True)
def fail(s):emit({"schema":"factory-runner-error/v1","result":"fail","error":s});raise SystemExit(1)
def caller_uid():
 try:uid=int(os.environ["SUDO_UID"])
 except Exception:fail("broker requires an exact sudo caller UID")
 if uid<=0 or (os.geteuid()!=0 and os.environ.get("FACTORY_BROKER_TEST_MODE")!="1"):fail("broker requires root for a non-root caller")
 return uid
def _read(fd,n,deadline):
 out=bytearray();sel=selectors.DefaultSelector();sel.register(fd,selectors.EVENT_READ)
 try:
  while len(out)<n:
   left=deadline-time.monotonic()
   if left<=0 or not sel.select(left):raise BrokerError("request read deadline exceeded")
   b=os.read(fd,min(65536,n-len(out)))
   if not b:break
   out+=b
 finally:sel.close()
 return bytes(out)
def header():
 raw=bytearray();deadline=time.monotonic()+READ_HEADER
 while b"\n" not in raw and len(raw)<=MAX_HEADER:raw+=_read(0,1,deadline)
 if not raw.endswith(b"\n") or len(raw)>MAX_HEADER+1:raise BrokerError("request header invalid")
 return json.loads(raw)
def _ledger(entry):
 root=Path(entry["nonce_ledger"]);root.mkdir(mode=0o700,parents=True,exist_ok=True);fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC);lock=os.open("lock",os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=fd);fcntl.flock(lock,fcntl.LOCK_EX)
 for name in ("issued","used"):
  try:os.mkdir(name,0o700,dir_fd=fd)
  except FileExistsError:pass
 return fd,lock,os.open("issued",os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd),os.open("used",os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
def issue(entry,req,uid):
 if set(req)!={"schema","runner","campaign_id","readiness_nonce"} or req.get("schema")!="factory-runner-nonce-request/v1" or req["runner"]!=entry["name"] or not NAME.fullmatch(str(req["campaign_id"])) or not SHA256.fullmatch(str(req["readiness_nonce"])):raise BrokerError("nonce request binding invalid")
 root,lock,issued,used=_ledger(entry)
 try:
  now=int(time.time());records=[]
  for name in os.listdir(issued):
   try:i=os.stat(name,dir_fd=issued,follow_symlinks=False)
   except OSError:raise BrokerError("nonce ledger raced")
   if now-int(i.st_mtime)>NONCE_TTL:os.unlink(name,dir_fd=issued)
   else:records.append(name)
  if len(records)>=NONCE_OUTSTANDING:raise BrokerError("nonce issuance bound reached")
  nonce=hashlib.sha256(os.urandom(64)).hexdigest();record={"uid":uid,"runner":entry["name"],"campaign_id":req["campaign_id"],"readiness_nonce":req["readiness_nonce"],"issued_at":now};f=os.open(nonce,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=issued);os.write(f,(json.dumps(record,sort_keys=True)+"\n").encode());os.fsync(f);os.close(f);os.fsync(issued)
 finally:
  for x in (used,issued,lock,root):os.close(x)
 emit({"schema":"factory-runner-nonce/v1","nonce":nonce,"runner":entry["name"],"campaign_id":req["campaign_id"],"readiness_nonce":req["readiness_nonce"]})
def consume(entry,req,uid):
 root,lock,issued,used=_ledger(entry)
 try:
  f=os.open(req["nonce"],os.O_RDONLY|os.O_NOFOLLOW,dir_fd=issued);raw=os.read(f,4097);i=os.fstat(f);os.close(f);record=json.loads(raw);now=int(time.time())
  expected={"uid":uid,"runner":entry["name"],"campaign_id":req["campaign_id"],"readiness_nonce":req["readiness_nonce"]}
  if not stat.S_ISREG(i.st_mode) or i.st_nlink!=1 or any(record.get(k)!=v for k,v in expected.items()) or not 0<=now-record.get("issued_at",0)<=NONCE_TTL:raise BrokerError("nonce stale or mismatched")
  os.rename(req["nonce"],req["nonce"],src_dir_fd=issued,dst_dir_fd=used);os.fsync(issued);os.fsync(used)
 except (OSError,ValueError) as e:raise BrokerError("nonce absent or already consumed") from e
 finally:
  for x in (used,issued,lock,root):os.close(x)
def extract(raw,dest):
 count=total=0
 with tarfile.open(fileobj=io.BytesIO(raw),mode="r:") as tf:
  for m in tf:
   count+=1;p=PurePosixPath(m.name)
   if count>MAX_FILES or p.is_absolute() or not p.parts or any(x in ("",".","..") for x in p.parts) or not (m.isdir() or m.isfile()):raise BrokerError("archive member rejected")
   target=dest.joinpath(*p.parts)
   if m.isdir():target.mkdir(parents=True,exist_ok=True);continue
   total+=m.size
   if total>MAX_CONTENT:raise BrokerError("archive content limit exceeded")
   target.parent.mkdir(parents=True,exist_ok=True);src=tf.extractfile(m);fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o755 if m.mode&0o111 else 0o644)
   try:
    left=m.size
    while left:
     b=src.read(min(65536,left))
     if not b:raise BrokerError("archive member shortened")
     os.write(fd,b);left-=len(b)
   finally:os.close(fd)
def freeze(root):
 for parent,dirs,files in os.walk(root,topdown=False):
  for n in files:
   p=Path(parent)/n;i=p.lstat()
   if not stat.S_ISREG(i.st_mode) or i.st_nlink!=1:raise BrokerError("candidate inode unsafe")
   os.chown(p,0,0);p.chmod(0o555 if i.st_mode&0o111 else 0o444)
  for n in dirs:(Path(parent)/n).chmod(0o555)
 root.chmod(0o555)
def argv(authority,items,product,artifacts,commit="",tree=""):
 out=[]
 for x in items:
  x=x.replace("{product}",str(product)).replace("{artifacts}",str(artifacts)).replace("{commit}",commit).replace("{tree}",tree)
  if x.startswith("@/"):x=str(authority.path(x[2:]))
  out.append(x)
 return out
def _limits():
 resource.setrlimit(resource.RLIMIT_CORE,(0,0));resource.setrlimit(resource.RLIMIT_FSIZE,(MAX_LOG,MAX_LOG));resource.setrlimit(resource.RLIMIT_NOFILE,(128,128));resource.setrlimit(resource.RLIMIT_NPROC,(256,256))
def bounded(cmd,env,cwd,limit):
 """Spool output under kernel limits; always kill/reap the process group."""
 with tempfile.TemporaryFile() as out_file,tempfile.TemporaryFile() as err_file:
  p=subprocess.Popen(cmd,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=out_file,stderr=err_file,start_new_session=True,preexec_fn=_limits,pass_fds=_pass_fds(cmd))
  timed=False
  try:p.wait(timeout=limit)
  except subprocess.TimeoutExpired:timed=True
  if timed:
   try:os.killpg(p.pid,signal.SIGTERM)
   except ProcessLookupError:pass
   try:p.wait(timeout=2)
   except subprocess.TimeoutExpired:
    try:os.killpg(p.pid,signal.SIGKILL)
    except ProcessLookupError:pass
    p.wait(timeout=5)
  out_file.seek(0);err_file.seek(0);out=out_file.read(MAX_LOG+1);err=err_file.read(MAX_LOG+1)
 if timed:raise BrokerError("operation deadline exceeded")
 if len(out)>MAX_LOG or len(err)>MAX_LOG:raise BrokerError("output limit exceeded")
 if p.returncode:raise BrokerError("contained operation failed")
 return out,err
def executable(entry,name):
 try:pin=entry["executable_pins"][name]
 except KeyError as e:raise BrokerError(f"missing executable enrollment: {name}") from e
 path=Path(pin["path"])
 if not path.is_absolute():raise BrokerError(f"executable pin is not absolute: {name}")
 cur=Path('/')
 for part in path.parts[1:-1]:
  cur/=part;i=os.stat(cur,follow_symlinks=False)
  if not stat.S_ISDIR(i.st_mode) or i.st_uid!=0 or i.st_mode&0o022:raise BrokerError(f"executable pin ancestry is mutable: {name}")
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC);i=os.fstat(fd);h=hashlib.sha256();off=0
 while True:
  b=os.pread(fd,65536,off)
  if not b:break
  h.update(b);off+=len(b)
 if not stat.S_ISREG(i.st_mode) or i.st_uid!=0 or i.st_mode&0o022 or (i.st_dev,i.st_ino,h.hexdigest())!=(pin["device"],pin["inode"],pin["sha256"]):os.close(fd);raise BrokerError(f"executable pin substitution: {name}")
 _PIN_FDS.append(fd);return f"/proc/self/fd/{fd}"

class WritablePool:
 """One root-mounted byte/inode-bounded tmpfs per operation."""
 def __init__(self,entry,runroot,uid):
  self.root=runroot/"writable";self.root.mkdir(mode=0o700);self.mount=executable(entry,"mount");self.umount=executable(entry,"umount");self.mounted=False
  opts="size=768M,nr_inodes=65536,mode=0700,uid=0,gid=0,nosuid,nodev,noexec"
  cmd=[self.mount,"-t","tmpfs","-o",opts,"factory-runner-writable",str(self.root)];result=subprocess.run(cmd,capture_output=True,timeout=10,pass_fds=_pass_fds(cmd))
  if result.returncode:raise BrokerError("bounded writable tmpfs unavailable")
  self.mounted=True
  for name in ("home","build","output"):
   p=self.root/name;p.mkdir(mode=0o700);os.chown(p,uid,uid)
 def paths(self):return self.root/"home",self.root/"build",self.root/"output"
 def close(self):
  if self.mounted:
   cmd=[self.umount,str(self.root)];result=subprocess.run(cmd,capture_output=True,timeout=10,pass_fds=_pass_fds(cmd))
   if result.returncode:raise BrokerError("bounded writable tmpfs cleanup not proven")
   self.mounted=False
  self.root.rmdir()

def resource_props(res):
 props=["PrivateDevices=yes"]
 if res["devices"]:
  props=["PrivateDevices=no","DevicePolicy=closed",*[f"DeviceAllow={x['path']} {x['access']}" for x in res["devices"]]]
 return props
def run_operation(entry,authority,desc,name,source,runroot,resource,uid):
 pool=WritablePool(entry,runroot,uid);home,build,output=pool.paths()
 if name!="gate": (output/name).mkdir(mode=0o700);os.chown(output/name,uid,uid)
 env={"HOME":"/run/factory/home","PATH":":".join(authority.document["trusted_path"]),"LANG":"C.UTF-8","LC_ALL":"C.UTF-8","FACTORY_PRODUCT_ROOT":"/run/factory/source","FACTORY_BUILD_ROOT":"/run/factory/build","FACTORY_RUNNER_ARTIFACT_DIR":f"/run/factory/output/{name}"}
 command=argv(authority,desc["argv"],Path("/run/factory/source"),Path("/run/factory/output"));systemd=executable(entry,"systemd-run");systemctl=executable(entry,"systemctl")
 props=["NoNewPrivileges=yes","CapabilityBoundingSet=","AmbientCapabilities=","ProtectSystem=strict","ProtectHome=yes","PrivateTmp=yes","PrivateMounts=yes","PrivatePIDs=yes","PrivateIPC=yes","ProtectKernelTunables=yes","ProtectKernelModules=yes","ProtectControlGroups=yes","RestrictSUIDSGID=yes","RestrictNamespaces=yes","IPAddressDeny=any","KillMode=control-group","TasksMax=256","MemoryMax=2G","RuntimeMaxSec=1800","TimeoutStopSec=10","LimitFSIZE=50331648",f"BindReadOnlyPaths={source}:/run/factory/source",f"BindReadOnlyPaths={authority.root}:{authority.root}",f"BindPaths={build}:/run/factory/build",f"BindPaths={home}:/run/factory/home",f"BindPaths={output}:/run/factory/output","WorkingDirectory=/run/factory/source",*resource_props(resource)]
 # D-Bus is default-deny. When declared, only an independently pinned proxy
 # and the exact destination/member allowlist are exposed; never the host bus.
 proxy=None;dbus=resource["dbus"]
 if dbus is not None:
  proxy_root=runroot/"dbus";proxy_root.mkdir(mode=0o700);socket=proxy_root/"bus.sock";proxy_exe=executable(entry,"dbus-proxy")
  pargv=[proxy_exe,dbus["address"],str(socket),"--filter",*[f"--call={dbus['destination']}={member}" for member in dbus["calls"]]]
  proxy=subprocess.Popen(pargv,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True,preexec_fn=_limits,pass_fds=_pass_fds(pargv))
  deadline=time.monotonic()+5
  while time.monotonic()<deadline and not socket.exists() and proxy.poll() is None:time.sleep(.02)
  if not socket.exists() or proxy.poll() is not None:raise BrokerError("enrolled D-Bus proxy failed closed")
  os.chown(socket,uid,uid);props.append(f"BindReadOnlyPaths={socket}:/run/factory/dbus.sock");env["DBUS_SYSTEM_BUS_ADDRESS" if dbus["bus"]=="system" else "DBUS_SESSION_BUS_ADDRESS"]="unix:path=/run/factory/dbus.sock"
 unit=f"factory-runner-{entry['name']}-{os.urandom(8).hex()}.service";cmd=[systemd,"--quiet","--wait","--pipe","--collect","--unit",unit,"--service-type=exec",f"--uid={entry['uid']}",*[f"--property={x}" for x in props],*[f"--setenv={k}={v}" for k,v in env.items()],*command]
 try:return bounded(cmd,{"PATH":"/usr/bin:/bin","LANG":"C.UTF-8"},Path("/"),1900)+(output,unit,pool)
 except BaseException:
  stopcmd=[systemctl,"stop",unit];subprocess.run(stopcmd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15,pass_fds=_pass_fds(stopcmd))
  pool.close();raise
 finally:
  if proxy is not None:
   try:os.killpg(proxy.pid,signal.SIGTERM);proxy.wait(timeout=3)
   except (ProcessLookupError,subprocess.TimeoutExpired):
    try:os.killpg(proxy.pid,signal.SIGKILL)
    except ProcessLookupError:pass
    proxy.wait(timeout=3)
def sign(evidence,entry):
 token=os.urandom(32);r,w=os.pipe();os.write(w,token);os.close(w);payload={"schema":"factory-runner-sign-request/v1","broker_auth_sha256":hashlib.sha256(token).hexdigest(),"manifest":evidence};env={"SUDO_UID":str(entry["uid"]),"PATH":"/usr/bin:/bin"}
 if os.environ.get("FACTORY_BROKER_TEST_MODE")=="1":env.update({"FACTORY_RUNNER_POLICY":os.environ["FACTORY_RUNNER_POLICY"],"FACTORY_SIGNER_TEST_MODE":"1"})
 try:p=subprocess.run([SIGNER,"--broker-fd",str(r)],input=(json.dumps(payload,separators=(",",":"))+"\n").encode(),capture_output=True,timeout=120,pass_fds=(r,),env=env)
 finally:os.close(r)
 if p.returncode:raise BrokerError("broker-only signer rejected request")
 return json.loads(p.stdout)
def main():
 uid=caller_uid()
 try:
  policy=load_policy();entry=class_for_uid(policy,uid);account=pwd.getpwuid(uid)
  if account.pw_name!=entry["account"]:raise BrokerError("numeric UID/account/class mapping differs from policy")
  actual={grp.getgrgid(g).gr_name for g in os.getgrouplist(account.pw_name,account.pw_gid)}
  if actual!=set(entry["approved_groups"]):raise BrokerError("account group set differs from dedicated-host policy")
  req=header()
  if req.get("schema")=="factory-runner-nonce-request/v1":issue(entry,req,uid);return 0
  fields={"schema","runner","class","commit","commit_object_b64","tree","environment_blob","archive_sha256","archive_size","capabilities","campaign_id","readiness_nonce","nonce","authority_sha256"}
  if not isinstance(req,dict) or set(req)!=fields or req.get("schema")!="factory-runner-request/v2" or req["runner"]!=entry["name"] or req["class"]!=entry["name"] or req["capabilities"]!=sorted(entry["allowed_capabilities"]):raise BrokerError("request differs from exact class policy")
  if any(not SHA1.fullmatch(str(req[x])) for x in ("commit","tree","environment_blob")) or any(not SHA256.fullmatch(str(req[x])) for x in ("archive_sha256","readiness_nonce","nonce","authority_sha256")) or req["authority_sha256"]!=entry["probe_authority_sha256"]:raise BrokerError("request binding invalid")
  consume(entry,req,uid);size=req["archive_size"]
  if type(size)is not int or not 0<size<=MAX_ARCHIVE:raise BrokerError("archive size invalid")
  raw=_read(0,size,time.monotonic()+READ_ARCHIVE)
  if len(raw)!=size or hashlib.sha256(raw).hexdigest()!=req["archive_sha256"]:raise BrokerError("archive framing/digest invalid")
  authority=load_authority(Path(entry["probe_authority"]),entry["probe_authority_sha256"],fixture=os.environ.get("FACTORY_BROKER_TEST_MODE")=="1");contract=authority.class_contract(entry["name"])
  if sorted(contract["capabilities"])!=req["capabilities"]:raise BrokerError("authority/class capability mismatch")
  work=Path(tempfile.mkdtemp(prefix="request-",dir=entry["workspace_root"]));work.chmod(0o700);source=work/"source";source.mkdir();extract(raw,source)
  git=executable(entry,"git")
  env={"HOME":str(work),"PATH":"/usr/bin:/bin","GIT_CONFIG_NOSYSTEM":"1","GIT_CONFIG_GLOBAL":"/dev/null"}
  bounded([git,"init","-q"],env,source,120);bounded([git,"add","-f","--all"],env,source,120)
  tree=bounded([git,"write-tree"],env,source,120)[0].decode().strip();co=base64.b64decode(req["commit_object_b64"],validate=True);hashcmd=[git,"hash-object","-t","commit","-w","--stdin"];commit=subprocess.run(hashcmd,cwd=source,env=env,input=co,capture_output=True,check=True,pass_fds=_pass_fds(hashcmd)).stdout.decode().strip()
  if tree!=req["tree"] or commit!=req["commit"]:raise BrokerError("archive does not reconstruct exact Git tree/commit")
  freeze(source);started=int(time.time());allout=b"";allerr=b"";descriptors=[];payload=[];held=[];cleanup=[];observations=[]
  for index,(name,desc) in enumerate([("gate",contract["gate"]),*sorted(contract["capabilities"].items())]):
   runroot=work/f"run-{index}";runroot.mkdir(mode=0o700);resource={"devices":[],"dbus":None,"collectors":[],"dedicated_host":False} if name=="gate" else entry["resources"][name]
   if resource["dedicated_host"] is not True and (resource["devices"] or resource["dbus"] or resource["collectors"]):raise BrokerError("privileged resources require dedicated_host=true")
   out,err,output,unit,pool=run_operation(entry,authority,desc,name,source,runroot,resource,uid);allout+=out;allerr+=err
   if name!="gate":
    d,p=collect(output,[name],{name:desc["artifacts"]},expected_uid=uid);h=hold(d,p,work);held.append(h);semantic=authority.analyzer(desc["semantic_id"]);cmd=argv(authority,semantic["argv"],Path("/noncandidate"),h.root,req["commit"],req["tree"]);bounded(cmd,{"PATH":":".join(authority.document["trusted_path"]),"LANG":"C.UTF-8"},Path("/"),180);descriptors+=d;payload+=h.payload
   for collector in resource["collectors"]:
    pin_name=next((n for n,p in entry["executable_pins"].items() if p["path"]==collector["argv"][0]),None)
    if pin_name is None:raise BrokerError("host collector executable is not enrolled")
    collector_argv=[executable(entry,pin_name),*collector["argv"][1:]]
    outc,_=bounded(collector_argv,{"PATH":"/usr/bin:/bin","LANG":"C.UTF-8"},Path("/"),30);observations.append({"capability":name,"collector":collector["id"],"sha256":hashlib.sha256(outc).hexdigest()})
   pool.close();pool=None
   cleanup.append({"capability":name,"unit":unit,"clean":True});shutil.rmtree(runroot)
  descriptors.sort(key=lambda x:x["path"]);payload.sort(key=lambda x:x["path"]);total,dd=validate_descriptors(descriptors,req["capabilities"]);scope=hashlib.sha256(json.dumps({"campaign_id":req["campaign_id"],"readiness_nonce":req["readiness_nonce"],"runner":entry["name"],"commit":req["commit"],"nonce":req["nonce"],"artifact_manifest_sha256":dd},sort_keys=True,separators=(",",":")).encode()).hexdigest();pins={n:{k:p[k] for k in ("path","sha256","device","inode")} for n,p in sorted(entry["executable_pins"].items())};host={"executable_pins":pins,"resource_policy_sha256":hashlib.sha256(json.dumps(entry["resources"],sort_keys=True,separators=(",",":")).encode()).hexdigest(),"resource_observations":observations,"containment":{"systemd_scope":True,"private_mounts":True,"private_pids":True,"bounded_writable_tmpfs":True,"broker_only_signing":True},"cleanup_states":cleanup};e={"schema":"factory-runner-receipt/v3","host_authority":host,"result":"pass","runner":entry["name"],"commit":req["commit"],"tree":req["tree"],"environment_blob":req["environment_blob"],"archive_sha256":req["archive_sha256"],"campaign_id":req["campaign_id"],"readiness_nonce":req["readiness_nonce"],"nonce":req["nonce"],"authority_sha256":authority.digest,"capabilities":req["capabilities"],"exit_code":0,"timed_out":False,"started_at":started,"finished_at":int(time.time()),"cleanup":True,"stdout_sha256":hashlib.sha256(allout).hexdigest(),"stderr_sha256":hashlib.sha256(allerr).hexdigest(),"artifact_protocol":PROTOCOL,"artifact_limits":{"count":MAX_ARTIFACTS,"file_bytes":MAX_ARTIFACT_FILE,"aggregate_bytes":MAX_ARTIFACT_BYTES},"artifact_count":len(descriptors),"artifact_bytes":total,"artifact_manifest_sha256":dd,"artifact_scope_sha256":scope,"artifacts":descriptors};signed=sign(e,entry);emit({**e,"stdout_b64":base64.b64encode(allout).decode(),"stderr_b64":base64.b64encode(allerr).decode(),"artifact_payload":payload,**{k:signed[k] for k in ("manifest_b64","signature_b64","signer_principal","signer_key_sha256","signature_algorithm","namespace","signature_sha256")}});return 0
 except (BrokerError,PolicyError,AuthorityError,ArtifactError,OSError,ValueError,subprocess.SubprocessError,KeyError) as e:fail(str(e))
 finally:
  if locals().get("pool"):
   try:pool.close()
   except Exception:pass
  for h in locals().get("held",[]):h.close()
  if "authority" in locals():authority.close()
  if "work" in locals():shutil.rmtree(work,ignore_errors=True)
if __name__=="__main__":raise SystemExit(main())
