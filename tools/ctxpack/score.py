# -*- coding: utf-8 -*-
r"""
score.py — 记忆三维权重打分器（原型）

设计来自 2026-11-08 的讨论：
  三个维度：用户权重 / 人格权重 / 情感偏好权重（人格权重用来防"舔狗化"）
  三档归档：>=80 永存（L1/L2）；50-80 关键词召回（L3）；<50 冷却后可清理
  不让大模型逐条打分：规则引擎先跑，命中词与命中理由都留痕，便于复核与调参

用法：
  python score.py            # 全量重建 memory.db
  python score.py --stats    # 只看统计
  python score.py --top 20   # 看总分最高的 N 条

环境变量：SYSPERSONA_ROOT 指记忆库根（旧名 PERSONA_ROOT 也认），重建的是
`<根>\memory.db`；不设就按脚本所在目录推导，推不出来直接抛错。
"""

import argparse
import glob
import io
import os
import re
import sqlite3
import time

from persona_root import root as _persona_root

ROOT = _persona_root()
DB = os.path.join(ROOT, "memory.db")

# ---------- 词表 ----------
DECIDE = ("决定", "不要再", "不要", "必须", "优先", "以后都", "记住", "定下来", "就这样",
          "不做了", "改用", "换成", "禁止", "一定要")
PERSONA = ("人格", "身份", "边界", "底线", "kernel", "persona", "我是谁", "原则",
           "性格", "设定", "神格", "灵魂")
BOUNDARY = ("不能", "不允许", "不许", "禁止", "不得", "绝不")
VALUE = ("意义", "值得", "应该", "正确", "价值观", "该不该", "对不对")
LONGTERM = ("一直", "永远", "以后", "长期", "终身", "一辈子")
PREF = ("我喜欢", "我讨厌", "我受不了", "我就想要", "我最怕", "我烦", "我爱")
EMO = ("烦", "气", "爽", "喜欢", "讨厌", "怕", "兴奋", "崩溃", "离谱", "舒服", "难受",
       "哈哈", "唉", "靠", "真香", "绝了", "要命")
# 捧场话术：人格权重本该防"舔狗化"，这些词绝不能被当成"人格浓度"
FLATTER = ("太对了", "绝了", "绝杀", "完美", "极其", "漂亮", "满分", "犀利", "深刻",
           "正确", "牛逼", "封神", "超神", "降维打击", "精准", "到位", "顶级", "硬核")
STRONG_FP = ("我要", "我想", "我不", "我得", "我觉", "我的")

W_USER, W_PERSONA, W_EMO = 0.40, 0.35, 0.25


def clip(n, lo, hi):
    return max(lo, min(hi, n))


