#!/usr/bin/env python3
# EU 热点日报：抓取 -> 去重 -> 分级 -> 渲染 index.html -> 163 SMTP 发送
import os, sys, json, re, hashlib, smtplib, datetime, pathlib
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr

try:
    import feedparser
except ModuleNotFoundError:
    feedparser = None

ROOT = pathlib.Path(__file__).resolve().parent
STATE = ROOT / "state.json"
PUBLIC = ROOT / "public"
PUBLIC.mkdir(exist_ok=True)

def load_env_file(path=ROOT / ".env"):
    # 无依赖解析 .env；不覆盖已存在的环境变量
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip("'").strip('"')
        if k and k not in os.environ:
            os.environ[k] = v

load_env_file()

def env(k, d=""):
    return os.environ.get(k, d)

TZ = env("TIMEZONE", "Europe/Brussels")
MAIL_HOST = env("MAIL_HOST", "smtp.163.com")
MAIL_PORT = int(env("MAIL_PORT", "465"))
MAIL_USER = env("MAIL_USER", "wednet800@163.com")
MAIL_PASS = env("MAIL_PASS", "")  # 163 授权码，切勿提交进 git
MAIL_FROM = env("MAIL_FROM", MAIL_USER)
MAIL_TO = [x.strip() for x in env("MAIL_TO", "hynes800@gmail.com").split(",") if x.strip()]
LOOKBACK_HOURS = int(env("LOOKBACK_HOURS", "24"))
MAX_ITEMS = int(env("MAX_ITEMS", "18"))
STATE_DAYS = int(env("STATE_DAYS", "30"))

FEEDS = {
    "EU整体": [
        "https://feeds.bbci.co.uk/news/world/europe/rss.xml",
        "https://www.politico.eu/feed/",
        "https://www.euractiv.com/feed/",
        "https://www.ecb.europa.eu/rss/press.html",
    ],
    "德国": ["https://www.tagesschau.de/xml/rss2/"],
    "法国": ["https://www.lemonde.fr/rss/une.xml"],
    "意大利": ["https://www.ansa.it/sito/notizie/economia/economia_rss.xml"],
    "西班牙": ["https://www.europapress.es/rss/rss.aspx"],
    "罗马尼亚": ["https://hotnews.ro/rss"],
    "匈牙利": ["https://telex.hu/rss"],
}

SECTOR_KEYS = {
    "政经": ["ecb","inflation","rate","gdp","budget","deficit","bond","euro","fiscal","tax","energy","oil","gas","election","government","财政","通胀","加息","利率","经济","选举"],
    "IT·AI": ["ai","artificial intelligence","gpt","llm","chip","semiconductor","data","cyber","cloud","quantum","startup","tech","regulat","garante","algorithm","digital","人工智能","芯片","数据","网络","算力","科技"],
}
A_KEYS = ["ecb","rate decision","inflation","ai act","sanction","bailout","emergency","fine","penalty","merger","acquisition","cyberattack","outage","central bank","加息","制裁","罚单","收购","网络攻击","破产"]

GRADE_NAME = {"a": "A级", "b": "B级", "c": "C级"}
GRADE_COLOR = {"a": "#c0392b", "b": "#2f6fb3", "c": "#8a8f98"}

def now_local():
    return datetime.datetime.now(ZoneInfo(TZ))

def should_run():
    # 仅在本地工作日 08:00 时段执行（配合 UTC cron 双重保险）
    n = now_local()
    return n.weekday() < 5 and n.hour == 8

def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"seen": {}}

def save_state(st):
    STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")

def prune_state(st):
    cutoff = now_local() - datetime.timedelta(days=STATE_DAYS)
    keep = {}
    for h, t in st.get("seen", {}).items():
        try:
            if datetime.datetime.fromisoformat(t) >= cutoff.replace(tzinfo=None):
                keep[h] = t
        except Exception:
            pass
    st["seen"] = keep

def pick_sector(text):
    t = text.lower()
    ai = sum(1 for k in SECTOR_KEYS["IT·AI"] if k in t)
    eco = sum(1 for k in SECTOR_KEYS["政经"] if k in t)
    if ai == 0 and eco == 0:
        return "政经"
    return "IT·AI" if ai >= eco else "政经"

