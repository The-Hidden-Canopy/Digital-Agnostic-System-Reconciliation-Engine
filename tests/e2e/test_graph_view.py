from tests.helpers import TUTORIAL, WorkspaceCase


class WorkspaceGraph(WorkspaceCase):
    def test_empty_workspace_has_an_empty_graph(self):
        g = self.svc.workspace_graph()
        self.assertEqual((g["nodes"], g["edges"], g["truncated"]), ([], [], False))

    def test_sources_claims_and_runs_are_linked_and_replacement_makes_the_run_stale(self):
        t = self.tutorial()
        claim = self.approve(t["spec"]["id"], "limits.retries")
        started = self.svc.start_run([claim], [t["cfg"]["id"]])
        self.assertEqual(self.svc.wait_job(started["job_id"])["state"], "SUCCEEDED")
        run_id = started["run_id"]
        g = self.svc.workspace_graph()
        kinds = {n["kind"] for n in g["nodes"]}
        self.assertEqual(kinds, {"source", "claim", "run"})
        edges = {(e["from"], e["to"], e["relation"]) for e in g["edges"]}
        self.assertIn((t["spec"]["id"], claim, "states"), edges)                 # the specification states the claim
        self.assertIn((claim, run_id, "tested"), edges)
        self.assertIn((t["cfg"]["id"], run_id, "evidence"), edges)
        run = next(n for n in g["nodes"] if n["id"] == run_id)
        self.assertEqual(run["state"], "CURRENT")
        changed = (TUTORIAL / "service-config.json").read_bytes().replace(b"5", b"6", 1)
        newer = self.imp(changed, "service-config.json", replaces=t["cfg"]["id"])
        g = self.svc.workspace_graph()
        by = {n["id"]: n for n in g["nodes"]}
        self.assertEqual(by[t["cfg"]["id"]]["state"], "SUPERSEDED")
        self.assertIn((t["cfg"]["id"], newer["id"], "replaced_by"), {(e["from"], e["to"], e["relation"]) for e in g["edges"]})
        self.assertEqual(by[run_id]["state"], "STALE")
        self.assertIn("evidence_superseded", by[run_id]["causes"])

    def test_truncation_never_leaves_dangling_edges(self):
        t = self.tutorial()
        claim = self.approve(t["spec"]["id"], "limits.retries")
        self.svc.start_run([claim], [t["cfg"]["id"]])
        g = self.svc.workspace_graph(limit=1)
        ids = {n["id"] for n in g["nodes"]}
        self.assertTrue(g["truncated"])
        self.assertTrue(all(e["from"] in ids and e["to"] in ids for e in g["edges"]))
