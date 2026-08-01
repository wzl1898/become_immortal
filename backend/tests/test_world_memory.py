import os
import tempfile
import unittest

import store
import game


class WorldMemoryStoreTests(unittest.TestCase):
    """store 层：状态型 upsert 覆盖、事件型追加、实体表落盘。"""

    def setUp(self):
        # 用临时库，别碰真实 saves.db
        self._tmp = tempfile.mkdtemp()
        self._orig_data, self._orig_db = store.DATA_DIR, store.DB_PATH
        store.DATA_DIR = self._tmp
        store.DB_PATH = os.path.join(self._tmp, "test_saves.db")
        store.init()
        self.sid = store.create("测试局", [{"role": "system", "content": "x"}])

    def tearDown(self):
        store.DATA_DIR, store.DB_PATH = self._orig_data, self._orig_db

    def _state(self, cid, text, turn, imp=0.5):
        return {"id": f"m{turn}", "scope": "state", "type": "item",
                "text": text, "canonical_id": cid, "turn": turn, "importance": imp}

    def _event(self, text, turn):
        return {"id": f"e{turn}", "scope": "event", "type": "plot",
                "text": text, "turn": turn, "importance": 0.5}

    def test_state_upsert_keeps_single_latest(self):
        store.upsert_world_memory(self.sid, [self._state("ent_token", "露出半截，渗白雾", 5)])
        store.upsert_world_memory(self.sid, [self._state("ent_token", "靠近破庙发凛", 11)])
        store.upsert_world_memory(self.sid, [self._state("ent_token", "触碰致昏厥", 60)])
        wm = store.load(self.sid)["world_memory"]
        token = [m for m in wm if m.get("canonical_id") == "ent_token"]
        self.assertEqual(len(token), 1)
        self.assertEqual(token[0]["text"], "触碰致昏厥")
        self.assertEqual(token[0]["turn"], 60)

    def test_state_different_entities_coexist(self):
        # 玄色小牌 vs 玉牌：不同 canonical_id，各留一条，互不覆盖（防 over-merge）
        store.upsert_world_memory(self.sid, [self._state("ent_token", "主角私藏的黑牌", 11)])
        store.upsert_world_memory(self.sid, [self._state("ent_jade", "玄霄宗持有的玉牌", 40)])
        wm = store.load(self.sid)["world_memory"]
        states = [m for m in wm if m.get("scope") == "state"]
        self.assertEqual(len(states), 2)

    def test_events_all_appended_with_turn(self):
        store.upsert_world_memory(self.sid, [self._event("玩家偷拿小牌", 6)])
        store.upsert_world_memory(self.sid, [self._event("宗门放话查验", 44)])
        store.upsert_world_memory(self.sid, [self._event("查验转向邻家", 47)])
        events = [m for m in store.load(self.sid)["world_memory"] if m.get("scope") == "event"]
        self.assertEqual(len(events), 3)
        self.assertEqual({e["turn"] for e in events}, {6, 44, 47})

    def test_same_batch_dup_key_last_wins(self):
        store.upsert_world_memory(self.sid, [
            self._state("ent_token", "旧状态", 5),
            self._state("ent_token", "新状态", 6),
        ])
        token = [m for m in store.load(self.sid)["world_memory"] if m.get("canonical_id") == "ent_token"]
        self.assertEqual(len(token), 1)
        self.assertEqual(token[0]["text"], "新状态")

    def test_save_and_load_world_entities(self):
        ents = {"ent_token": {"name": "玄色小牌", "aliases": ["小牌"], "identity": "后山黑牌"}}
        store.save_world_entities(self.sid, ents)
        self.assertEqual(store.load(self.sid)["world_entities"], ents)


class WorldMemoryParseTests(unittest.TestCase):
    """game 层：解析 scope/subject、实体消解、dossier 时间标注。"""

    def test_parse_defaults_missing_scope_to_event(self):
        raw = '[{"type":"plot","text":"发生了一件事","entities":[]}]'
        items = game._parse_extracted_memories(raw, turn=3, entities={})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["scope"], "event")

    def test_parse_invalid_scope_to_event(self):
        raw = '[{"scope":"bogus","type":"plot","text":"x"}]'
        items = game._parse_extracted_memories(raw, turn=1, entities={})
        self.assertEqual(items[0]["scope"], "event")

    def test_parse_state_new_entity_gets_canonical_id(self):
        entities = {}
        raw = '[{"scope":"state","type":"item","subject":"玄色小牌","matched_id":"","text":"渗白雾"}]'
        items = game._parse_extracted_memories(raw, turn=5, entities=entities)
        cid = items[0].get("canonical_id")
        self.assertTrue(cid)
        self.assertIn(cid, entities)
        self.assertEqual(entities[cid]["name"], "玄色小牌")

    def test_parse_state_matched_entity_reuses_id_and_adds_alias(self):
        entities = {"ent_token": {"name": "玄色小牌", "aliases": [], "identity": "后山黑牌"}}
        raw = '[{"scope":"state","type":"item","subject":"小牌","matched_id":"ent_token","text":"发凛"}]'
        items = game._parse_extracted_memories(raw, turn=11, entities=entities)
        self.assertEqual(items[0]["canonical_id"], "ent_token")
        self.assertIn("小牌", entities["ent_token"]["aliases"])
        self.assertEqual(len(entities), 1)  # 没有新建实体

    def test_dossier_event_has_turn_prefix(self):
        dossier = game._world_memory_dossier([
            {"scope": "event", "type": "plot", "text": "宗门放话查验", "turn": 44},
        ])
        self.assertIn("第44回合", dossier)

    def test_dossier_state_marked_current(self):
        dossier = game._world_memory_dossier([
            {"scope": "state", "type": "item", "text": "小牌触碰会反噬", "turn": 60},
        ])
        self.assertIn("现状", dossier)


if __name__ == "__main__":
    unittest.main()
