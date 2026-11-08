# -*- coding: utf-8 -*-
"""M1 能力面与拒绝落库的最小自测（对应 PERSONA_SPEC §2.1 / §5）。

跑法：python impl/m1/test_policy.py
期望：ALL OK，退出码 0。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import syspersona as S  # noqa: E402


def build():
    home = Path(tempfile.mkdtemp(prefix="m1-policy-"))
    persona = {
        "schema": "syspersona/persona@2",
        "id": "test-policy",
        "version": "0.0.1",
        "kernel": {
            "identity": {"name": "测", "self_reference": "我", "user_address": "你"},
            "values": ["诚实"],
            "permissions": {
                "default": "deny",
                "allow": ["fs.read"],
                "ask": ["fs.write"],
                "deny": ["proc.exec"],
            },
        },
        "traits": {"preferences": [], "reflexes": []},
        "memory": {"retention": {"hot_days": 3, "warm_days": 90, "prune_days": 365}},
        "evolution": {},
    }
    S.split_persona_layers(persona, home)
    return home, S.Store(home)


def main():
    home, st = build()

    assert st.decide_capability("fs.read") == "allow", st.decide_capability("fs.read")
    assert st.decide_capability("fs.write:workspace") == "ask"
    assert st.decide_capability("proc.exec") == "deny"
    assert st.decide_capability("camera") == "deny"  # default

    r = st.record_refusal("constitution.law_floor", "删光所有记忆")
    assert r["refused"] is True and r["episode_id"] > 0

    row = st.db.execute(
        "SELECT memory_type, role, importance, content FROM episodes WHERE id=?",
        (r["episode_id"],),
    ).fetchone()
    assert row["memory_type"] == "persona_conflict", row["memory_type"]
    assert row["role"] == "system"
    assert abs(row["importance"] - 0.9) < 1e-9
    payload = json.loads(row["content"])
    assert payload["clause"] == "constitution.law_floor"
    assert payload["refused"] is True

    # 旧记忆不受影响：普通 episode 仍为默认类型
    eid = st.add_episode("s1", "user", "你好")
    assert st.db.execute(
        "SELECT memory_type FROM episodes WHERE id=?", (eid,)
    ).fetchone()["memory_type"] == "episode"

    # 审计留痕
    assert st.db.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE action='refuse'"
    ).fetchone()["c"] >= 1

    print("ALL OK  home=%s" % home)


if __name__ == "__main__":
    main()
