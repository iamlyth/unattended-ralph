#!/usr/bin/env python3
"""Rootless adversarial fixtures for the generic runner trust protocols.

These tests exercise protocol mechanics only and never produce live evidence.
"""
from __future__ import annotations
import copy,hashlib,importlib.util,json,os,pathlib,shutil,stat,subprocess,sys,tempfile,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
def module(name,path):
 spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
sys.path.insert(0,str(ROOT/"scripts"))
import factory_runner_policy as policy
import factory_runner_authority as authority
import factory_runner_artifacts as artifacts

class RunnerFrameworkTests(unittest.TestCase):
 def pin(self,path="/usr/bin/true"):return {"path":path,"sha256":"a"*64,"device":1,"inode":2,"status":"enrolled"}
 def klass(self,n,uid):
  cap=f"cap-{n}"
  return {"name":f"class-{n}","uid":uid,"account":f"account-{n}","workspace_root":f"/srv/factory-runner/{n}","allowed_capabilities":[cap],"broker_helper":"/usr/local/libexec/factory-runner-broker","probe_authority":f"/opt/factory-runner/authority/{n}","probe_authority_sha256":"b"*64,"probe_authority_status":"enrolled","signer_key":f"/etc/factory-runner/{n}.key","signer_principal_file":f"/etc/factory-runner/{n}.principal","nonce_ledger":f"/var/lib/factory-runner/{n}/nonces","systemd_run":"/usr/bin/systemd-run","systemctl":"/usr/bin/systemctl","cgroup_root":"/sys/fs/cgroup","approved_groups":[f"account-{n}"],"executable_pins":{"git":self.pin(),"ssh-keygen":self.pin("/usr/bin/ssh-keygen"),"systemd-run":self.pin("/usr/bin/systemd-run"),"systemctl":self.pin("/usr/bin/systemctl"),"mount":self.pin("/usr/bin/mount"),"umount":self.pin("/usr/bin/umount")},"resources":{cap:{"devices":[],"dbus":None,"collectors":[],"dedicated_host":False}}}
 def test_arbitrary_class_counts_and_exact_resource_coverage(self):
  for count in (1,2,7,64):policy.validate_policy({"schema":policy.POLICY_SCHEMA,"namespace":"factory-runner-receipt","classes":[self.klass(str(i),1000+i) for i in range(count)]})
  bad={"schema":policy.POLICY_SCHEMA,"namespace":"factory-runner-receipt","classes":[self.klass("x",1001)]};bad["classes"][0]["resources"]={}
  with self.assertRaises(policy.PolicyError):policy.validate_policy(bad)
 def test_unknown_resource_and_wildcard_dbus_fail(self):
  value={"schema":policy.POLICY_SCHEMA,"namespace":"factory-runner-receipt","classes":[self.klass("x",1001)]};cap="cap-x";value["classes"][0]["resources"][cap]["unknown"]=[]
  with self.assertRaises(policy.PolicyError):policy.validate_policy(value)
  value={"schema":policy.POLICY_SCHEMA,"namespace":"factory-runner-receipt","classes":[self.klass("x",1001)]};value["classes"][0]["resources"][cap]["dbus"]={"bus":"system","address":"unix:path=/run/bus","destination":"org.example.Service","calls":["*"]}
  with self.assertRaises(policy.PolicyError):policy.validate_policy(value)
 def test_fixture_authority_is_harmless_and_unknown_analyzer_fails(self):
  root=ROOT/".factory/tests/fixtures/runner-authority";digest=hashlib.sha256((root/"authority.json").read_bytes()).hexdigest();bundle=authority.load_authority(root,digest,fixture=True)
  try:
   self.assertEqual(bundle.class_contract("fixture-class")["capabilities"]["fixture-capability"]["semantic_id"],"fixture-json-v1")
   with self.assertRaises(authority.AuthorityError):bundle.analyzer("unknown")
  finally:bundle.close()
 def test_candidate_probe_replacement_is_detected(self):
  source=ROOT/".factory/tests/fixtures/runner-authority"
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td)/"a";shutil.copytree(source,root);digest=hashlib.sha256((root/"authority.json").read_bytes()).hexdigest();bundle=authority.load_authority(root,digest,fixture=True)
   try:
    replacement=root/"replacement";replacement.write_bytes((root/"probe.py").read_bytes());os.replace(replacement,root/"probe.py")
    with self.assertRaises(authority.AuthorityError):bundle.path("probe.py")
   finally:bundle.close()
 def test_artifact_traversal_symlink_sparse_and_limits(self):
  for value in ("../x","/x","A/x","x//y"):
   with self.assertRaises(artifacts.ArtifactError):artifacts.canonical_path(value)
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td);root.chmod(0o700);cap=root/"cap";cap.mkdir(mode=0o700);(cap/"result.json").symlink_to("/etc/passwd")
   with self.assertRaises(artifacts.ArtifactError):artifacts.collect(root,["cap"],{"cap":{"files":{"result.json":"application/json"},"required":["result.json"]}})
 def test_signer_has_no_direct_oracle(self):
  result=subprocess.run([sys.executable,str(ROOT/"scripts/factory-runner-signer.py"),"--broker-fd","-1"],input=b'{}\n',capture_output=True)
  self.assertNotEqual(result.returncode,0);self.assertNotIn(b"BEGIN SSH SIGNATURE",result.stdout)
 def test_forced_command_and_bootstrap_are_exact(self):
  server=(ROOT/"scripts/factory-runner-server.py").read_text();installer=(ROOT/"scripts/install-factory-runner-v2.sh").read_text();bootstrap=(ROOT/"scripts/factory-runner-root-bootstrap").read_text()
  self.assertIn('factory-runner-v2',server);self.assertIn('restrict,command=',installer);self.assertIn('ssh-keygen -Y verify',bootstrap);self.assertNotIn('--no-verify',bootstrap)
 def test_install_manifest_rejects_dirty_and_symlink_tree(self):
  with tempfile.TemporaryDirectory() as td:
   repo=pathlib.Path(td);subprocess.run(["git","init","-q",repo],check=True);subprocess.run(["git","-C",repo,"config","user.email","fixture@example.invalid"]);subprocess.run(["git","-C",repo,"config","user.name","fixture"]);(repo/"x").write_text("x");subprocess.run(["git","-C",repo,"add","x"],check=True);subprocess.run(["git","-C",repo,"commit","-qm","base"],check=True);(repo/"dirty").write_text("x")
   r=subprocess.run([sys.executable,str(ROOT/"scripts/generate-runner-install-manifest.py"),"--source",str(repo),"--output",str(repo/"m.json")],capture_output=True);self.assertNotEqual(r.returncode,0)
 def test_archive_adapter_namespaces_runner_evidence(self):
  text=(ROOT/"scripts/archive-factory-campaign.py").read_text();self.assertIn('runner-evidence/',text);self.assertIn('PurePosixPath',text)
  archive=module("fixture_archive",ROOT/"scripts/archive-factory-campaign.py")
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td);(root/"safe").write_text("x");(root/"link").symlink_to("safe")
   with self.assertRaises(SystemExit):archive.inspect(root,os.getuid())
 def test_bounds_and_nonce_controls_are_present(self):
  broker=(ROOT/"scripts/factory-runner-broker.py").read_text()
  for token in ("NONCE_TTL","NONCE_OUTSTANDING","MAX_ARCHIVE","MAX_FILES","PrivatePIDs=yes","TasksMax=256","MemoryMax=2G","RuntimeMaxSec=1800","broker_auth_sha256"):self.assertIn(token,broker)
 def test_fork_setsid_output_disk_and_nonce_flood_controls(self):
  text=(ROOT/"scripts/factory-runner-broker.py").read_text()
  for token in ("KillMode=control-group","PrivatePIDs=yes","TasksMax=256","RLIMIT_FSIZE","RLIMIT_NPROC","nr_inodes=65536","size=768M","NONCE_OUTSTANDING=32"):self.assertIn(token,text)
  broker=module("fixture_broker",ROOT/"scripts/factory-runner-broker.py")
  with self.assertRaises(broker.BrokerError):broker.bounded([sys.executable,"-c","import sys;sys.stdout.write('x'*(5*1024*1024))"],{"PATH":"/usr/bin:/bin"},pathlib.Path("/"),10)
 def test_pin_substitution_and_publication_race_controls(self):
  broker=(ROOT/"scripts/factory-runner-broker.py").read_text();client=(ROOT/"scripts/run-factory-runners.py").read_text()
  self.assertIn("executable pin substitution",broker);self.assertIn("os.O_NOFOLLOW",broker)
  self.assertIn("renameat2",client);self.assertIn("publication collision",client);self.assertIn("hold_tree(staging)",client)
 def test_authorized_keys_install_is_nofollow_and_configurable(self):
  text=(ROOT/"scripts/install-factory-runner-v2.sh").read_text()
  self.assertIn("Repeat",(ROOT/"docs/OPERATIONS.md").read_text());self.assertIn("os.O_NOFOLLOW",text);self.assertIn("set(keys)!=accounts",text);self.assertIn("transaction.json",text);self.assertIn("current-rollback",text)

if __name__=="__main__":unittest.main(verbosity=2)
