#!/usr/bin/env python3
# EU 热点日报：抓取 -> 去重 -> 分级 -> 每期存档 -> 渲染首页(日历+历史) -> 163 SMTP 发送
import os, sys, json, re, calendar, hashlib, smtplib, datetime, pathlib, urllib.request
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
ARCHIVE = PUBLIC / "archive"
ARCHIVE.mkdir(parents=True, exist_ok=True)

def load_env_file(path=ROOT / ".env"):
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
MAIL_PASS = env("MAIL_PASS", "")
MAIL_FROM = env("MAIL_FROM", MAIL_USER)
MAIL_TO = [x.strip() for x in env("MAIL_TO", "hynes800@gmail.com").split(",") if x.strip()]
LOOKBACK_HOURS = int(env("LOOKBACK_HOURS", "24"))
MAX_ITEMS = int(env("MAX_ITEMS", "18"))
STATE_DAYS = int(env("STATE_DAYS", "30"))
REPO = env("GITHUB_REPOSITORY", "")
LLM_API_KEY = env("LLM_API_KEY", "")
LLM_BASE_URL = env("LLM_BASE_URL", "")   # OpenAI 兼容接口，如 https://api.deepseek.com/v1
LLM_MODEL = env("LLM_MODEL", "")         # 如 deepseek-chat / qwen-plus / gpt-4o-mini          # Actions 自动注入，如 hynes800/eu-daily
CAL_MONTHS = int(env("CAL_MONTHS", "3"))     # 日历显示最近几个月
SITE_TITLE = "EU 热点日报"

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


def llm_translate(items):
    """调用 OpenAI 兼容接口：标题译中文、摘要改写中文、校准领域与置信度。
    未配置 LLM_API_KEY 或调用失败时，原样返回（不影响发送）。"""
    if not LLM_API_KEY or not LLM_BASE_URL or not LLM_MODEL or not items:
        return items
    batch = items[:20]
    payload_items = [{"i": n, "title": it["title"], "summary": it.get("summary", "")[:300],
                      "country": it["country"], "sector": it["sector"]}
                     for n, it in enumerate(batch)]
    sys_prompt = ("你是欧洲新闻编辑。把给定条目的标题翻译成简洁中文，把摘要改写为50-80字中文摘要；"
                  "保持事实不变，不添加原文没有的信息；校准领域（只能返回 政经 或 IT·AI）；"
                  "评估置信度（高/中/低，依据来源权威性）。只输出JSON，不要输出其他内容。")
    user = json.dumps({"items": payload_items}, ensure_ascii=False)
    body = {"model": LLM_MODEL, "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": sys_prompt},
                          {"role": "user", "content": user}]}
    try:
        req = urllib.request.Request(
            LLM_BASE_URL.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + LLM_API_KEY},
            method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        result = json.loads(content)
        for r in result.get("items", []):
            try:
                it = batch[int(r.get("i"))]
            except Exception:
                continue
            if r.get("title"):
                it["title"] = str(r["title"]).strip()
            if r.get("summary"):
                it["summary"] = str(r["summary"]).strip()
            if r.get("sector") in ("政经", "IT·AI"):
                it["sector"] = r["sector"]
            if r.get("conf") in ("高", "中", "低"):
                it["conf"] = r["conf"]
            it["tr"] = "持续"
    except Exception as ex:
        print("LLM translate failed, fallback to original:", ex)
    return items

