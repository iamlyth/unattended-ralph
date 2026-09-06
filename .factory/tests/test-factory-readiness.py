#!/usr/bin/env python3
from __future__ import annotations
import copy, hashlib, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/".factory/loop"))
import readiness
import campaign

class ReadinessPolicyTests(unittest.TestCase):
    def policy(self):
        return {
          "schema":"factory-readiness-policy/v1","status":"active",
          "production_authority":{"enrolled":True,"reason":"fixture authority"},
          "required_runner_classes":[{"id":"class-a","capabilities":["cap-a"]},{"id":"class-b","capabilities":["cap-b","cap-c"]}],
          "required_capabilities":["cap-a","cap-c"],
          "conformance_gate_ids":["conformance-planning"],"core_gate_ids":["boilerplate-verification"],
          "human_approval":None,
          "invalidation":{"accepted_commit":["runner-aggregate","capability-evidence","conformance-planning"],"current_product":["boilerplate-verification"],"paths":{"boilerplate-verification":["src"]}}
        }

    def test_neutral_policy_is_explicitly_unenrolled(self):
        policy,_=readiness.load_policy(ROOT)
        self.assertFalse(policy["production_authority"]["enrolled"])
        self.assertEqual(policy["required_runner_classes"],[])

    def test_unknown_gate_adapter_fails_closed(self):
        value=self.policy(); value["core_gate_ids"]=["operator-command"]
        with self.assertRaises(readiness.ReadinessError): readiness.validate_policy(value)
        with self.assertRaises(readiness.ReadinessError): readiness.gate_argv("operator-command")

    def test_aggregate_is_class_order_independent_and_exact(self):
        policy=readiness.validate_policy(self.policy()); commit="a"*40; tree="b"*40; env="c"*40
        def record(name,caps): return {"name":name,"manifest":f".factory-state/runner-evidence/{name}/manifest.json","manifest_sha256":"d"*64,"capabilities":caps,"artifact_manifest_sha256":"e"*64,"artifact_count":0,"artifact_bytes":0,"signer":{"principal":name,"key_sha256":"f"*64,"algorithm":"ssh-ed25519","signature_sha256":"1"*64}}
        records=[record("class-b",["cap-c","cap-b"]),record("class-a",["cap-a"])]
        def aggregate(items): return {"schema":"factory-runner-aggregate/v4","campaign_id":"fixture","readiness_nonce":"9"*64,"commit":commit,"tree":tree,"environment_blob":env,"runners":items}
        kwargs={"accepted_commit":commit,"tree":tree,"environment_blob":env,"campaign_id":"fixture","readiness_nonce":"9"*64}
        first=readiness.validate_aggregate(aggregate(records),policy,**kwargs)
        second=readiness.validate_aggregate(aggregate(list(reversed(records))),policy,**kwargs)
        self.assertEqual(first,second)
        for mutation in (records[:1], records+[record("class-c",[])]):
            with self.assertRaises(readiness.ReadinessFindings): readiness.validate_aggregate(aggregate(mutation),policy,**kwargs)

    def test_generic_fixture_policy_passes_all_fixed_adapters(self):
        policy=readiness.validate_policy(self.policy())
        gates={gate:{"ran":True,"exit":0,"digest":hashlib.sha256(gate.encode()).hexdigest()} for gate in policy["conformance_gate_ids"]+policy["core_gate_ids"]}
        status,results=readiness.evaluate(policy,aggregate_sha256="a"*64,gate_results=gates,human_sha256="b"*64)
        self.assertEqual(status,"complete")
        self.assertEqual(set(results),{"aggregate","human","conformance-planning","boilerplate-verification"})
        gates["boilerplate-verification"]["exit"]=1
        self.assertEqual(readiness.evaluate(policy,aggregate_sha256="a"*64,gate_results=gates,human_sha256="b"*64)[0],"findings")

    def test_result_rejects_forged_cache_cross_campaign_and_zero_digest(self):
        bindings=readiness.readiness_bindings(accepted_commit="a"*40,accepted_tree="b"*40,current_commit="a"*40,current_tree="b"*40,config_sha256="1"*64,environment_sha256="2"*64,specification_sha256="3"*64,plan_sha256="4"*64,contracts_sha256="5"*64,policy_sha256="6"*64,trust_sha256="7"*64,install_manifest_sha256="8"*64)
        value=readiness.result_document(campaign_id="fixture",nonce="9"*64,status="complete",bindings=bindings,results={"aggregate":"a"*64,"core":"b"*64})
        readiness.validate_result(value,campaign_id="fixture",nonce="9"*64,bindings=bindings)
        for key,changed in (("campaign_id","other"),("nonce","8"*64)):
            forged=copy.deepcopy(value); forged[key]=changed
            with self.assertRaises(readiness.ReadinessError): readiness.validate_result(forged,campaign_id="fixture",nonce="9"*64,bindings=bindings)
        with self.assertRaises(readiness.ReadinessError): readiness.result_document(campaign_id="fixture",nonce="9"*64,status="complete",bindings=bindings,results={"aggregate":"0"*64})

    def test_missing_required_human_authority_blocks(self):
        policy=self.policy(); policy["human_approval"]={"required":True,"approval_schema":"project-approval-v1","approval_path":".factory/approval.json","signature_path":".factory/approval.sig","signature_namespace":"project-approval","trust_scope":"project-review","checklist":["reviewed"],"captures":[]}
        policy=readiness.validate_policy(policy)
        with self.assertRaises(readiness.HumanAuthorityBlocked): readiness.validate_human_authority(policy,None,None,accepted_commit="a"*40,accepted_tree="b"*40,blob_at=lambda c,p:b"")

    def test_public_import_cannot_mint_authorization_store(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"state/campaign").mkdir(parents=True,mode=0o700)
            fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
            try:
                with self.assertRaises(readiness.AuthorizationError):
                    readiness.AuthorizationStore(root,"state/campaign","fixture","a"*64,fd,object())
            finally: os.close(fd)

    def test_campaign_ids_and_nonces_are_canonical_lowercase(self):
        self.assertIsNone(readiness.IDENT.fullmatch("Upper"))
        self.assertIsNotNone(readiness.IDENT.fullmatch("lower-1"))
        self.assertIsNone(readiness.SHA256.fullmatch("A"*64))

    def test_neutral_campaign_blocks_before_external_execution(self):
        head=subprocess.check_output(["git","-C",str(ROOT),"rev-parse","HEAD"],text=True).strip()
        command=[sys.executable,str(ROOT/".factory/loop/campaign.py"),"--root",str(ROOT),"run","--campaign-id","neutral-check","--rounds","1","--branch","boilerplate-develop","--provider","ollama","--model","unused","--backend","/bin/false","--accepted-commit",head,"--install-manifest","/nonexistent","--campaign-timeout","1","--verification-command","./.factory/tools/verify-boilerplate.sh","--acceptance-command","./.factory/tools/verify-boilerplate.sh"]
        result=subprocess.run(command,text=True,capture_output=True)
        self.assertEqual(result.returncode,campaign.EXIT_BLOCKED,result.stderr)
        value=json.loads(result.stdout)
        self.assertEqual((value["terminal_phase"],value["terminal_outcome"]),("blocked","blocked"))
        self.assertEqual(value["phase_history"],[])

    def test_readiness_only_is_not_success(self):
        ready=campaign.CampaignResult("ready",1,0,"readiness_complete","readiness_complete","a"*40,())
        ready.validate()
        with self.assertRaises(campaign.CampaignResultError):
            campaign.CampaignResult("fake",1,0,"success","pass","a"*40,()).validate()

if __name__=="__main__": unittest.main(verbosity=2)
