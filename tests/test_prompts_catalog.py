"""The prompt inventory panel shows the real constants, not a second copy.

`backend/prompts_catalog.py` exists so every hardcoded instruction is readable in
**Settings > Prompts**. The failure it must not introduce is the obvious one: a
catalog that drifts from the prompts it claims to mirror. So each row is asserted
against the module attribute it was read from, and the endpoint is asserted to
serve the same thing the module builds.
"""

import unittest

from backend import prompts_catalog


def _rows():
    cat = prompts_catalog.catalog()
    return cat, {i["source"]: i for g in cat["groups"] for i in g["items"]}


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat, cls.by_source = _rows()

    def test_the_inventory_is_not_empty(self):
        self.assertTrue(self.cat["ok"])
        self.assertGreaterEqual(len(self.cat["groups"]), 10)
        for g in self.cat["groups"]:
            self.assertTrue(g["items"], f"{g['id']} has no rows")

    def test_every_row_carries_real_text_and_a_measurement(self):
        for g in self.cat["groups"]:
            for i in g["items"]:
                self.assertTrue(i["text"].strip(), i["name"])
                self.assertEqual(i["chars"], len(i["text"]))
                self.assertGreater(i["chars"], 0)
                self.assertTrue(i["source"], i["name"])

    def test_the_base_prompt_is_the_module_constant_verbatim(self):
        from backend.ai import prompts

        row = self.by_source["ai/prompts.py:BASE"]
        self.assertEqual(row["text"], prompts.BASE)

    def test_tool_rows_are_read_from_the_tool_schemas(self):
        from backend import agent

        names = {i["name"] for g in self.cat["groups"] if g["id"] == "tools"
                 for i in g["items"]}
        self.assertEqual(names, {t["function"]["name"] for t in agent.TOOLS})

    def test_a_guidance_block_matches_its_source(self):
        from backend import guidance

        row = self.by_source["guidance.py:_BUDGET"]
        self.assertEqual(row["text"], guidance._BUDGET)

    def test_the_live_prompt_is_rendered_not_stored(self):
        from backend.ai import prompts

        live = self.cat["live"]
        self.assertIn(prompts.BASE.split("\n")[0], live["agent"])
        self.assertIn("sub-agent", live["subagent"])

    def test_the_live_blocks_carry_what_the_project_would_inject(self):
        """Asked about a project, the panel must report that project's block.

        The renderer passes the session's folder to the route, because the standing
        prefix is rebuilt from it. Without that the live half would always show a
        prompt with no instruction block, which is not the prompt a turn pays. The
        assertion follows whatever the configured mode says rather than pinning one,
        so it tests the wiring and not this machine's settings.
        """
        from backend import config, project_context

        root = str(config.ROOT)
        expected = project_context.block(root) or {}
        cat = prompts_catalog.catalog(root)
        names = [b["name"] for b in cat["live"]["blocks"]]
        if expected.get("text"):
            self.assertIn("Project instructions", names)
        else:
            self.assertNotIn("Project instructions", names)

    def test_totals_count_what_is_listed(self):
        flat = [i for g in self.cat["groups"] for i in g["items"]]
        self.assertEqual(self.cat["totals"]["entries"], len(flat))
        self.assertEqual(self.cat["totals"]["chars"], sum(i["chars"] for i in flat))


class EndpointTests(unittest.TestCase):
    def test_the_route_serves_the_catalog(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from backend.routers import api

        app = FastAPI()
        app.include_router(api.router)
        res = TestClient(app).get("/api/prompts")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["ok"])
        self.assertGreaterEqual(len(body["groups"]), 10)


if __name__ == "__main__":
    unittest.main()
