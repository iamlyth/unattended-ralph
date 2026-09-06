#!/usr/bin/env python3
"""Broker-authenticated root signer for factory-runner-receipt/v3.

There is no stdin-only signing oracle: a one-shot random token arrives on a
broker-owned descriptor and its digest must match the request.  The signer
rebuilds canonical bytes after strict policy/class/receipt validation.
"""
from __future__ import annotations
import argparse,base64,hashlib,json,os,re,stat,subprocess,sys
from pathlib import Path
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from factory_runner_policy import PolicyError,class_for_uid,load_policy
SHA1=re.compile(r"^[0-9a-f]{40}$");SHA256=re.compile(r"^[0-9a-f]{64}$");MAX=2*1024*1024
class SignerError(RuntimeError):pass
def die(msg):print("factory-runner-signer: "+msg,file=sys.stderr);raise SystemExit(1)
def protected(path):
 if Path(path).is_symlink():raise SignerError("signing state must not be symlinked")
 i=os.stat(path)
 fixture=os.environ.get("FACTORY_SIGNER_TEST_MODE")=="1";owner=os.getuid() if fixture else 0
 if not stat.S_ISREG(i.st_mode) or i.st_uid!=owner or i.st_nlink!=1 or stat.S_IMODE(i.st_mode)!=0o600:raise SignerError("signing state ownership/mode is unsafe")
def validate(m,e):
 fields={"schema","host_authority","result","runner","commit","tree","environment_blob","archive_sha256","campaign_id","readiness_nonce","nonce","authority_sha256","capabilities","exit_code","timed_out","started_at","finished_at","cleanup","stdout_sha256","stderr_sha256","artifact_protocol","artifact_limits","artifact_count","artifact_bytes","artifact_manifest_sha256","artifact_scope_sha256","artifacts"}
 if not isinstance(m,dict) or set(m)!=fields or m.get("schema")!="factory-runner-receipt/v3" or m.get("result")!="pass":raise SignerError("manifest schema/fields are invalid")
 if m["runner"]!=e["name"] or m["capabilities"]!=sorted(e["allowed_capabilities"]):raise SignerError("manifest class/capabilities differ from root policy")
 for k in ("commit","tree","environment_blob"):
  if not SHA1.fullmatch(str(m[k])):raise SignerError(f"manifest {k} is invalid")
 for k in ("archive_sha256","readiness_nonce","nonce","authority_sha256","stdout_sha256","stderr_sha256","artifact_manifest_sha256","artifact_scope_sha256"):
  if not SHA256.fullmatch(str(m[k])):raise SignerError(f"manifest {k} is invalid")
 if m["authority_sha256"]!=e["probe_authority_sha256"] or m["exit_code"]!=0 or m["timed_out"] is not False or m["cleanup"] is not True:raise SignerError("manifest does not prove exact authority clean pass")
 if m["artifact_protocol"]!="factory-runner-artifacts/v1" or type(m["artifact_count"])is not int or not 0<=m["artifact_count"]<=64 or type(m["artifact_bytes"])is not int or not 0<=m["artifact_bytes"]<=48*1024*1024:raise SignerError("artifact binding is invalid")
 if not isinstance(m["artifacts"],list) or len(m["artifacts"])!=m["artifact_count"]:raise SignerError("artifact count mismatch")
 host=m["host_authority"]
 if not isinstance(host,dict) or set(host)!={"executable_pins","resource_policy_sha256","resource_observations","containment","cleanup_states"}:raise SignerError("host authority fields are invalid")
 expected={n:{k:p[k] for k in ("path","sha256","device","inode")} for n,p in sorted(e["executable_pins"].items())}
 if host["executable_pins"]!=expected or host["resource_policy_sha256"]!=hashlib.sha256(json.dumps(e["resources"],sort_keys=True,separators=(",",":")).encode()).hexdigest():raise SignerError("host authority differs from root enrollment")
 if host["containment"]!={"systemd_scope":True,"private_mounts":True,"private_pids":True,"bounded_writable_tmpfs":True,"broker_only_signing":True}:raise SignerError("containment proof is incomplete")
 if not isinstance(host["cleanup_states"],list) or len(host["cleanup_states"])!=len(e["allowed_capabilities"])+1:raise SignerError("cleanup proof is incomplete")
 return m
def main():
 ap=argparse.ArgumentParser(add_help=False);ap.add_argument("--broker-fd",type=int,required=True);a=ap.parse_args()
 try:
  if not os.geteuid()==0 and os.environ.get("FACTORY_SIGNER_TEST_MODE")!="1":raise SignerError("signer must run as root")
  token=os.read(a.broker_fd,33)
  if len(token)!=32 or os.read(a.broker_fd,1):raise SignerError("broker authentication token is invalid")
  raw=sys.stdin.buffer.read(MAX+1)
  if len(raw)>MAX:raise SignerError("sign request exceeds bound")
  req=json.loads(raw)
  if not isinstance(req,dict) or set(req)!={"schema","broker_auth_sha256","manifest"} or req.get("schema")!="factory-runner-sign-request/v1" or req["broker_auth_sha256"]!=hashlib.sha256(token).hexdigest():raise SignerError("broker authentication failed")
  policy=load_policy();uid=int(os.environ["SUDO_UID"]);entry=class_for_uid(policy,uid);manifest=validate(req["manifest"],entry)
  key=Path(entry["signer_key"]);principal_path=Path(entry["signer_principal_file"]);protected(key);protected(principal_path);principal=principal_path.read_text().strip()
  if principal!=entry["name"]:raise SignerError("signer principal differs from class")
  pub=subprocess.run([entry["executable_pins"]["ssh-keygen"]["path"],"-y","-f",str(key)],capture_output=True,text=True,timeout=30,check=True).stdout.strip().split()
  public=" ".join(pub[:2]);key_digest=hashlib.sha256(public.encode()).hexdigest()
  signed={**manifest,"signer_principal":principal,"signer_key_sha256":key_digest,"namespace":policy["namespace"],"signature_algorithm":"ssh-ed25519"};canonical=(json.dumps(signed,sort_keys=True,indent=2)+"\n").encode()
  proc=subprocess.run([entry["executable_pins"]["ssh-keygen"]["path"],"-Y","sign","-f",str(key),"-n",policy["namespace"]],input=canonical,capture_output=True,timeout=120,check=True);sig=proc.stdout
  out={"schema":"factory-runner-sign-response/v1","result":"signed","manifest_b64":base64.b64encode(canonical).decode(),"signature_b64":base64.b64encode(sig).decode(),"signer_principal":principal,"signer_key_sha256":key_digest,"signature_algorithm":"ssh-ed25519","namespace":policy["namespace"],"signature_sha256":hashlib.sha256(sig).hexdigest()}
  print(json.dumps(out,sort_keys=True,separators=(",",":")));return 0
 except (SignerError,PolicyError,OSError,ValueError,subprocess.SubprocessError) as e:die(str(e))
if __name__=="__main__":raise SystemExit(main())
