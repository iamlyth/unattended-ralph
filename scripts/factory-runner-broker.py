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
def bounded(cmd,env,cwd,limit):
 p=subprocess.Popen(cmd,cwd=cwd,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True);out,err=p.communicate(timeout=limit)
 if len(out)>MAX_LOG or len(err)>MAX_LOG:raise BrokerError("output limit exceeded")
 try:os.killpg(p.pid,signal.SIGKILL)
 except ProcessLookupError:pass
 if p.returncode:raise BrokerError("contained operation failed")
 return out,err
def resource_props(res):
 props=["PrivateDevices=yes"]
 if res["devices"]:
  props=["PrivateDevices=no","DevicePolicy=closed",*[f"DeviceAllow={x['path']} {x['access']}" for x in res["devices"]]]
 return props
def run_operation(entry,authority,desc,name,source,runroot,resource):
 home=runroot/"home";build=runroot/"build";output=runroot/"output"
 for p in (home,build,output):p.mkdir(mode=0o700)
 if name!="gate": (output/name).mkdir(mode=0o700)
 env={"HOME":"/run/factory/home","PATH":":".join(authority.document["trusted_path"]),"LANG":"C.UTF-8","LC_ALL":"C.UTF-8","FACTORY_PRODUCT_ROOT":"/run/factory/source","FACTORY_BUILD_ROOT":"/run/factory/build","FACTORY_RUNNER_ARTIFACT_DIR":f"/run/factory/output/{name}"}
 command=argv(authority,desc["argv"],Path("/run/factory/source"),Path("/run/factory/output"));systemd=entry["systemd_run"]
 props=["NoNewPrivileges=yes","CapabilityBoundingSet=","AmbientCapabilities=","ProtectSystem=strict","ProtectHome=yes","PrivateTmp=yes","PrivateMounts=yes","PrivatePIDs=yes","PrivateIPC=yes","ProtectKernelTunables=yes","ProtectKernelModules=yes","ProtectControlGroups=yes","RestrictSUIDSGID=yes","RestrictNamespaces=yes","IPAddressDeny=any","KillMode=control-group","TasksMax=256","MemoryMax=2G","RuntimeMaxSec=1800","TimeoutStopSec=10","LimitFSIZE=50331648",f"BindReadOnlyPaths={source}:/run/factory/source",f"BindReadOnlyPaths={authority.root}:{authority.root}",f"BindPaths={build}:/run/factory/build",f"BindPaths={home}:/run/factory/home",f"BindPaths={output}:/run/factory/output","WorkingDirectory=/run/factory/source",*resource_props(resource)]
 # D-Bus is default-deny. An adopter must enroll an exact proxy executable and
 # calls; the broker never grants the host bus directly.
 if resource["dbus"] is not None:raise BrokerError("D-Bus resource requires an externally installed proxy plugin; direct bus access is forbidden")
 unit=f"factory-runner-{entry['name']}-{os.urandom(8).hex()}.service";cmd=[systemd,"--quiet","--wait","--pipe","--collect","--unit",unit,"--service-type=exec",f"--uid={entry['uid']}",*[f"--property={x}" for x in props],*[f"--setenv={k}={v}" for k,v in env.items()],*command]
 try:return bounded(cmd,{"PATH":"/usr/bin:/bin","LANG":"C.UTF-8"},Path("/"),1900)+(output,unit)
 finally:subprocess.run([entry["systemctl"],"stop",unit],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
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
  git=entry["executable_pins"]["git"]["path"]
  env={"HOME":str(work),"PATH":"/usr/bin:/bin","GIT_CONFIG_NOSYSTEM":"1","GIT_CONFIG_GLOBAL":"/dev/null"}
  bounded([git,"init","-q"],env,source,120);bounded([git,"add","-f","--all"],env,source,120)
  tree=bounded([git,"write-tree"],env,source,120)[0].decode().strip();co=base64.b64decode(req["commit_object_b64"],validate=True);commit=bounded([git,"hash-object","-t","commit","-w","--stdin"],env,source,120)[0] if False else subprocess.run([git,"hash-object","-t","commit","-w","--stdin"],cwd=source,env=env,input=co,capture_output=True,check=True).stdout.decode().strip()
  if tree!=req["tree"] or commit!=req["commit"]:raise BrokerError("archive does not reconstruct exact Git tree/commit")
  freeze(source);started=int(time.time());allout=b"";allerr=b"";descriptors=[];payload=[];held=[];cleanup=[];observations=[]
  for index,(name,desc) in enumerate([("gate",contract["gate"]),*sorted(contract["capabilities"].items())]):
   runroot=work/f"run-{index}";runroot.mkdir(mode=0o700);resource={"devices":[],"dbus":None,"collectors":[],"dedicated_host":False} if name=="gate" else entry["resources"][name]
   if resource["dedicated_host"] is not True and (resource["devices"] or resource["dbus"] or resource["collectors"]):raise BrokerError("privileged resources require dedicated_host=true")
   out,err,output,unit=run_operation(entry,authority,desc,name,source,runroot,resource);allout+=out;allerr+=err
   if name!="gate":
    d,p=collect(output,[name],{name:desc["artifacts"]},expected_uid=uid);h=hold(d,p,work);held.append(h);semantic=authority.analyzer(desc["semantic_id"]);cmd=argv(authority,semantic["argv"],Path("/noncandidate"),h.root,req["commit"],req["tree"]);bounded(cmd,{"PATH":":".join(authority.document["trusted_path"]),"LANG":"C.UTF-8"},Path("/"),180);descriptors+=d;payload+=h.payload
   for collector in resource["collectors"]:
    outc,_=bounded(collector["argv"],{"PATH":"/usr/bin:/bin","LANG":"C.UTF-8"},Path("/"),30);observations.append({"capability":name,"collector":collector["id"],"sha256":hashlib.sha256(outc).hexdigest()})
   cleanup.append({"capability":name,"unit":unit,"clean":True});shutil.rmtree(runroot)
  descriptors.sort(key=lambda x:x["path"]);payload.sort(key=lambda x:x["path"]);total,dd=validate_descriptors(descriptors,req["capabilities"]);scope=hashlib.sha256(json.dumps({"campaign_id":req["campaign_id"],"readiness_nonce":req["readiness_nonce"],"runner":entry["name"],"commit":req["commit"],"nonce":req["nonce"],"artifact_manifest_sha256":dd},sort_keys=True,separators=(",",":")).encode()).hexdigest();pins={n:{k:p[k] for k in ("path","sha256","device","inode")} for n,p in sorted(entry["executable_pins"].items())};host={"executable_pins":pins,"resource_policy_sha256":hashlib.sha256(json.dumps(entry["resources"],sort_keys=True,separators=(",",":")).encode()).hexdigest(),"resource_observations":observations,"containment":{"systemd_scope":True,"private_mounts":True,"private_pids":True,"bounded_writable_tmpfs":True,"broker_only_signing":True},"cleanup_states":cleanup};e={"schema":"factory-runner-receipt/v3","host_authority":host,"result":"pass","runner":entry["name"],"commit":req["commit"],"tree":req["tree"],"environment_blob":req["environment_blob"],"archive_sha256":req["archive_sha256"],"campaign_id":req["campaign_id"],"readiness_nonce":req["readiness_nonce"],"nonce":req["nonce"],"authority_sha256":authority.digest,"capabilities":req["capabilities"],"exit_code":0,"timed_out":False,"started_at":started,"finished_at":int(time.time()),"cleanup":True,"stdout_sha256":hashlib.sha256(allout).hexdigest(),"stderr_sha256":hashlib.sha256(allerr).hexdigest(),"artifact_protocol":PROTOCOL,"artifact_limits":{"count":MAX_ARTIFACTS,"file_bytes":MAX_ARTIFACT_FILE,"aggregate_bytes":MAX_ARTIFACT_BYTES},"artifact_count":len(descriptors),"artifact_bytes":total,"artifact_manifest_sha256":dd,"artifact_scope_sha256":scope,"artifacts":descriptors};signed=sign(e,entry);emit({**e,"stdout_b64":base64.b64encode(allout).decode(),"stderr_b64":base64.b64encode(allerr).decode(),"artifact_payload":payload,**{k:signed[k] for k in ("manifest_b64","signature_b64","signer_principal","signer_key_sha256","signature_algorithm","namespace","signature_sha256")}});return 0
 except (BrokerError,PolicyError,AuthorityError,ArtifactError,OSError,ValueError,subprocess.SubprocessError,KeyError) as e:fail(str(e))
 finally:
  for h in locals().get("held",[]):h.close()
  if "authority" in locals():authority.close()
  if "work" in locals():shutil.rmtree(work,ignore_errors=True)
if __name__=="__main__":raise SystemExit(main())