# ---------- 渲染 ----------
BASE_CSS = """
body{margin:0;background:#f2f4f7;color:#1a1d23;font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;}
a{color:#0b3d91;}
.wrap{max-width:1020px;margin:0 auto;padding:24px 14px 40px;}
h1{font-size:22px;margin:0 0 4px;} .m{font-size:12px;color:#6b7280;}
h2{font-size:16px;margin:22px 0 10px;border-left:4px solid #0b3d91;padding-left:8px;}
.card{background:#fff;border:1px solid #e2e6ec;border-radius:10px;padding:12px 14px;margin-bottom:10px;}
.card.a{border-left:4px solid #c0392b;} .card.b{border-left:4px solid #2f6fb3;} .card.c{border-left:4px solid #c0c4cc;}
.row1{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:4px;}
.grade{font-size:11px;border-radius:4px;padding:1px 8px;color:#fff;}
.tag{font-size:11px;border-radius:6px;padding:2px 8px;color:#4b5563;background:#eef1f5;} .tag.country{color:#111827;background:#e3e8ef;}
.tt{font-size:15px;font-weight:600;line-height:1.5;} .tt a{color:#1a1d23;text-decoration:none;}
.sum{font-size:14px;line-height:1.75;color:#374151;margin-top:4px;}
.src{font-size:12px;color:#6b7280;margin-top:6px;}
.foot{margin-top:18px;font-size:12px;color:#9aa1ab;line-height:1.7;border-top:1px solid #e2e6ec;padding-top:12px;}
/* 左侧日历栏 */
.layout{display:flex;gap:18px;align-items:flex-start;}
.side{width:252px;flex:none;position:sticky;top:12px;}
.side.hidden .calbox{display:none;}
.calctl{display:flex;gap:6px;align-items:center;margin-bottom:8px;}
.calctl select,.calctl button{border:1px solid #d5dae2;border-radius:8px;background:#fff;padding:4px 8px;font-size:13px;color:#1a1d23;cursor:pointer;}
#calToggle{width:100%;margin-bottom:8px;border:1px solid #d5dae2;border-radius:8px;background:#fff;padding:6px;font-size:13px;color:#1a1d23;cursor:pointer;}
#calToggle:hover,.calctl button:hover{background:#f0f4fa;}
#calTbl{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e2e6ec;border-radius:10px;font-size:12px;overflow:hidden;}
#calTbl th{color:#9aa1ab;font-weight:400;padding:5px 2px;}
#calTbl td{padding:3px 2px;text-align:center;}
#calTbl td .has a{display:inline-block;min-width:24px;padding:3px 4px;border-radius:6px;background:#eaf1fb;color:#0b3d91;text-decoration:none;font-weight:600;}
#calTbl td.today .has a{outline:2px solid #0b3d91;}
#calTbl td .dot{display:inline-block;min-width:24px;padding:3px 4px;color:#9aa1ab;}
.maincol{flex:1;min-width:0;}
@media (max-width:860px){.layout{flex-direction:column;}.side{width:100%;position:static;}}
/* 历史 */
.hist{background:#fff;border:1px solid #e2e6ec;border-radius:10px;padding:6px 14px;margin-bottom:14px;}
.hist a{display:block;padding:9px 0;border-bottom:1px solid #eef1f5;text-decoration:none;color:#1a1d23;font-size:14px;}
.hist a:last-child{border-bottom:none;}
.hist .dt{color:#0b3d91;font-weight:600;margin-right:8px;font-variant-numeric:tabular-nums;}
.hist .hl{color:#6b7280;font-size:13px;}
.badge{font-size:11px;color:#6b7280;font-weight:400;border:none;padding:0;}
"""

def cards_html(items):
    out = []
    for it in items:
        g = it["grade"]
        srcs = " ｜ ".join(f'<a href="{u}">{n}</a>' for n, u in it.get("src", []))
        src_line = f"{srcs} ｜ " if srcs else ""
        out.append(f"""<div class="card {g}">
  <div class="row1"><span class="grade" style="background:{GRADE_COLOR[g]}">{GRADE_NAME[g]}</span>
  <span class="tag country">{it["country"]}</span><span class="tag">{it["sector"]}</span></div>
  <div class="tt"><a href="{it["link"]}">{it["title"]}</a></div>
  <div class="sum">{it.get("summary","")}</div>
  <div class="src">{src_line}置信度 {it.get("conf","待复核")} ｜ 追踪 {it.get("tr","观察")}</div>
</div>""")
    return "".join(out)

def foot_html(extra=""):
    return f"""<div class="foot">自动抓取生成，信源窗口近 {LOOKBACK_HOURS} 小时；内容为公开信息摘要，不构成投资或法律建议，重要事项请核对原文。<br>{extra}</div>"""

