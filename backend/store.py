"""SQLite 持久化层：存档的增删改查。

一个存档(save) = 一整局游戏，包含：
- messages   : 喂给 LLM 的消息数组（会按轮数截断，省 token）
- transcript : 展示用的完整剧情，只增不删（读档时重放全程）
- character_state : 主角当前状态快照（从最新《状态》面板解析）
- world_memory : 长期世界记忆（剧情事实、问询、人物、地点、物品等）

单机单进程使用，每次操作开独立连接，简单可靠。
"""

import json
import os
import re
import sqlite3
import time
import uuid

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "saves.db")
DEFAULT_WORLD_SEASON = "\u6df1\u79cb"
DEFAULT_CALENDAR_LABEL = "\u4ed9\u5386"


def _conn() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    """建表（幂等），并对老库补齐新列。"""
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saves (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                messages    TEXT NOT NULL,   -- JSON: list[dict]
                transcript  TEXT NOT NULL,   -- JSON: list[dict{role,text}]
                turns       INTEGER NOT NULL DEFAULT 0,
                lore        TEXT NOT NULL DEFAULT '[]',  -- JSON: list[dict{q,a,ts}]，见闻录
                character_state TEXT NOT NULL DEFAULT '{}', -- JSON: dict，主角当前状态
                world_memory TEXT NOT NULL DEFAULT '[]', -- JSON: list[dict]，长期世界记忆
                world_entities TEXT NOT NULL DEFAULT '{}', -- JSON: dict，规范实体表 canonical_id->{name,aliases,identity}
                inventory   TEXT NOT NULL DEFAULT '[]',  -- JSON: list[dict{id,name,attrs,kind,whereabouts,last_turn}]，物品影子库
                director_state TEXT NOT NULL DEFAULT '{}', -- JSON: dict，导演模块状态（当前爽点/留白期等）
                stage_summary TEXT NOT NULL DEFAULT '', -- 低频更新的历史阶段摘要
                summary_turn INTEGER NOT NULL DEFAULT 0,
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            )
            """
        )
        # 老库迁移：改动前建的表没有 lore/inventory 列，幂等补上
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(saves)")}
        if "lore" not in cols:
            conn.execute("ALTER TABLE saves ADD COLUMN lore TEXT NOT NULL DEFAULT '[]'")
        if "inventory" not in cols:
            conn.execute("ALTER TABLE saves ADD COLUMN inventory TEXT NOT NULL DEFAULT '[]'")
        if "character_state" not in cols:
            conn.execute("ALTER TABLE saves ADD COLUMN character_state TEXT NOT NULL DEFAULT '{}'")
        if "world_memory" not in cols:
            conn.execute("ALTER TABLE saves ADD COLUMN world_memory TEXT NOT NULL DEFAULT '[]'")
        if "world_entities" not in cols:
            conn.execute("ALTER TABLE saves ADD COLUMN world_entities TEXT NOT NULL DEFAULT '{}'")
        if "director_state" not in cols:
            conn.execute("ALTER TABLE saves ADD COLUMN director_state TEXT NOT NULL DEFAULT '{}'")
        if "stage_summary" not in cols:
            conn.execute("ALTER TABLE saves ADD COLUMN stage_summary TEXT NOT NULL DEFAULT ''")
        if "summary_turn" not in cols:
            conn.execute("ALTER TABLE saves ADD COLUMN summary_turn INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_request_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                save_id TEXT,
                request_type TEXT NOT NULL,
                protocol TEXT NOT NULL,
                model TEXT NOT NULL,
                status TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                input_chars INTEGER NOT NULL,
                output_chars INTEGER NOT NULL,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                cache_hit_tokens INTEGER,
                cache_miss_tokens INTEGER,
                error_type TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                save_id TEXT,
                turn INTEGER,
                agent_type TEXT NOT NULL,
                protocol TEXT NOT NULL,
                model TEXT NOT NULL,
                stream INTEGER NOT NULL DEFAULT 0,
                input_messages TEXT NOT NULL DEFAULT '[]',
                raw_output TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                error_type TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_traces_save_turn "
            "ON agent_traces(save_id, turn, id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS save_opportunity_rewards (
                payoff_id       TEXT PRIMARY KEY,
                save_id         TEXT NOT NULL,
                opportunity_id  TEXT NOT NULL,
                reward_kind     TEXT NOT NULL,
                reward_id       TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                created_turn    INTEGER NOT NULL,
                triggered_turn  INTEGER,
                updated_at      REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_save_opportunity_rewards_save "
            "ON save_opportunity_rewards(save_id, status)"
        )
        _init_world_tables(conn)
        _migrate_lore_to_world_memory(conn)
        _migrate_character_state(conn)


_STATUS_RE = re.compile(r"《状态》(.*?)《/状态》", re.S)
_STATE_FIELDS = {
    "境界": "realm",
    "气血": "health",
    "灵力": "spiritual_power",
    "修为": "cultivation",
    "状态": "condition",
    "资源": "resources",
    "法宝": "artifacts",
}


def _parse_character_state(status_text: str, turn: int, updated_at: float) -> dict:
    state = {}
    for line in status_text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(.+?)[：:]\s*(.*)$", line)
        if not m:
            continue
        key = _STATE_FIELDS.get(m.group(1).strip())
        if key:
            state[key] = m.group(2).strip()
    if not state:
        return {}
    state["turn"] = turn
    state["updated_at"] = updated_at
    return state


def _latest_character_state(transcript: list[dict], turns: int, updated_at: float) -> dict:
    for blk in reversed(transcript):
        if blk.get("role") != "narration":
            continue
        match = _STATUS_RE.search(blk.get("text", ""))
        if match:
            return _parse_character_state(match.group(1), turns, updated_at)
    return {}


def _migrate_character_state(conn: sqlite3.Connection) -> None:
    """从旧 transcript 的最后一个状态面板回填主角状态；已有值不覆盖。"""
    rows = conn.execute(
        "SELECT id, transcript, turns, updated_at, character_state FROM saves "
        "WHERE transcript IS NOT NULL AND transcript != '[]'"
    ).fetchall()
    for row in rows:
        try:
            existing = json.loads(row["character_state"] or "{}")
            transcript = json.loads(row["transcript"] or "[]")
        except json.JSONDecodeError:
            continue
        if existing:
            continue
        character_state = _latest_character_state(
            transcript,
            int(row["turns"] or 0),
            float(row["updated_at"] or time.time()),
        )
        if character_state:
            conn.execute(
                "UPDATE saves SET character_state=? WHERE id=?",
                (json.dumps(character_state, ensure_ascii=False), row["id"]),
            )


def _migrate_lore_to_world_memory(conn: sqlite3.Connection) -> None:
    """把旧见闻录迁移成 qa 类型世界记忆；已迁移过的存档不重复写。"""
    rows = conn.execute(
        "SELECT id, turns, lore, world_memory FROM saves WHERE lore IS NOT NULL AND lore != '[]'"
    ).fetchall()
    for row in rows:
        try:
            existing = json.loads(row["world_memory"] or "[]")
            lore = json.loads(row["lore"] or "[]")
        except json.JSONDecodeError:
            continue
        if existing or not lore:
            continue
        migrated = []
        for entry in lore:
            q = (entry.get("q") or "").strip()
            a = (entry.get("a") or "").strip()
            if not q and not a:
                continue
            try:
                ts = float(entry.get("ts") or time.time())
            except (TypeError, ValueError):
                ts = time.time()
            migrated.append({
                "id": uuid.uuid4().hex,
                "type": "qa",
                "text": f"问：{q}　答：{a}" if q else a,
                "entities": [],
                "turn": row["turns"],
                "importance": 0.7,
                "source": "inquiry_migration",
                "q": q,
                "a": a,
                "ts": ts,
            })
        if migrated:
            conn.execute(
                "UPDATE saves SET world_memory=? WHERE id=?",
                (json.dumps(migrated, ensure_ascii=False), row["id"]),
            )


def create(name: str, messages: list[dict]) -> str:
    """新建存档，返回 save_id。"""
    sid = uuid.uuid4().hex
    now = time.time()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO saves (id, name, messages, transcript, turns, lore, inventory, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, '[]', '[]', ?, ?)",
            (sid, name, json.dumps(messages, ensure_ascii=False), "[]", now, now),
        )
        _ensure_default_save_world_state(conn, sid)
    return sid


# ---- 固定世界库 + 存档世界状态 ----

WORLD_VERSION = 1
WORLD_NAME = "玄苍大陆"

_REGIONS = [
    ("qingwu_county", "青梧郡", "凡人与低阶修士边界", "灵气稀薄，村镇、山野、低阶坊市与小宗门交错。"),
    ("chiyuan_ridge", "赤渊岭", "矿脉、火脉与散修争夺", "赤铁矿场、地火裂隙和散修黑市密布。"),
    ("hanshui_marsh", "寒水泽", "水域、妖族与魂道遗迹", "湖泽岛屿连绵，水府、沉船和阴魂传闻极多。"),
    ("central_lingdu", "中州灵都", "宗门、世家与仙城秩序", "大宗门、仙族、万宝楼与散修登记体系的中心。"),
    ("wanzang_waste", "万葬荒原", "古战场与禁地", "古战场阴气沉积，剑冢、碑林与古宗废墟散落其间。"),
    ("tianduan_mountains", "天断山脉", "大陆边界与后期险地", "雷泽、妖岭与空间裂隙横亘在大陆边缘。"),
]

_LOCATIONS = [
    ("baishi_village", "qingwu_county", "白石村", "village", None, "偏僻凡人村落，主角开局之地。"),
    ("baishi_back_mountain", "qingwu_county", "白石村后山", "wild", "baishi_village", "草木繁密，夜里偶有微光。"),
    ("baishi_ruined_temple", "qingwu_county", "村外破庙", "site", "baishi_village", "荒废多年，香火断绝。"),
    ("qingxi_town", "qingwu_county", "青溪镇", "town", None, "白石村外最近的大镇，有药铺、武馆和散修传闻。"),
    ("qingmu_market", "qingwu_county", "青木集", "market", None, "半公开的低阶散修交易地。"),
    ("black_wind_mountain", "qingwu_county", "黑风山", "wild", None, "山势阴沉，外围有猎道，深处有妖兽。"),
    ("black_wind_outer", "qingwu_county", "黑风山外围", "wild", "black_wind_mountain", "乱石坡与密林交错，常有采药人失踪。"),
    ("xuanxiao_outer_gate", "qingwu_county", "玄霄宗外山门", "sect", None, "青梧郡一带有名的修仙宗门入口。"),
    ("qinglian_valley", "qingwu_county", "青莲谷", "secret_entrance", None, "谷中常年雾锁，少有人能深入。"),
    ("red_iron_mine", "chiyuan_ridge", "赤铁矿场", "mine", None, "赤渊岭最大的赤铁矿脉。"),
    ("lieyang_market", "chiyuan_ridge", "烈阳坊市", "market", None, "散修与矿修交易之地。"),
    ("bloodsha_cave_ruin", "chiyuan_ridge", "血煞洞遗址", "ruin", None, "旧魔修洞府残址。"),
    ("earthfire_cavern", "chiyuan_ridge", "地火窟", "site", None, "天然地火汇聚之处。"),
    ("hanshui_market", "hanshui_marsh", "寒水坊", "market", None, "寒水泽水路坊市。"),
    ("mist_conch_island", "hanshui_marsh", "雾螺岛", "island", None, "散修聚居岛。"),
    ("sunken_star_lake", "hanshui_marsh", "沉星湖", "lake", None, "湖底传有古沉船与水府残址。"),
    ("white_bone_ferry", "hanshui_marsh", "白骨渡", "ferry", None, "阴气重的古渡口。"),
    ("lingdu_city", "central_lingdu", "灵都仙城", "city", None, "玄苍大陆中部最大仙城。"),
    ("tianheng_sect", "central_lingdu", "天衡宗", "sect", None, "中州正道大宗。"),
    ("taixuan_academy", "central_lingdu", "太玄书院", "academy", None, "以功法、阵符和典籍闻名。"),
    ("wanbao_tower", "central_lingdu", "万宝楼总阁", "market", None, "拍卖、交易和情报势力总阁。"),
    ("wanzang_camp", "wanzang_waste", "万葬原外围营地", "camp", None, "进入万葬荒原前的修士落脚处。"),
    ("broken_sword_mound", "wanzang_waste", "断剑冢", "ruin", None, "古战场中的剑修遗迹。"),
    ("soul_stele_forest", "wanzang_waste", "镇魂碑林", "ruin", None, "碑林封着残魂与旧战记忆。"),
    ("nameless_old_sect", "wanzang_waste", "无名古宗废墟", "ruin", None, "失落古宗门遗址。"),
    ("tianduan_pass", "tianduan_mountains", "天断关", "pass", None, "通往天断山脉的要塞。"),
    ("thunder_marsh_peak", "tianduan_mountains", "雷泽峰", "peak", None, "雷气常年不散。"),
    ("sky_demon_ridge", "tianduan_mountains", "天妖岭", "wild", None, "高阶妖族活动区域。"),
    ("rift_gorge", "tianduan_mountains", "裂天峡", "rift", None, "传有空间裂隙。"),
]

_ROUTES = [
    ("baishi_to_qingxi", "baishi_village", "qingxi_town", "白石村至青溪镇土路", "low", "low", "村口土路通往青溪镇。"),
    ("baishi_to_back_mountain", "baishi_village", "baishi_back_mountain", "白石村后山小径", "low", "low", "村后柴道入山。"),
    ("baishi_to_ruined_temple", "baishi_village", "baishi_ruined_temple", "村外破庙岔路", "low", "low", "村西荒草路通向破庙。"),
    ("qingxi_to_qingmu", "qingxi_town", "qingmu_market", "青溪镇至青木集商道", "low", "medium", "跟着商队最稳。"),
    ("qingxi_to_black_wind", "qingxi_town", "black_wind_outer", "青溪镇至黑风山外围猎道", "medium", "medium", "猎户和采药人偶尔走此道。"),
    ("black_wind_outer_to_mountain", "black_wind_outer", "black_wind_mountain", "黑风山入山路", "medium", "high", "越往深处越容易迷路。"),
    ("qingmu_to_xuanxiao", "qingmu_market", "xuanxiao_outer_gate", "青木集至玄霄宗外山门", "medium", "medium", "散修地图上常见的山门方向。"),
    ("qingmu_to_qinglian", "qingmu_market", "qinglian_valley", "青木集至青莲谷旧路", "medium", "medium", "旧路常年雾锁。"),
    ("qingmu_to_lieyang", "qingmu_market", "lieyang_market", "青木集至烈阳坊市远商道", "high", "medium", "跨郡商道，需地图或商队。"),
    ("lieyang_to_bloodsha", "lieyang_market", "bloodsha_cave_ruin", "烈阳坊市至血煞洞遗址", "high", "high", "散修间流传的险路。"),
    ("lieyang_to_earthfire", "lieyang_market", "earthfire_cavern", "烈阳坊市至地火窟", "medium", "medium", "炼器师常走。"),
    ("lingdu_to_wanbao", "lingdu_city", "wanbao_tower", "灵都内城至万宝楼", "low", "low", "仙城内路线。"),
    ("lingdu_to_tianheng", "lingdu_city", "tianheng_sect", "灵都至天衡宗山门", "medium", "low", "中州正道山道。"),
    ("lingdu_to_taixuan", "lingdu_city", "taixuan_academy", "灵都至太玄书院", "low", "low", "官道清晰。"),
    ("wanzang_to_sword", "wanzang_camp", "broken_sword_mound", "万葬营地至断剑冢", "high", "high", "古战场外围险路。"),
    ("tianduan_to_thunder", "tianduan_pass", "thunder_marsh_peak", "天断关至雷泽峰", "high", "high", "山路险峻，雷雨频繁。"),
]

_FACTIONS = [
    ("xuanxiao_sect", "玄霄宗", "sect", "qingwu_county", "青梧郡修仙宗门，收徒严格。"),
    ("wanbao_tower", "万宝楼", "merchant", "central_lingdu", "经营拍卖、交易与情报。"),
    ("tianheng_sect", "天衡宗", "sect", "central_lingdu", "中州正道大宗。"),
    ("taixuan_academy", "太玄书院", "academy", "central_lingdu", "典籍、阵符与功法传承势力。"),
    ("danxia_valley", "丹霞谷", "alchemy", "central_lingdu", "丹道势力。"),
    ("shen_clan", "沈氏仙族", "clan", "central_lingdu", "中州修仙世家。"),
    ("red_crow_fort", "赤鸦寨", "loose", "chiyuan_ridge", "赤渊岭半匪半修士势力。"),
]

_ARTS = [
    ("yin_qi_jue", "引气诀", "黄阶下品", "吐纳", "neutral", "炼气三层", "凡人入门吐纳法，流传很广。", "common", "qingxi_town", "散修流通"),
    ("small_zhoutian", "小周天吐纳法", "黄阶中品", "吐纳", "neutral", "炼气六层", "散修常见功法。", "common", "qingmu_market", "散修流通"),
    ("qingxin_naling", "清心纳灵功", "黄阶上品", "吐纳", "neutral", "炼气九层", "稳妥但进境偏慢。", "common", "xuanxiao_sect", "宗门外门"),
    ("qingmu_yangqi", "青木养气诀", "黄阶上品", "五行", "wood", "炼气九层", "木行入门正法。", "common", "qingmu_market", "坊市流通"),
    ("hanyuan_water", "寒渊凝水诀", "玄阶下品", "五行", "water", "筑基初期", "水行功法。", "hidden", "hanshui_market", "寒水坊"),
    ("lieyang_fire", "烈阳吐火经", "玄阶下品", "五行", "fire", "筑基中期", "火行功法。", "hidden", "lieyang_market", "烈阳坊市"),
    ("small_wuxing_guiyuan", "小五行归元功", "玄阶中品", "五行", "five_elements", "筑基圆满", "五行均衡，资源消耗较大。", "restricted", "xuanxiao_sect", "玄霄宗"),
    ("qingfeng_sword", "青锋剑诀", "黄阶上品", "剑修", "metal", "炼气九层", "低阶剑诀。", "common", "qingmu_market", "散修流通"),
    ("xuanxiao_sword", "玄霄剑经", "玄阶上品", "剑修", "metal", "金丹初期", "玄霄宗剑修传承。", "restricted", "xuanxiao_sect", "玄霄宗"),
    ("iron_bone", "铁骨功", "黄阶中品", "炼体", "earth", "炼气期", "低阶炼体法。", "common", "qingxi_town", "武馆流通"),
    ("grasswood_alchemy", "草木丹经", "黄阶上品", "丹道", "wood", "炼气期", "炼丹入门典籍。", "common", "qingmu_market", "坊市流通"),
    ("basic_talisman", "基础符箓录", "黄阶中品", "符箓", "neutral", "炼气期", "基础符箓典籍。", "common", "qingmu_market", "坊市流通"),
    ("redsha_blood", "赤煞炼血经", "玄阶下品", "魔道", "blood", "筑基后期", "血道速成法。", "forbidden", "bloodsha_cave_ruin", "血煞洞遗址"),
    ("yin_soul_nian", "阴魂寄念术", "玄阶上品", "魂道", "soul", "金丹初期", "魂道秘术。", "hidden", "white_bone_ferry", "白骨渡"),
    ("qinglian_upper", "青莲化生诀·上篇", "地阶残篇", "古法", "wood", "筑基圆满", "青莲古法残篇。", "lost", "qinglian_valley", "青莲秘境"),
    ("taixu_fragment", "太虚观想录·残页", "天阶残篇", "古法", "soul", "未知", "观想古法残页。", "lost", "nameless_old_sect", "无名古宗废墟"),
]

_OPPORTUNITIES = [
    ("baishi_spirit_spring", "白石后山灵泉", "baishi_back_mountain", "resource", "后山夜里偶有青光。", "low", "unknown"),
    ("ruined_temple_bones", "破庙道人遗骨", "baishi_ruined_temple", "art", "破庙石像后有旧物痕迹。", "low", "unknown"),
    ("black_wind_cave", "黑风山筑基洞府", "black_wind_mountain", "cave", "黑风山深处有旧阵封痕。", "medium", "unknown"),
    ("qingwu_fair", "青梧小会", "qingmu_market", "market", "低阶散修定期聚集交易。", "low", "unknown"),
    ("qinglian_secret", "青莲秘境", "qinglian_valley", "realm", "青莲谷雾锁，传有周期性开启。", "high", "unknown"),
    ("bloodsha_manual_cache", "血煞洞传承暗格", "bloodsha_cave_ruin", "art", "遗址中残留血纹石室。", "high", "unknown"),
    ("sunken_star_water_mansion", "沉星湖水府", "sunken_star_lake", "realm", "湖底有旧水府传闻。", "high", "unknown"),
    ("broken_sword_inheritance", "断剑冢剑意传承", "broken_sword_mound", "inheritance", "断剑冢内剑气不散。", "high", "unknown"),
]

_REALMS = [
    ("qinglian_realm", "青莲秘境", "qinglian_valley", "周期型秘境", "十年一开", "炼气后期至筑基初期"),
    ("black_wind_foundation_cave", "黑风山筑基洞府", "black_wind_mountain", "洞府", "封印松动后可入", "炼气后期至筑基期"),
    ("sunken_star_mansion", "沉星湖水府", "sunken_star_lake", "水府", "需水路线索", "筑基至金丹"),
    ("nameless_old_sect_realm", "无名古宗废墟", "nameless_old_sect", "古宗遗迹", "灾变/残图", "金丹以上"),
]

_DEFAULT_KNOWLEDGE = [
    ("location", "baishi_village", "confirmed", "high", "亲身所在", "已确认白石村。"),
    ("location", "baishi_back_mountain", "confirmed", "high", "村中生活", "知道后山小径。"),
    ("location", "baishi_ruined_temple", "confirmed", "medium", "村中传闻", "知道村外有座破庙。"),
    ("location", "qingxi_town", "confirmed", "high", "村民往来", "知道青溪镇大致方向。"),
    ("route", "baishi_to_qingxi", "confirmed", "high", "村民往来", "村口土路可到青溪镇。"),
    ("route", "baishi_to_back_mountain", "confirmed", "high", "村中生活", "柴道入后山。"),
    ("route", "baishi_to_ruined_temple", "confirmed", "medium", "村中生活", "荒草岔路通破庙。"),
    ("location", "black_wind_mountain", "rumored", "medium", "村民传闻", "听过黑风山之名，但不知深处路径。"),
    ("location", "xuanxiao_outer_gate", "rumored", "low", "仙师传闻", "听过玄霄宗招收弟子的传闻。"),
    ("location", "qingmu_market", "rumored", "low", "散修传闻", "听过青木集有修士交易。"),
    ("faction", "xuanxiao_sect", "rumored", "low", "仙师传闻", "听过玄霄宗。"),
    ("art", "yin_qi_jue", "rumored", "low", "凡间传闻", "听过入门吐纳法。"),
]


def _init_world_tables(conn: sqlite3.Connection) -> None:
    """固定世界表与每档世界状态表。world_* 是事实，save_* 是玩家视野/状态。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS world_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS world_regions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL,
            summary TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS world_locations (
            id TEXT PRIMARY KEY,
            region_id TEXT NOT NULL,
            name TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            parent_id TEXT,
            summary TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS world_routes (
            id TEXT PRIMARY KEY,
            from_location_id TEXT NOT NULL,
            to_location_id TEXT NOT NULL,
            name TEXT NOT NULL,
            difficulty TEXT NOT NULL,
            risk TEXT NOT NULL,
            summary TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS world_factions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            region_id TEXT NOT NULL,
            summary TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS world_arts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            rank TEXT NOT NULL,
            category TEXT NOT NULL,
            primary_element TEXT NOT NULL,
            realm_cap TEXT NOT NULL,
            summary TEXT NOT NULL,
            visibility TEXT NOT NULL,
            source_location_id TEXT,
            source_label TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS world_opportunities (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            location_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            clue TEXT NOT NULL,
            danger TEXT NOT NULL,
            default_state TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS world_realms (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            entrance_location_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            opening_rule TEXT NOT NULL,
            entry_limit TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS save_player_location (
            save_id TEXT PRIMARY KEY,
            region_id TEXT NOT NULL,
            location_id TEXT NOT NULL,
            site_name TEXT NOT NULL DEFAULT '',
            location_state TEXT NOT NULL DEFAULT '安全',
            intended_destination_id TEXT,
            lost_risk TEXT NOT NULL DEFAULT '无',
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS save_world_time (
            save_id TEXT PRIMARY KEY,
            day INTEGER NOT NULL DEFAULT 1,
            minute_of_day INTEGER NOT NULL DEFAULT 930,
            season TEXT NOT NULL DEFAULT '',
            calendar_label TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS save_player_knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            save_id TEXT NOT NULL,
            knowledge_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            status TEXT NOT NULL,
            reliability TEXT NOT NULL DEFAULT 'medium',
            source TEXT NOT NULL DEFAULT '',
            detail_level TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(save_id, knowledge_type, target_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS save_opportunity_states (
            save_id TEXT NOT NULL,
            opportunity_id TEXT NOT NULL,
            state TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL,
            PRIMARY KEY(save_id, opportunity_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS save_realm_states (
            save_id TEXT NOT NULL,
            realm_id TEXT NOT NULL,
            state TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL,
            PRIMARY KEY(save_id, realm_id)
        )
        """
    )
    _seed_world(conn)


def _seed_world(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO world_regions (id, name, role, summary) VALUES (?, ?, ?, ?)",
        _REGIONS,
    )
    conn.executemany(
        "INSERT OR IGNORE INTO world_locations (id, region_id, name, kind, parent_id, summary) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        _LOCATIONS,
    )
    conn.executemany(
        "INSERT OR IGNORE INTO world_routes (id, from_location_id, to_location_id, name, difficulty, risk, summary) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        _ROUTES,
    )
    conn.executemany(
        "INSERT OR IGNORE INTO world_factions (id, name, kind, region_id, summary) VALUES (?, ?, ?, ?, ?)",
        _FACTIONS,
    )
    conn.executemany(
        "INSERT OR IGNORE INTO world_arts "
        "(id, name, rank, category, primary_element, realm_cap, summary, visibility, source_location_id, source_label) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        _ARTS,
    )
    conn.executemany(
        "INSERT OR IGNORE INTO world_opportunities "
        "(id, name, location_id, kind, clue, danger, default_state) VALUES (?, ?, ?, ?, ?, ?, ?)",
        _OPPORTUNITIES,
    )
    conn.executemany(
        "INSERT OR IGNORE INTO world_realms "
        "(id, name, entrance_location_id, kind, opening_rule, entry_limit) VALUES (?, ?, ?, ?, ?, ?)",
        _REALMS,
    )
    conn.execute("INSERT OR REPLACE INTO world_meta (key, value) VALUES ('world_name', ?)", (WORLD_NAME,))
    conn.execute("INSERT OR REPLACE INTO world_meta (key, value) VALUES ('world_version', ?)", (str(WORLD_VERSION),))


def _ensure_default_save_world_state(conn: sqlite3.Connection, sid: str) -> None:
    now = time.time()
    conn.execute(
        """
        INSERT OR IGNORE INTO save_player_location
        (save_id, region_id, location_id, site_name, location_state, intended_destination_id, lost_risk, updated_at)
        VALUES (?, 'qingwu_county', 'baishi_village', '村西老槐树', '安全', NULL, '无', ?)
        """,
        (sid, now),
    )
    save_row = conn.execute("SELECT turns FROM saves WHERE id=?", (sid,)).fetchone()
    elapsed_turns = max(0, int(save_row["turns"] or 0) - 1) if save_row else 0
    initial_minute = min(1439, 930 + elapsed_turns * 15)
    conn.execute(
        """
        INSERT OR IGNORE INTO save_world_time
        (save_id, day, minute_of_day, season, calendar_label, updated_at)
        VALUES (?, 1, ?, ?, ?, ?)
        """,
        (sid, initial_minute, DEFAULT_WORLD_SEASON, DEFAULT_CALENDAR_LABEL, now),
    )
    for kind, target_id, status, reliability, source, notes in _DEFAULT_KNOWLEDGE:
        conn.execute(
            """
            INSERT OR IGNORE INTO save_player_knowledge
            (save_id, knowledge_type, target_id, status, reliability, source, detail_level, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?)
            """,
            (sid, kind, target_id, status, reliability, source, notes, now, now),
        )
    for oid, *_ in _OPPORTUNITIES:
        conn.execute(
            """
            INSERT OR IGNORE INTO save_opportunity_states
            (save_id, opportunity_id, state, notes, updated_at)
            VALUES (?, ?, 'unknown', '', ?)
            """,
            (sid, oid, now),
        )
    for rid, *_ in _REALMS:
        conn.execute(
            """
            INSERT OR IGNORE INTO save_realm_states
            (save_id, realm_id, state, notes, updated_at)
            VALUES (?, ?, 'unknown', '', ?)
            """,
            (sid, rid, now),
        )


def ensure_save_world_state(sid: str) -> None:
    """给老存档补默认位置/知识；新存档 create 时已做。"""
    with _conn() as conn:
        _ensure_default_save_world_state(conn, sid)


def world_snapshot(sid: str) -> dict | None:
    """读取当前存档的固定世界视野与真实位置。"""
    with _conn() as conn:
        _ensure_default_save_world_state(conn, sid)
        loc = conn.execute(
            """
            SELECT spl.*, wr.name AS region_name, wl.name AS location_name, wl.kind AS location_kind,
                   wl.summary AS location_summary
            FROM save_player_location spl
            JOIN world_regions wr ON wr.id = spl.region_id
            JOIN world_locations wl ON wl.id = spl.location_id
            WHERE spl.save_id=?
            """,
            (sid,),
        ).fetchone()
        if loc is None:
            return None
        world_time = conn.execute(
            "SELECT * FROM save_world_time WHERE save_id=?", (sid,)
        ).fetchone()
        knowledge = conn.execute(
            "SELECT * FROM save_player_knowledge WHERE save_id=? ORDER BY knowledge_type, status, id",
            (sid,),
        ).fetchall()
        regions = conn.execute("SELECT * FROM world_regions ORDER BY rowid").fetchall()
        locations = conn.execute("SELECT * FROM world_locations ORDER BY rowid").fetchall()
        routes = conn.execute("SELECT * FROM world_routes ORDER BY rowid").fetchall()
        factions = conn.execute("SELECT * FROM world_factions ORDER BY rowid").fetchall()
        arts = conn.execute("SELECT * FROM world_arts ORDER BY rowid").fetchall()
        opportunities = conn.execute("SELECT * FROM world_opportunities ORDER BY rowid").fetchall()
        realms = conn.execute("SELECT * FROM world_realms ORDER BY rowid").fetchall()
    return {
        "location": dict(loc),
        "time": dict(world_time),
        "knowledge": [dict(row) for row in knowledge],
        "regions": [dict(row) for row in regions],
        "locations": [dict(row) for row in locations],
        "routes": [dict(row) for row in routes],
        "factions": [dict(row) for row in factions],
        "arts": [dict(row) for row in arts],
        "opportunities": [dict(row) for row in opportunities],
        "realms": [dict(row) for row in realms],
    }


def update_player_location(
    sid: str,
    *,
    region_id: str,
    location_id: str,
    site_name: str = "",
    location_state: str = "安全",
    intended_destination_id: str | None = None,
    lost_risk: str = "无",
) -> None:
    with _conn() as conn:
        conn.execute(
            """
            UPDATE save_player_location
            SET region_id=?, location_id=?, site_name=?, location_state=?,
                intended_destination_id=?, lost_risk=?, updated_at=?
            WHERE save_id=?
            """,
            (region_id, location_id, site_name, location_state, intended_destination_id, lost_risk, time.time(), sid),
        )


def advance_world_time(sid: str, minutes: int) -> dict:
    """Advance the persistent story clock; the clock can never move backward."""
    elapsed = max(0, int(minutes))
    with _conn() as conn:
        _ensure_default_save_world_state(conn, sid)
        row = conn.execute(
            "SELECT * FROM save_world_time WHERE save_id=?", (sid,)
        ).fetchone()
        total = (int(row["day"]) - 1) * 1440 + int(row["minute_of_day"]) + elapsed
        day, minute_of_day = divmod(total, 1440)
        conn.execute(
            "UPDATE save_world_time SET day=?, minute_of_day=?, updated_at=? WHERE save_id=?",
            (day + 1, minute_of_day, time.time(), sid),
        )
        updated = conn.execute(
            "SELECT * FROM save_world_time WHERE save_id=?", (sid,)
        ).fetchone()
    return dict(updated)


def set_intended_destination(sid: str, target_id: str | None) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE save_player_location SET intended_destination_id=?, updated_at=? WHERE save_id=?",
            (target_id, time.time(), sid),
        )


def upsert_knowledge(
    sid: str,
    knowledge_type: str,
    target_id: str,
    status: str,
    *,
    reliability: str = "medium",
    source: str = "",
    notes: str = "",
) -> None:
    now = time.time()
    rank = {"unknown": 0, "rumored": 1, "known": 2, "confirmed": 3}
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT status FROM save_player_knowledge
            WHERE save_id=? AND knowledge_type=? AND target_id=?
            """,
            (sid, knowledge_type, target_id),
        ).fetchone()
        if row is not None and rank.get(row["status"], 0) >= rank.get(status, 0):
            return
        conn.execute(
            """
            INSERT INTO save_player_knowledge
            (save_id, knowledge_type, target_id, status, reliability, source, detail_level, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?)
            ON CONFLICT(save_id, knowledge_type, target_id) DO UPDATE SET
                status=excluded.status,
                reliability=excluded.reliability,
                source=excluded.source,
                notes=excluded.notes,
                updated_at=excluded.updated_at
            """,
            (sid, knowledge_type, target_id, status, reliability, source, notes, now, now),
        )


def save_state(sid: str, messages: list[dict], transcript: list[dict], turns: int) -> None:
    """覆盖写入某存档的当前状态（每手落盘）。"""
    with _conn() as conn:
        conn.execute(
            "UPDATE saves SET messages=?, transcript=?, turns=?, updated_at=? WHERE id=?",
            (
                json.dumps(messages, ensure_ascii=False),
                json.dumps(transcript, ensure_ascii=False),
                turns,
                time.time(),
                sid,
            ),
        )


def save_stage_summary(sid: str, summary: str, summary_turn: int) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE saves SET stage_summary=?, summary_turn=? WHERE id=?",
            (summary, summary_turn, sid),
        )


def record_llm_request_metric(metric: dict) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO llm_request_metrics (
                save_id, request_type, protocol, model, status, duration_ms,
                input_chars, output_chars, prompt_tokens, completion_tokens,
                cache_hit_tokens, cache_miss_tokens, error_type, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                metric.get("save_id"), metric.get("request_type") or "unknown",
                metric.get("protocol") or "", metric.get("model") or "",
                metric.get("status") or "error", int(metric.get("duration_ms") or 0),
                int(metric.get("input_chars") or 0), int(metric.get("output_chars") or 0),
                metric.get("prompt_tokens"), metric.get("completion_tokens"),
                metric.get("cache_hit_tokens"), metric.get("cache_miss_tokens"),
                metric.get("error_type") or "", time.time(),
            ),
        )


def list_llm_request_metrics(sid: str, limit: int = 30) -> list[dict] | None:
    """Return recent LLM requests for a save, newest first."""
    limit = max(1, min(int(limit), 50))
    with _conn() as conn:
        if conn.execute("SELECT 1 FROM saves WHERE id=?", (sid,)).fetchone() is None:
            return None
        rows = conn.execute(
            """
            SELECT id, request_type, protocol, model, status, duration_ms,
                   input_chars, output_chars, prompt_tokens, completion_tokens,
                   cache_hit_tokens, cache_miss_tokens, error_type, created_at
            FROM llm_request_metrics
            WHERE save_id=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (sid, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def record_agent_trace(trace: dict) -> int:
    """Append one immutable full-input/full-output Agent execution trace."""
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO agent_traces (
                save_id, turn, agent_type, protocol, model, stream,
                input_messages, raw_output, status, duration_ms,
                error_type, error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trace.get("save_id"),
                trace.get("turn"),
                trace.get("agent_type") or "unknown",
                trace.get("protocol") or "",
                trace.get("model") or "",
                int(bool(trace.get("stream"))),
                json.dumps(trace.get("input_messages") or [], ensure_ascii=False, default=str),
                str(trace.get("raw_output") or ""),
                trace.get("status") or "unknown",
                int(trace.get("duration_ms") or 0),
                trace.get("error_type") or "",
                trace.get("error_message") or "",
                float(trace.get("created_at") or time.time()),
            ),
        )
    return int(cur.lastrowid)


def list_agent_traces(
    sid: str, *, turn: int | None = None, limit: int = 100, include_content: bool = False
) -> list[dict] | None:
    """Read trace summaries, or complete payloads when explicitly requested."""
    limit = max(1, min(int(limit), 500))
    with _conn() as conn:
        if conn.execute("SELECT 1 FROM saves WHERE id=?", (sid,)).fetchone() is None:
            return None
        content_columns = ", input_messages, raw_output, error_message" if include_content else ""
        turn_clause = " AND turn=?" if turn is not None else ""
        params = (sid, int(turn), limit) if turn is not None else (sid, limit)
        rows = conn.execute(
            f"""
            SELECT id, save_id, turn, agent_type, protocol, model, stream,
                   status, duration_ms, error_type, created_at,
                   length(input_messages) AS input_chars, length(raw_output) AS output_chars
                   {content_columns}
            FROM agent_traces
            WHERE save_id=?{turn_clause}
            ORDER BY id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
    result = [dict(row) for row in rows]
    if include_content:
        for row in result:
            row["input_messages"] = json.loads(row["input_messages"] or "[]")
    return result


def save_lore(sid: str, lore: list[dict]) -> None:
    """只更新见闻录（问询旁路，不触发主状态落盘）。"""
    with _conn() as conn:
        conn.execute(
            "UPDATE saves SET lore=?, updated_at=? WHERE id=?",
            (json.dumps(lore, ensure_ascii=False), time.time(), sid),
        )


def save_world_memory(sid: str, world_memory: list[dict]) -> None:
    """只更新世界记忆。"""
    with _conn() as conn:
        conn.execute(
            "UPDATE saves SET world_memory=?, updated_at=? WHERE id=?",
            (json.dumps(world_memory, ensure_ascii=False), time.time(), sid),
        )


def save_character_state(sid: str, character_state: dict) -> None:
    """只更新主角当前状态快照。"""
    with _conn() as conn:
        conn.execute(
            "UPDATE saves SET character_state=?, updated_at=? WHERE id=?",
            (json.dumps(character_state, ensure_ascii=False), time.time(), sid),
        )


def append_world_memory(sid: str, items: list[dict]) -> list[dict] | None:
    """追加世界记忆并返回新列表；存档不存在返回 None。"""
    if not items:
        return load(sid)["world_memory"] if exists(sid) else None
    with _conn() as conn:
        row = conn.execute(
            "SELECT world_memory FROM saves WHERE id=?",
            (sid,),
        ).fetchone()
        if row is None:
            return None
        current = json.loads(row["world_memory"] or "[]")
        current.extend(items)
        conn.execute(
            "UPDATE saves SET world_memory=?, updated_at=? WHERE id=?",
            (json.dumps(current, ensure_ascii=False), time.time(), sid),
        )
        return current


def save_world_entities(sid: str, world_entities: dict) -> None:
    """只更新规范实体表。"""
    with _conn() as conn:
        conn.execute(
            "UPDATE saves SET world_entities=?, updated_at=? WHERE id=?",
            (json.dumps(world_entities, ensure_ascii=False), time.time(), sid),
        )


def _state_key(mem: dict) -> tuple | None:
    """状态型记忆的合并键：(type, canonical_id)。缺 canonical_id 或非状态型返回 None。"""
    if mem.get("scope") != "state":
        return None
    cid = mem.get("canonical_id")
    if not cid:
        return None
    return (mem.get("type"), cid)


def upsert_world_memory(sid: str, items: list[dict]) -> list[dict] | None:
    """写入世界记忆：事件型追加，状态型按 (type, canonical_id) 覆盖旧条。

    存档不存在返回 None；items 为空返回当前列表。同批多条命中同键时以最后一条为准。
    """
    if not items:
        return load(sid)["world_memory"] if exists(sid) else None
    with _conn() as conn:
        row = conn.execute(
            "SELECT world_memory FROM saves WHERE id=?",
            (sid,),
        ).fetchone()
        if row is None:
            return None
        current = json.loads(row["world_memory"] or "[]")
        # 建索引：状态键 -> 在 current 中的下标
        index: dict[tuple, int] = {}
        for i, m in enumerate(current):
            k = _state_key(m)
            if k is not None:
                index[k] = i
        for item in items:
            k = _state_key(item)
            if k is not None and k in index:
                current[index[k]] = item  # 覆盖旧状态（丢弃旧内容）
            else:
                current.append(item)
                if k is not None:
                    index[k] = len(current) - 1
        conn.execute(
            "UPDATE saves SET world_memory=?, updated_at=? WHERE id=?",
            (json.dumps(current, ensure_ascii=False), time.time(), sid),
        )
        return current


def save_inventory(sid: str, inventory: list[dict]) -> None:
    """只更新物品影子库。"""
    with _conn() as conn:
        conn.execute(
            "UPDATE saves SET inventory=?, updated_at=? WHERE id=?",
            (json.dumps(inventory, ensure_ascii=False), time.time(), sid),
        )


def save_director_state(sid: str, state: dict) -> None:
    """只更新导演模块状态。"""
    with _conn() as conn:
        conn.execute(
            "UPDATE saves SET director_state=?, updated_at=? WHERE id=?",
            (json.dumps(state, ensure_ascii=False), time.time(), sid),
        )


def save_opportunity_reward_binding(sid: str, payoff: dict | None) -> None:
    """Persist the director's save-specific opportunity-to-reward binding."""
    binding = payoff.get("binding") if isinstance(payoff, dict) else None
    required = ("opportunity_id", "reward_kind", "reward_id")
    if (
        not isinstance(binding, dict)
        or not payoff.get("id")
        or any(not binding.get(key) for key in required)
    ):
        return
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO save_opportunity_rewards (
                payoff_id, save_id, opportunity_id, reward_kind, reward_id,
                status, created_turn, triggered_turn, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(payoff_id) DO UPDATE SET
                status=excluded.status,
                triggered_turn=excluded.triggered_turn,
                updated_at=excluded.updated_at
            """,
            (
                payoff["id"], sid, binding.get("opportunity_id"),
                binding.get("reward_kind"), binding.get("reward_id"),
                payoff.get("status") or "pending", int(payoff.get("created_turn") or 0),
                payoff.get("triggered_turn"), time.time(),
            ),
        )


def list_opportunity_reward_bindings(sid: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT payoff_id, opportunity_id, reward_kind, reward_id, status,
                   created_turn, triggered_turn
            FROM save_opportunity_rewards
            WHERE save_id=?
            ORDER BY created_turn, payoff_id
            """,
            (sid,),
        ).fetchall()
    return [dict(row) for row in rows]


def load(sid: str) -> dict | None:
    """读取单个存档的完整数据；不存在返回 None。"""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM saves WHERE id=?", (sid,)).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "messages": json.loads(row["messages"]),
        "transcript": json.loads(row["transcript"]),
        "turns": row["turns"],
        "lore": json.loads(row["lore"] or "[]"),
        "character_state": json.loads(row["character_state"] or "{}"),
        "world_memory": json.loads(row["world_memory"] or "[]"),
        "world_entities": json.loads(row["world_entities"] or "{}"),
        "inventory": json.loads(row["inventory"] or "[]"),
        "director_state": json.loads(row["director_state"] or "{}"),
        "stage_summary": row["stage_summary"] or "",
        "summary_turn": int(row["summary_turn"] or 0),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def exists(sid: str) -> bool:
    with _conn() as conn:
        row = conn.execute("SELECT 1 FROM saves WHERE id=?", (sid,)).fetchone()
    return row is not None


def list_saves() -> list[dict]:
    """列出所有存档的摘要（不含完整 messages/transcript），按最近更新排序。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, name, turns, transcript, created_at, updated_at "
            "FROM saves ORDER BY updated_at DESC"
        ).fetchall()
    result = []
    for r in rows:
        transcript = json.loads(r["transcript"])
        preview = ""
        for blk in reversed(transcript):
            if blk.get("role") == "narration":
                preview = blk.get("text", "")[:60]
                break
        result.append(
            {
                "id": r["id"],
                "name": r["name"],
                "turns": r["turns"],
                "preview": preview,
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
        )
    return result


def rename(sid: str, name: str) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE saves SET name=?, updated_at=? WHERE id=?",
            (name, time.time(), sid),
        )
    return cur.rowcount > 0


def delete(sid: str) -> bool:
    with _conn() as conn:
        conn.execute("DELETE FROM save_opportunity_rewards WHERE save_id=?", (sid,))
        conn.execute("DELETE FROM save_world_time WHERE save_id=?", (sid,))
        conn.execute("DELETE FROM agent_traces WHERE save_id=?", (sid,))
        cur = conn.execute("DELETE FROM saves WHERE id=?", (sid,))
    return cur.rowcount > 0
