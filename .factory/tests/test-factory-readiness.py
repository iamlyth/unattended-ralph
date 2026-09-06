#!/usr/bin/env python3
from __future__ import annotations
import copy, hashlib, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/".factory/loop"))
import readiness
import campaign
import sidecars


class ReadinessPolicyTests(unittest.TestCase):
    def policy(self):
        return {
          "schema":"factory-readiness-policy/v1","status":"active",
          "production_authority":{"enrolled":True,"reason":"fixture authority"},
          "required_runner_classes":[{"id":"class-a","capabilities":["cap-a"]},{"id":"class-b","capabilities":["cap-b","cap-c"]}],
          "required_capabilities":["cap-a","cap-c"],
          "conformance_gate_ids":["conformance-planning","conformance-implementation"],"core_gate_ids":["boilerplate-verification"],
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

    def test_aggregate_requires_name_and_ignores_class_result_fallback(self):
        policy=readiness.validate_policy(self.policy()); commit="a"*40; tree="b"*40; env="c"*40
        def record(name,caps,**extra):
            base={"name":name,"manifest":f".factory-state/runner-evidence/{name}/manifest.json","manifest_sha256":"d"*64,"capabilities":caps,"artifact_manifest_sha256":"e"*64,"artifact_count":0,"artifact_bytes":0,"signer":{"principal":name,"key_sha256":"f"*64,"algorithm":"ssh-ed25519","signature_sha256":"1"*64}}
            base.update(extra); return base
        def aggregate(items): return {"schema":"factory-runner-aggregate/v4","campaign_id":"fixture","readiness_nonce":"9"*64,"commit":commit,"tree":tree,"environment_blob":env,"runners":items}
        kwargs={"accepted_commit":commit,"tree":tree,"environment_blob":env,"campaign_id":"fixture","readiness_nonce":"9"*64}
        # ``class`` is never a substitute for the exact v4 ``name`` field.
        class_only=aggregate([record("class-a",["cap-a"]),record("class-b",["cap-b","cap-c"])])
        for item in class_only["runners"]:
            item["class"]=item.pop("name")
        with self.assertRaises(readiness.ReadinessFindings): readiness.validate_aggregate(class_only,policy,**kwargs)
        # ``result`` is not a pass signal: presence in the aggregate is the
        # only authority, so a ``result: fail`` record is still accepted.
        with_result=aggregate([record("class-a",["cap-a"],result="fail"),record("class-b",["cap-b","cap-c"],result="fail")])
        self.assertRegex(readiness.validate_aggregate(with_result,policy,**kwargs),r"^[0-9a-f]{64}$")
        # A record that omits ``name`` entirely fails closed.
        no_name=aggregate([record("class-a",["cap-a"]),record("class-b",["cap-b","cap-c"])])
        no_name["runners"][0].pop("name")
        with self.assertRaises(readiness.ReadinessFindings): readiness.validate_aggregate(no_name,policy,**kwargs)

    def test_generic_fixture_policy_passes_all_fixed_adapters(self):
        policy=readiness.validate_policy(self.policy())
        gates={gate:{"ran":True,"exit":0,"digest":hashlib.sha256(gate.encode()).hexdigest()} for gate in policy["conformance_gate_ids"]+policy["core_gate_ids"]}
        status,results=readiness.evaluate(policy,aggregate_sha256="a"*64,gate_results=gates,human_sha256="b"*64)
        self.assertEqual(status,"complete")
        self.assertEqual(set(results),{"aggregate","human","conformance-planning","conformance-implementation","boilerplate-verification"})
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
        policy=self.policy(); policy["human_approval"]={"required":True,"approval_schema":"project-approval-v1","approval_path":".factory/approval.json","signature_path":".factory/approval.sig","signature_namespace":"project-approval","trust_scope":"project-review","trust_path":"/etc/factory/human-trust.json","checklist":["reviewed"],"captures":[]}
        policy=readiness.validate_policy(policy)
        with self.assertRaises(readiness.HumanAuthorityBlocked): readiness.validate_human_authority(policy,None,None,accepted_commit="a"*40,accepted_tree="b"*40,blob_at=lambda c,p:b"")

    def test_human_approval_requires_detached_signature_and_current_trust(self):
        with tempfile.TemporaryDirectory() as td:
            directory=Path(td); key=directory/"id"; approval_path=directory/"approval.json"
            subprocess.run(["ssh-keygen","-q","-t","ed25519","-N","","-f",str(key)],check=True)
            issued=1700000000
            approval={"schema":"project-approval-v1","status":"approved","commit":"a"*40,"tree":"b"*40,"checklist":["reviewed"],"captures":[],"reviewer":"reviewer-1","issued_at":issued}
            raw=readiness.canonical_bytes(approval);approval_path.write_bytes(raw)
            subprocess.run(["ssh-keygen","-Y","sign","-f",str(key),"-n","project-approval",str(approval_path)],check=True,stdout=subprocess.DEVNULL)
            signature=(directory/"approval.json.sig").read_bytes();public=(directory/"id.pub").read_text().strip()
            trust=readiness.canonical_bytes({"schema":"factory-human-trust/v2","status":"active","scope":"project-review","namespace":"project-approval","keys":[{"principal":"reviewer-1","public_key":public,"issued_at":issued-1,"revoked_at":None}]})
            policy=self.policy();policy["human_approval"]={"required":True,"approval_schema":"project-approval-v1","approval_path":".factory/approval.json","signature_path":".factory/approval.sig","signature_namespace":"project-approval","trust_scope":"project-review","trust_path":"/etc/factory/human-trust.json","checklist":["reviewed"],"captures":[]};policy=readiness.validate_policy(policy)
            value=readiness.validate_human_authority(policy,raw,trust,signature_raw=signature,accepted_commit="a"*40,accepted_tree="b"*40,blob_at=lambda c,p:b"",now=issued+1)
            self.assertRegex(value,r"^[0-9a-f]{64}$")
            revoked=json.loads(trust);revoked["keys"][0]["revoked_at"]=issued+1
            with self.assertRaises(readiness.HumanAuthorityBlocked):readiness.validate_human_authority(policy,raw,readiness.canonical_bytes(revoked),signature_raw=signature,accepted_commit="a"*40,accepted_tree="b"*40,blob_at=lambda c,p:b"",now=issued+2)
            with self.assertRaises(readiness.HumanAuthorityBlocked):readiness.validate_human_authority(policy,raw+b" ",trust,signature_raw=signature,accepted_commit="a"*40,accepted_tree="b"*40,blob_at=lambda c,p:b"",now=issued+1)

    def test_public_import_cannot_mint_authorization_store(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"state/campaign").mkdir(parents=True,mode=0o700)
            fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
            try:
                with self.assertRaises(readiness.AuthorizationError):
                    readiness.AuthorizationStore(root,"state/campaign","fixture","a"*64,fd,{})
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

    def test_readiness_sidecar_roundtrip_and_binding_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            base=sidecars.empty_readiness(required=True)
            base.update({"nonce":"9"*64,"accepted_commit":"a"*40,"tree":"b"*40,"environment_blob":"c"*40,"specification_sha256":"1"*64,"plan_sha256":"2"*64,"policy_sha256":"3"*64,"contracts_sha256":"4"*64,"install_manifest_sha256":"5"*64})
            state=sidecars.ReadinessState(schema=sidecars.READINESS_SCHEMA,campaign_id="fixture",readiness=base)
            sidecars.write_readiness(root,state)
            loaded=sidecars.read_readiness(root,expected_campaign_id="fixture")
            self.assertEqual(loaded.readiness["nonce"],"9"*64)
            self.assertEqual(loaded.readiness["status"],"pending")
            # Advancing cursor/status is allowed.
            advanced=dict(loaded.readiness); advanced.update({"cursor":1,"status":"acquiring"})
            updated=sidecars.update_readiness(loaded,advanced)
            self.assertEqual(updated.readiness["cursor"],1)
            # A binding field may never change.
            forged=dict(advanced); forged["nonce"]="8"*64
            with self.assertRaises(sidecars.SidecarBindingError): sidecars.update_readiness(updated,forged)
            # A rewound cursor fails closed.
            rewound=dict(advanced); rewound["cursor"]=0
            with self.assertRaises(sidecars.SidecarTransitionError): sidecars.update_readiness(updated,rewound)
            # A cross-campaign read fails closed.
            with self.assertRaises(sidecars.SidecarBindingError): sidecars.read_readiness(root,expected_campaign_id="other")

    def test_readiness_sidecar_complete_requires_all_result_digests(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            base=sidecars.empty_readiness(required=True)
            base.update({"nonce":"9"*64,"accepted_commit":"a"*40,"tree":"b"*40,"environment_blob":"c"*40,"specification_sha256":"1"*64,"plan_sha256":"2"*64,"policy_sha256":"3"*64,"contracts_sha256":"4"*64,"install_manifest_sha256":"5"*64})
            state=sidecars.ReadinessState(schema=sidecars.READINESS_SCHEMA,campaign_id="fixture",readiness=base)
            sidecars.write_readiness(root,state)
            loaded=sidecars.read_readiness(root,expected_campaign_id="fixture")
            complete=dict(loaded.readiness)
            complete.update({"status":"complete","cursor":6,"terminal_outcome":"pass","aggregate_sha256":"a"*64,"capability_result_sha256":"b"*64,"core_result_sha256":"c"*64,"conformance_result_sha256":"d"*64,"human_result_sha256":"e"*64,"result_sha256":"f"*64})
            done=sidecars.update_readiness(loaded,complete)
            sidecars.write_readiness(root,done)
            self.assertEqual(sidecars.read_readiness(root,expected_campaign_id="fixture").readiness["status"],"complete")
            # A complete sidecar missing a result digest is rejected on write.
            partial=dict(complete); partial["result_sha256"]="0"*64
            with self.assertRaises(sidecars.SidecarError): sidecars.write_readiness(root,sidecars.ReadinessState(schema=sidecars.READINESS_SCHEMA,campaign_id="fixture",readiness=partial))

if __name__=="__main__": unittest.main(verbosity=2)