def render_issue(items, errors, date_str):
    err_html = f'<div class="sum">抓取异常源 {len(errors)} 个（详见日志）。</div>' if errors else ""
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{SITE_TITLE} · {date_str} 期</title>
<style>{BASE_CSS}</style></head><body><div class="wrap">
<h1>{SITE_TITLE}</h1>
<div class="m">{date_str} 期 · <a href="../index.html">返回首页/日历</a> · 覆盖 EU整体 + 德/法/意/西/罗/匈 · 政经 · IT · AI 高科技 · 本期 {len(items)} 条</div>
{err_html}
{cards_html(items)}
{foot_html()}
</div></body></html>"""

SCRIPT = '\n<script>\nvar ISSUE_DATES = new Set(__DATES__);\nvar TODAY = "__TODAY__";\nvar curY = parseInt(TODAY.slice(0,4),10), curM = parseInt(TODAY.slice(5,7),10);\nfunction pad(n){return (n<10?"0":"")+n;}\nfunction ds(y,m,d){return y+"-"+pad(m)+"-"+pad(d);}\nfunction fillSel(){\n  var ys=document.getElementById("selY"), ms=document.getElementById("selM");\n  var minY=curY-3, maxY=curY+1;\n  ISSUE_DATES.forEach(function(s){var y=parseInt(s.slice(0,4),10); if(y<minY)minY=y;});\n  ys.innerHTML=""; for(var y=maxY;y>=minY;y--){var o=document.createElement("option");o.value=y;o.textContent=y+"\\u5e74";if(y===curY)o.selected=true;ys.appendChild(o);}\n  ms.innerHTML=""; for(var m=1;m<=12;m++){var o=document.createElement("option");o.value=m;o.textContent=m+"\\u6708";if(m===curM)o.selected=true;ms.appendChild(o);}\n}\nfunction renderCal(){\n  var first=new Date(curY,curM-1,1);\n  var offset=(first.getDay()+6)%7;\n  var days=new Date(curY,curM,0).getDate();\n  var html="<tr><th>\\u4e00</th><th>\\u4e8c</th><th>\\u4e09</th><th>\\u56db</th><th>\\u4e94</th><th>\\u516d</th><th>\\u65e5</th></tr><tr>";\n  for(var i=0;i<offset;i++) html+="<td></td>";\n  for(var d=1;d<=days;d++){\n    var s=ds(curY,curM,d);\n    if(s===TODAY){ html+=\'<td class="today"><span class="has"><a href="archive/\'+s+\'.html">\'+d+\'</a></span></td>\'; }\n    else if(ISSUE_DATES.has(s)){ html+=\'<td><span class="has"><a href="archive/\'+s+\'.html">\'+d+\'</a></span></td>\'; }\n    else { html+=\'<td><span class="dot">\'+d+\'</span></td>\'; }\n    if((offset+d)%7===0) html+="</tr><tr>";\n  }\n  html+="</tr>";\n  document.getElementById("calTbl").innerHTML=html;\n  fillSel();\n}\nfunction shiftMonth(n){ curM+=n; if(curM<1){curM=12;curY--;} if(curM>12){curM=1;curY++;} renderCal(); }\ndocument.getElementById("prevM").onclick=function(){shiftMonth(-1);};\ndocument.getElementById("nextM").onclick=function(){shiftMonth(1);};\ndocument.getElementById("selY").onchange=function(){curY=parseInt(this.value,10);renderCal();};\ndocument.getElementById("selM").onchange=function(){curM=parseInt(this.value,10);renderCal();};\nvar side=document.getElementById("side"), tgl=document.getElementById("calToggle");\nfunction applyHidden(h){ side.className="side"+(h?" hidden":""); tgl.textContent=h?"\\u5c55\\u5f00\\u65e5\\u5386":"\\u6536\\u8d77\\u65e5\\u5386"; }\napplyHidden(localStorage.getItem("euCalHidden")==="1");\ntgl.onclick=function(){ var h=side.className.indexOf("hidden")===-1; applyHidden(h); localStorage.setItem("euCalHidden",h?"1":"0"); };\nrenderCal();\n</script>'


def render_index(items, errors, issues, today_str):
    dates = {i["date"] for i in issues}
    hist_rows = "".join(
        f'<a href="{i["url"]}"><span class="dt">{i["date"]}</span><span class="hl">{i.get("title","")}</span></a>'
        for i in sorted(issues, key=lambda x: x["date"], reverse=True)[:60]
    )
    if REPO:
        backup = (f'<h2>数据备份</h2><div class="hist">'
                  f'<a href="manifest.json">manifest.json（各期索引，JSON）</a>'
                  f'<a href="https://github.com/{REPO}/archive/refs/heads/gh-pages.zip">下载全部历史存档（gh-pages 分支 zip）</a>'
                  f"</div>")
    else:
        backup = (f'<h2>数据备份</h2><div class="hist">'
                  f'<a href="manifest.json">manifest.json（各期索引，JSON）</a>'
                  f"</div>")
    err_html = f'<div class="sum">抓取异常源 {len(errors)} 个（详见日志）。</div>' if errors else ""
    dates_js = json.dumps(sorted(dates), ensure_ascii=False)
    script = SCRIPT.replace("__DATES__", dates_js).replace("__TODAY__", today_str)
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{SITE_TITLE} · {today_str}</title>
<style>{BASE_CSS}</style></head><body><div class="wrap">
<h1>{SITE_TITLE}</h1>
<div class="m">{today_str} · Europe/Brussels · 覆盖 EU整体 + 德/法/意/西/罗/匈 · 政经 · IT · AI 高科技 · 本期 {len(items)} 条 · <a href="archive/{today_str}.html">本期单页</a></div>
{err_html}
<div class="layout">
<div class="side" id="side">
<button id="calToggle">收起日历</button>
<div class="calbox">
<div class="calctl"><button id="prevM">&lsaquo;</button><select id="selY"></select><select id="selM"></select><button id="nextM">&rsaquo;</button></div>
<table id="calTbl"></table>
</div>
</div>
<div class="maincol">
{cards_html(items)}
<h2>历史简报</h2>
<div class="hist">{hist_rows or '<span class="hl">暂无历史存档</span>'}</div>
{backup}
{foot_html()}
</div>
</div>
{script}
</body></html>"""