def grade(item):
    t = (item["title"] + " " + item.get("summary", "")).lower()
    if any(k in t for k in A_KEYS):
        return "a"
    return "b" if item.get("summary") else "c"

def fetch():
    if feedparser is None:
        return [], ["feedparser not installed: pip install -r requirements.txt"]
    items, errors = [], []
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=LOOKBACK_HOURS)
    for country, urls in FEEDS.items():
        for u in urls:
            try:
                d = feedparser.parse(u)
                for e in d.entries[:25]:
                    title = getattr(e, "title", "")
                    link = getattr(e, "link", "")
                    summary = re.sub("<[^>]+>", "", getattr(e, "summary", "") or "")[:400]
                    if not title or not link:
                        continue
                    ts = None
                    for attr in ("published_parsed", "updated_parsed"):
                        v = getattr(e, attr, None)
                        if v:
                            ts = datetime.datetime(*v[:6], tzinfo=datetime.timezone.utc)
                            break
                    if ts and ts < cutoff:
                        continue
                    h = hashlib.sha1(link.encode()).hexdigest()[:16]
                    items.append({"id": h, "country": country, "title": title.strip(),
                                  "link": link, "summary": summary, "time": ts.isoformat() if ts else "",
                                  "sector": pick_sector(title + " " + summary)})
            except Exception as ex:
                errors.append(f"{u}: {ex}")
    for it in items:
        it["grade"] = grade(it)
    order = {"a": 0, "b": 1, "c": 2}
    items.sort(key=lambda x: (order[x["grade"]], x["country"], x["time"]))
    return items[:MAX_ITEMS], errors