def score_user(role, text):
    """用户权重：是不是用户亲口说的、说得有多具体、有没有下决心。"""
    s, why = 0, []
    if role == "用户":
        s += 35; why.append("用户亲口+35")
    else:
        s += 5
    s += clip(len(text) // 60, 0, 10)
    if len(text) > 80:
        why.append("篇幅长+%d" % clip(len(text) // 40, 0, 20))
    hit = [k for k in DECIDE if k in text]
    if hit:
        s += 15; why.append("决定词(%s)+15" % hit[0])
    fp = [k for k in STRONG_FP if k in text]
    if fp:
        s += 10; why.append("第一人称(%s)+10" % fp[0])
    if re.search(r"\d", text) and re.search(r"GB|MB|KB|版|条|分钟|天|小时|v\d", text):
        s += 10; why.append("具体数字+10")
    if text.strip().endswith(("？", "?")):
        s -= 5; why.append("提问-5")
    return clip(s, 0, 100), ";".join(why)


def score_persona(role, text):
    """人格权重：涉不涉及"它是谁"、有没有碰到边界。防舔狗化就靠这一维。"""
    s, why = 0, []
    hit = [k for k in PERSONA if k.lower() in text.lower()]
    if hit:
        s += min(50, 25 * len(hit)); why.append("人格词(%s)+%d" % ("/".join(hit[:2]), min(50, 25 * len(hit))))
    b = [k for k in BOUNDARY if k in text]
    if b:
        s += 20; why.append("边界词(%s)+20" % b[0])
    v = [k for k in VALUE if k in text]
    if v:
        s += 15; why.append("价值判断(%s)+15" % v[0])
    lg = [k for k in LONGTERM if k in text]
    if lg:
        s += 10; why.append("长期倾向(%s)+10" % lg[0])
    if role == "助手" and ("你" in text and "我" in text):
        s += 10; why.append("助手回应主体+10")
    # 防舔狗化：助手如果只是在捧，不算"人格浓度"，反而扣分
    if role == "助手":
        f = [k for k in FLATTER if k in text]
        if len(f) >= 2:
            pen = min(40, 12 * len(f))
            s -= pen
            why.append("捧场扣分-%d(%s)" % (pen, "/".join(f[:3])))
    return clip(s, 0, 100), ";".join(why)


def score_emotion(role, text):
    """情感权重：情绪浓度与个人偏好。"""
    s, why = 0, []
    hit = [k for k in EMO if k in text]
    if hit:
        s += min(45, 15 * len(hit)); why.append("情绪词(%s)+%d" % ("/".join(hit[:2]), min(45, 15 * len(hit))))
    p = [k for k in PREF if k in text]
    if p:
        s += 20; why.append("偏好表达(%s)+20" % p[0])
    bang = text.count("！") + text.count("!")
    if bang:
        s += min(15, 5 * bang); why.append("感叹+%d" % min(15, 5 * bang))
    mark = text.count("？") + text.count("?")
    if mark >= 2:
        s += 5; why.append("连环追问+5")
    if role == "助手":
        f = [k for k in FLATTER if k in text]
        if len(f) >= 2:
            pen = min(30, 10 * len(f))
            s -= pen
            why.append("捧场扣分-%d" % pen)
    return clip(s, 0, 100), ";".join(why)


def bucket(total):
    if total >= 80:
        return "L1-永存"
    if total >= 50:
        return "L3-召回"
    return "冷却"


def parse(path):
    text = io.open(path, encoding="utf-8", errors="replace").read()
    parts = text.split("\n---\n", 1)
    body = parts[1] if len(parts) > 1 else text
    out = []
    for chunk in re.split(r"\n\n---\n\n", body):
        m = re.match(r"##\s*(用户|助手)\s*\n(.*)", chunk, re.S)
        if not m:
            continue
        role = m.group(1)
        raw = m.group(2).strip()
        clean = re.sub(r"(?m)^>\s?.*\n?", "", raw).strip()   # 去掉思考块
        if len(clean) < 4:
            continue
        out.append((role, raw, clean))
    return out


def bigrams(text):
    """取字面二元组：中文按滑窗切，英文/数字整词。用来衡量"说法是否跨会话复现"。"""
    s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", " ", text)
    out = set()
    for seg in s.split():
        if not seg:
            continue
        if re.match(r"^[A-Za-z0-9]", seg):
            if len(seg) >= 3:
                out.add(seg.lower())
        else:
            for i in range(len(seg) - 1):
                g = seg[i:i + 2]
                if g[0] in "的了是在有和与或就都也还很太我你他它这那什么怎么" and g[1] in "的了是在有和与或":
                    continue
                out.add(g)
    return out


def build_df(files):
    """每个二元组出现在多少个会话里（跨会话复现率的底座）。"""
    df = {}
    for p in files:
        try:
            t = io.open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for g in bigrams(t):
            df[g] = df.get(g, 0) + 1
    return df


def score_recurrence(text, df):
    """复现率：这条消息里有多少说法是"在别的会话也出现过"的。
    只出现一次的当噪声，铺满全库的当口头禅，两头都不算。"""
    bs = bigrams(text)
    if not bs:
        return 0, ""
    shared = [g for g in bs if 2 <= df.get(g, 0) <= 30]
    ratio = len(shared) / float(len(bs))
    # 短消息只有一两个二元组，命中就是 100%，没有信息量——按长度打折
    ratio *= min(1.0, len(bs) / 12.0)
    s = int(round(100 * ratio))
    why = "复现率%d%%(%d/%d 个说法跨会话出现)" % (s, len(shared), len(bs))
    return clip(s, 0, 100), why


def build():
    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session TEXT, day TEXT, source TEXT, role TEXT,
        user_w INTEGER, persona_w INTEGER, emotion_w INTEGER, recurrence INTEGER,
        total REAL, bucket TEXT, text TEXT, why TEXT, created REAL)""")
    con.execute("CREATE INDEX idx_total ON memory(total DESC)")
    con.execute("CREATE INDEX idx_rec ON memory(recurrence DESC)")

    files = [p for p in glob.glob(os.path.join(ROOT, "sessions", "**", "*.md"), recursive=True)
             if "HANDOFF" not in os.path.basename(p).upper()]
    print("建复现率索引…（%d 个会话）" % len(files))
    df = build_df(files)
    print("二元组词表 %d，其中跨会话出现过 2 次以上的 %d" %
          (len(df), sum(1 for v in df.values() if v >= 2)))
    n = 0
    for p in files:
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        name = os.path.basename(p)
        m = re.search(r"sessions/(\d{4}-\d{2}-\d{2})/([^/]+)/", rel)
        day, src = (m.group(1), m.group(2)) if m else ("", "")
        for role, raw, clean in parse(p):
            u, wu = score_user(role, clean)
            pe, wp = score_persona(role, clean)
            e, we = score_emotion(role, clean)
            rc, wr = score_recurrence(clean, df)
            total = round(0.35 * u + 0.28 * pe + 0.17 * e + 0.20 * rc, 1)
            if role == "助手":
                total = round(total * 0.75, 1)   # 助手的解释是派生信息，不该压过用户原话
            con.execute("INSERT INTO memory (session,day,source,role,user_w,persona_w,emotion_w,recurrence,total,bucket,text,why,created)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (name, day, src, role, u, pe, e, rc, total, bucket(total),
                         clean[:600], "user:" + wu + " | persona:" + wp + " | emo:" + we + " | " + wr, time.time()))
            n += 1
    con.commit()
    recalibrate(con)
    return con, n


def recalibrate(con):
    """设计里的 80/50 是绝对阈值；实测总分上不去 50。
    改成按分位标定：前 5% 永存、5–25% 召回、其余冷却。阈值会打印出来，可回填文档。"""
    vals = [r[0] for r in con.execute("SELECT total FROM memory ORDER BY total")]
    if not vals:
        return
    def q(p):
        return vals[min(len(vals) - 1, int(len(vals) * p))]
    hi, mid = q(0.95), q(0.75)
    con.execute("UPDATE memory SET bucket = CASE WHEN total >= ? THEN 'L1-永存'"
                " WHEN total >= ? THEN 'L3-召回' ELSE '冷却' END", (hi, mid))
    # 记忆该记的是"你说过什么"，不是"AI 怎么复述你说过什么"：
    # 助手消息最多进 L3，永不进 L1
    con.execute("UPDATE memory SET bucket = 'L3-召回' WHERE role = '助手' AND bucket = 'L1-永存'")
    con.commit()
    print("分位标定：L1 >= %.1f（前 5%%）　L3 >= %.1f（前 25%%）　（助手不进 L1）" % (hi, mid))


def stats(con):
    total = con.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    print("消息总数", total)
    print("\n分档：")
    for b, c in con.execute("SELECT bucket, COUNT(*) FROM memory GROUP BY bucket ORDER BY COUNT(*) DESC"):
        print("  %-8s %5d  (%.1f%%)" % (b, c, 100.0 * c / total))
    print("\n维度均值：")
    for col in ("user_w", "persona_w", "emotion_w", "total"):
        v = con.execute("SELECT AVG(%s), MIN(%s), MAX(%s) FROM memory" % (col, col, col)).fetchone()
        print("  %-10s 均值 %5.1f  最小 %5.1f  最大 %5.1f" % (col, v[0], v[1], v[2]))
    print("\n角色分布：")
    for r, c in con.execute("SELECT role, COUNT(*) FROM memory GROUP BY role"):
        print("  %s %d" % (r, c))


def top(con, k):
    rows = con.execute("SELECT total,bucket,role,substr(text,1,90),why FROM memory ORDER BY total DESC LIMIT ?", (k,))
    for i, (t, b, r, txt, why) in enumerate(rows, 1):
        print("%2d) %5.1f %-8s %s  %s" % (i, t, b, txt.replace("\n", " "), ""))
        print("     理由: %s" % why[:160])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--top", type=int, default=0)
    a = ap.parse_args()
    if a.stats and os.path.exists(DB):
        con = sqlite3.connect(DB)
    else:
        con, n = build()
        print("已重建 %s，写入 %d 条消息" % (DB, n))
    if a.stats or not a.top:
        stats(con)
    if a.top:
        print()
        top(con, a.top)