# ---------- 存档 / manifest ----------
def list_issues():
    issues = []
    for f in sorted(ARCHIVE.glob("*.html")):
        issues.append({"date": f.stem, "url": f"archive/{f.name}", "title": ""})
    return issues

def update_manifest(issues):
    manifest = {"site": SITE_TITLE, "updated": now_local().isoformat(), "count": len(issues),
                "issues": [{"date": i["date"], "url": i["url"], "title": i.get("title", "")} for i in issues]}
    (PUBLIC / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------- 邮件 ----------
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

SAMPLE_ITEMS = [
    {"id":"smp01","country":"EU整体","sector":"政经","grade":"a","title":"[SAMPLE] ECB 9 月加息 25bp 纪要公布，存款利率 2.50%","link":"https://www.ecb.europa.eu/","summary":"管委会就通胀上行风险达成加息共识，10-29 议息成焦点。","conf":"高","tr":"持续"},
    {"id":"smp02","country":"意大利","sector":"IT·AI","grade":"a","title":"[SAMPLE] 意大利 AI 法 12 个月授权届满，Garante 像素新规 10-28 截止","link":"https://www.whitecase.com/","summary":"Legge 132/2025 实施条例窗口开启；Provision No.284 将邮件像素视同 cookie 需事先同意。","conf":"高","tr":"持续"},
    {"id":"smp03","country":"罗马尼亚","sector":"政经","grade":"b","title":"[SAMPLE] BNR 维持政策利率 6.5% 不变","link":"https://www.romania-insider.com/","summary":"央行在非常高不确定性下按兵不动。","conf":"高","tr":"观察"},
]

def main():
    if "--dry-run" in sys.argv:
        date_str = now_local().strftime("%Y-%m-%d")
        (ARCHIVE / f"{date_str}.html").write_text(render_issue(SAMPLE_ITEMS, ["dry-run"], date_str), encoding="utf-8")
        issues = list_issues()
        for i in issues:
            if not i["title"]:
                i["title"] = SAMPLE_ITEMS[0]["title"]
        update_manifest(issues)
        html = render_index(SAMPLE_ITEMS, [], issues, date_str)
        (PUBLIC / "index.html").write_text(html, encoding="utf-8")
        msg = build_email(html)
        print("[dry-run] index.html + archive/%s.html + manifest.json 已生成" % date_str)
        print("[dry-run] From:", msg["From"])
        print("[dry-run] To:", msg["To"])
        print("[dry-run] Subject:", msg["Subject"])
        print("[dry-run] REPO:", REPO or "(未设置，备份链接将省略 zip 项)")
        print("[dry-run] 未连接 SMTP，未发送")
        return
    if "--check-time" in sys.argv and not should_run():
        print("非 Brussels 工作日 08:00 时段，退出。")
        return

    date_str = now_local().strftime("%Y-%m-%d")
    st = load_state(); prune_state(st)
    items, errors = fetch()
    fresh = [it for it in items if it["id"] not in st["seen"]]
    for it in fresh:
        st["seen"][it["id"]] = now_local().replace(tzinfo=None).isoformat()
    save_state(st)
    issue_items = fresh or items
    issue_items = llm_translate(issue_items)

    # 1) 本期存档
    (ARCHIVE / f"{date_str}.html").write_text(render_issue(issue_items, errors, date_str), encoding="utf-8")
    # 2) 重建索引：manifest + 首页（日历 + 历史）
    issues = list_issues()
    head = issue_items[0]["title"] if issue_items else "本期无新条目"
    for i in issues:
        if i["date"] == date_str:
            i["title"] = head
    update_manifest(issues)
    html = render_index(issue_items, errors, issues, date_str)
    (PUBLIC / "index.html").write_text(html, encoding="utf-8")
    print(f"已生成 archive/{date_str}.html、index.html、manifest.json；新条目 {len(fresh)}（去重后），异常源 {len(errors)}。")
    for e in errors[:5]:
        print("WARN:", e)
    if "--send" in sys.argv:
        if not MAIL_PASS:
            print("ERROR: MAIL_PASS 为空——请确认 .env 或 Secrets 已配置 163 授权码。")
            sys.exit(2)
        ok = send_email(html)
        if ok:
            print("邮件已发送。")
        else:
            sys.exit(3)

if __name__ == "__main__":
    main()