def render(items, errors):
    date = now_local().strftime("%Y-%m-%d")
    cards = []
    for it in items:
        g = it["grade"]
        cards.append(f"""<div class="card {g}">
  <div class="row1"><span class="grade" style="background:{GRADE_COLOR[g]}">{GRADE_NAME[g]}</span>
  <span class="tag country">{it["country"]}</span><span class="tag">{it["sector"]}</span></div>
  <div class="tt"><a href="{it["link"]}">{it["title"]}</a></div>
  <div class="sum">{it.get("summary","")}</div>
  <div class="src">{it["country"]}信源 · 置信度 待人工复核 ｜ 追踪 观察</div>
</div>""")
    err_html = f'<div class="sum">抓取异常源 {len(errors)} 个（详见日志）。</div>' if errors else ""
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>EU 热点日报 · {date}</title>
<style>
body{{margin:0;background:#f2f4f7;color:#1a1d23;font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;}}
a{{color:#0b3d91;}}
.wrap{{max-width:760px;margin:0 auto;padding:24px 14px 40px;}}
h1{{font-size:22px;margin:0 0 4px;}} .m{{font-size:12px;color:#6b7280;}}
.card{{background:#fff;border:1px solid #e2e6ec;border-radius:10px;padding:12px 14px;margin-bottom:10px;}}
.card.a{{border-left:4px solid {GRADE_COLOR["a"]};}} .card.b{{border-left:4px solid {GRADE_COLOR["b"]};}} .card.c{{border-left:4px solid {GRADE_COLOR["c"]};}}
.row1{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:4px;}}
.grade{{font-size:11px;border-radius:4px;padding:1px 8px;color:#fff;}}
.tag{{font-size:11px;border-radius:6px;padding:2px 8px;color:#4b5563;background:#eef1f5;}} .tag.country{{color:#111827;background:#e3e8ef;}}
.tt{{font-size:15px;font-weight:600;line-height:1.5;}} .tt a{{color:#1a1d23;text-decoration:none;}}
.sum{{font-size:14px;line-height:1.75;color:#374151;margin-top:4px;}}
.src{{font-size:12px;color:#6b7280;margin-top:6px;}}
.foot{{margin-top:18px;font-size:12px;color:#9aa1ab;line-height:1.7;border-top:1px solid #e2e6ec;padding-top:12px;}}
</style></head><body><div class="wrap">
<h1>EU 热点日报</h1>
<div class="m">{date} · {TZ} · 覆盖 EU整体 + 德/法/意/西/罗/匈 · 政经 · IT · AI 高科技 · 本期 {len(items)} 条</div>
{err_html}
{"".join(cards)}
<div class="foot">自动抓取生成，信源窗口近 {LOOKBACK_HOURS} 小时；内容为公开信息摘要，不构成投资或法律建议，重要事项请核对原文。</div>
</div></body></html>"""


# 干跑测试用样例（--dry-run 时不访问网络）
SAMPLE_ITEMS = [
    {"id":"smp01","country":"EU整体","sector":"政经","grade":"a",
     "title":"[SAMPLE] ECB 9 月加息 25bp 纪要公布，存款利率 2.50%",
     "link":"https://www.ecb.europa.eu/","summary":"管委会就通胀上行风险达成加息共识，10-29 议息成焦点。","time":""},
    {"id":"smp02","country":"意大利","sector":"IT·AI","grade":"a",
     "title":"[SAMPLE] 意大利 AI 法 12 个月授权届满，Garante 像素新规 10-28 截止",
     "link":"https://www.whitecase.com/","summary":"Legge 132/2025 实施条例窗口开启；Provision No.284 将邮件像素视同 cookie 需事先同意。","time":""},
    {"id":"smp03","country":"罗马尼亚","sector":"政经","grade":"b",
     "title":"[SAMPLE] BNR 维持政策利率 6.5% 不变",
     "link":"https://www.romania-insider.com/","summary":"央行在非常高不确定性下按兵不动，分析师预计长期维持。","time":""},
    {"id":"smp04","country":"匈牙利","sector":"政经","grade":"b",
     "title":"[SAMPLE] 匈牙利 Q2 GDP +1.7%，欧盟解冻资金进入执行期",
     "link":"https://tradingeconomics.com/hungary/news","summary":"服务业驱动复苏，欧委会预测 2026 全年 +1.8%。","time":""},
]

def build_email(html):
    msg = MIMEText(html, "html", "utf-8")
    msg["From"] = formataddr((str(Header("EU Daily Bot", "utf-8")), MAIL_FROM))
    msg["To"] = ", ".join(MAIL_TO)
    msg["Subject"] = Header(f"【EU热点】{now_local().strftime('%Y-%m-%d')} · 自动版", "utf-8")
    return msg

def send_email(html):
    if not MAIL_PASS:
        print("MAIL_PASS 未设置，跳过发送。")
        return False
    msg = build_email(html)
    with smtplib.SMTP_SSL(MAIL_HOST, MAIL_PORT, timeout=30) as s:
        s.login(MAIL_USER, MAIL_PASS)
        s.sendmail(MAIL_FROM, MAIL_TO, msg.as_string())
    return True

def main():
    if "--dry-run" in sys.argv:
        html = render(SAMPLE_ITEMS, ["dry-run: no network in sandbox"])
        (PUBLIC / "index.html").write_text(html, encoding="utf-8")
        msg = build_email(html)
        raw = msg.as_string()
        print("[dry-run] index.html 已生成：", len(html), "chars")
        print("[dry-run] From:", msg["From"])
        print("[dry-run] To:", msg["To"])
        print("[dry-run] Subject:", msg["Subject"])
        print("[dry-run] SMTP 目标:", MAIL_HOST + ":" + str(MAIL_PORT), "user:", MAIL_USER)
        print("[dry-run] 邮件体积:", len(raw.encode("utf-8")), "bytes —— 未连接 SMTP，未发送")
        return
    if "--check-time" in sys.argv and not should_run():
        print("非 Brussels 工作日 08:00 时段，退出。")
        return
    st = load_state(); prune_state(st)
    items, errors = fetch()
    fresh = [it for it in items if it["id"] not in st["seen"]]
    for it in fresh:
        st["seen"][it["id"]] = now_local().replace(tzinfo=None).isoformat()
    save_state(st)
    html = render(fresh or items, errors)
    (PUBLIC / "index.html").write_text(html, encoding="utf-8")
    print(f"已生成 public/index.html：{len(fresh)} 条新条目（去重后），异常源 {len(errors)}。")
    for e in errors[:5]:
        print("WARN:", e)
    if "--send" in sys.argv:
        if not MAIL_PASS:
            print("ERROR: MAIL_PASS 为空——请确认 .env 文件存在且已填入 163 授权码，或用 export MAIL_PASS=... 注入。")
            sys.exit(2)
        ok = send_email(html)
        if ok:
            print("邮件已发送。")
        else:
            sys.exit(3)

if __name__ == "__main__":
    main()
