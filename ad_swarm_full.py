
#!/usr/bin/env python3
"""
hackathon_swarm_full.py - Complete local-first ad swarm
- Lists your Meta ad accounts via Python
- You.com research ($0.05) + Crusoe Nemotron brain
- Creates Campaign -> AdSet -> AdCreative -> Ad (PAUSED)
- Local N+1 memory: ad_swarm.db + runs.jsonl

Requirements: pip install openai requests youdotcom

Env vars:
  YDC_API_KEY=...              # you.com/platform ( $100 free )
  CRUSOE_API_KEY=...           # Intelligence API Key
  META_ACCESS_TOKEN=...        # System User token: Business Settings > System Users > Generate Token
  META_AD_ACCOUNT_ID=act_...   # optional, if not set we list accounts
  META_PAGE_ID=...             # your FB Page ID for ad creatives
  META_LINK=https://example.com

Usage:
  python hackathon_swarm_full.py --list-accounts
  python hackathon_swarm_full.py --hypothesis "UGC + price anchor for GenZ" --dry-run
  python hackathon_swarm_full.py --hypothesis "UGC + price anchor for GenZ"
  python hackathon_swarm_full.py --learn --account act_123
  python hackathon_swarm_full.py --list-runs
"""
import os, sys, json, time, uuid, sqlite3, argparse, pathlib
from datetime import datetime
import requests

# Try to import openai, but allow dry-run without it
try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

# Try to import youdotcom, but allow dry-run without it
try:
    from youdotcom import You
    from youdotcom.models.researcheffort import ResearchEffort
    HAS_YOUDOTCOM = True
except ImportError:
    HAS_YOUDOTCOM = False

# ---------- CONFIG ----------
YDC_API_KEY = os.getenv("YDC_API_KEY")
CRUSOE_API_KEY = os.getenv("CRUSOE_API_KEY")
META_TOKEN = os.getenv("META_ACCESS_TOKEN")
META_PAGE_ID = os.getenv("META_PAGE_ID")
META_LINK = os.getenv("META_LINK", "https://example.com")
AD_ACCOUNT_ENV = os.getenv("META_AD_ACCOUNT_ID")
GRAPH_VER = "v21.0"
DB_PATH = pathlib.Path("ad_swarm.db")
JSONL_PATH = pathlib.Path("runs.jsonl")
CRUSOE_BASE = "https://api.inference.crusoecloud.com/v1"
CRUSOE_MODEL = "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B"
CRUSOE_FALLBACK = "nvidia/Nemotron-3.5-Lightning-30B-A3B"

# ---------- 1. LOCAL MEMORY (N+1) ----------
def init_memory():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        timestamp TEXT,
        hypothesis TEXT,
        youcom_angle TEXT,
        youcom_sources TEXT,
        creatives_json TEXT,
        targeting TEXT,
        campaign_id TEXT,
        adset_id TEXT,
        spend REAL DEFAULT 0,
        roas REAL DEFAULT 0,
        ctr REAL DEFAULT 0,
        hold_rate REAL DEFAULT 0,
        cpa REAL DEFAULT 0,
        score REAL DEFAULT 0,
        learning TEXT,
        raw_insights TEXT
    )""")
    conn.commit()
    return conn

def save_run(conn, run: dict):
    with open(JSONL_PATH, "a") as f:
        f.write(json.dumps(run, default=str) + "\n")
    cols = ",".join(run.keys())
    placeholders = ",".join(["?"]*len(run))
    conn.execute(f"INSERT OR REPLACE INTO runs ({cols}) VALUES ({placeholders})", list(run.values()))
    conn.commit()
    print(f"[MEMORY] Saved {run['id']} | score={run.get('score',0):.2f}")

def get_best_learnings(conn, limit=5) -> str:
    cur = conn.execute("SELECT hypothesis, learning, roas, ctr, score FROM runs WHERE score > 0 ORDER BY score DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    if not rows:
        return "No past runs yet - this is run #1. Be creative and establish baseline."
    return "\n".join([f"- WIN: {r['hypothesis']} -> {r['learning']} (ROAS:{r['roas']:.2f} CTR:{r['ctr']:.2f} Score:{r['score']:.2f})" for r in rows])

# ---------- 2. META ACCOUNT DISCOVERY (PYTHON) ----------
def meta_get(path, params=None):
    if not META_TOKEN:
        raise ValueError("META_ACCESS_TOKEN not set")
    params = params or {}
    params["access_token"] = META_TOKEN
    url = f"https://graph.facebook.com/{GRAPH_VER}/{path}"
    r = requests.get(url, params=params, timeout=30)
    if r.status_code != 200:
        print(f"[META GET ERROR] {r.status_code} {r.text}")
    r.raise_for_status()
    return r.json()

def meta_post(path, data):
    if not META_TOKEN:
        print(f"[MOCK POST] {path} {data}")
        return {"id": f"mock_{uuid.uuid4().hex[:8]}"}
    url = f"https://graph.facebook.com/{GRAPH_VER}/{path}"
    data = dict(data)
    data["access_token"] = META_TOKEN
    r = requests.post(url, data=data, timeout=30)
    if r.status_code != 200:
        print(f"[META POST ERROR] {r.status_code} {r.text}")
    r.raise_for_status()
    return r.json()

def list_ad_accounts():
    print("\n=== YOUR META AD ACCOUNTS ===")
    if not META_TOKEN:
        print("Set META_ACCESS_TOKEN first")
        return []
    try:
        data = meta_get("me/adaccounts", {
            "fields": "id,name,account_status,balance,amount_spent,currency,timezone_name,account_id",
            "limit": 50
        })
        accounts = data.get("data", [])
        if not accounts:
            print("No ad accounts found. Check System User assets assignment.")
            return []
        for i, acc in enumerate(accounts):
            status_map = {1:"ACTIVE", 2:"DISABLED", 3:"UNSETTLED", 7:"PENDING", 8:"IN_GRACE", 9:"TEMP_DISABLED"}
            st = status_map.get(acc.get("account_status"), acc.get("account_status"))
            print(f"\n[{i}] {acc['id']}  (account_id: {acc.get('account_id')})")
            print(f"    Name: {acc['name']}")
            print(f"    Status: {st} | Currency: {acc.get('currency')} | TZ: {acc.get('timezone_name')}")
            print(f"    Balance: {acc.get('balance')} | Spent: {acc.get('amount_spent')}")
            print(f"    -> Use: export META_AD_ACCOUNT_ID={acc['id']}")
        return accounts
    except Exception as e:
        print(f"Failed to list accounts: {e}")
        print("Tip: Make sure System User has Admin on Business and token has ads_read + business_management")
        return []

def get_account_details(account_id):
    return meta_get(account_id, {"fields": "id,name,account_status,balance,amount_spent,spend_cap,currency,timezone_name"})

# ---------- 3. YOU.COM ----------
def get_you_client():
    if not YDC_API_KEY or not HAS_YOUDOTCOM:
        return None
    return You(api_key_auth=YDC_API_KEY)

def you_search(query: str, num=5):
    client = get_you_client()
    if not client:
        return []
    try:
        resp = client.search(query=query, count=num)
        return [
            {"url": r.url, "title": r.title, "description": r.description}
            for r in (resp.results.web or [])
        ]
    except Exception as e:
        print(f"[You.com search] {e}")
        return []

def you_research(query: str, effort="standard"):
    client = get_you_client()
    if not client:
        return "No YDC_API_KEY - using hypothesis only", []
    try:
        resp = client.research(
            input=query,
            research_effort=ResearchEffort(effort),
            timeout_ms=90000,
        )
        content = resp.output.content
        sources = [s.model_dump(mode="json") for s in (resp.output.sources or [])]
        return content, sources
    except Exception as e:
        print(f"[You.com research] {e}")
        return f"Research error: {e}", []

# ---------- 4. CRUSOE BRAIN ----------
def get_crusoe_client():
    if not CRUSOE_API_KEY or not HAS_OPENAI:
        return None
    return OpenAI(base_url=CRUSOE_BASE, api_key=CRUSOE_API_KEY)

def crusoe_generate(client, prompt: str, system="You are a performance Meta ads strategist."):
    if not client:
        # mock variants for dry-run
        return json.dumps([
            {"hook": f"Stop scrolling - {prompt[:20]}", "body": "Real results in 7 days. No fluff.", "cta": "SHOP_NOW", "visual_idea": "UGC selfie video, price in first 2s", "why": "Builds on past winners"},
            {"hook": "POV: your old solution fails", "body": "We fixed the 1 thing others ignore.", "cta": "LEARN_MORE", "visual_idea": "Split screen before/after", "why": "Contrast hook"},
            {"hook": "I wasted $200 until this", "body": "Found this, never going back.", "cta": "SHOP_NOW", "visual_idea": "Testimonial + product in hand", "why": "Social proof"},
        ])
    try:
        resp = client.chat.completions.create(
            model=CRUSOE_MODEL,
            messages=[{"role":"system","content":system},{"role":"user","content":prompt}],
            temperature=0.8, max_tokens=2000,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}}
        )
        content = resp.choices[0].message.content
        if content:
            return content
        print(f"[Crusoe {CRUSOE_MODEL}] empty content (finish_reason={resp.choices[0].finish_reason}), trying fallback")
    except Exception as e:
        print(f"[Crusoe {CRUSOE_MODEL} failed] {e}, trying fallback")
    try:
        resp = client.chat.completions.create(
            model=CRUSOE_FALLBACK,
            messages=[{"role":"system","content":system},{"role":"user","content":prompt}],
            extra_body={"chat_template_kwargs": {"enable_thinking": False}}
        )
        return resp.choices[0].message.content or "[]"
    except Exception as e2:
        print(f"[Crusoe fallback failed] {e2}")
        return "[]"

# ---------- 5. META CAMPAIGN BUILDER ----------
def create_campaign_stack(account_id, hypothesis, creatives, dry_run=False):
    run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
    print(f"\n[CAMPAIGN] Creating stack {run_id} in {account_id}")

    if dry_run or not META_TOKEN:
        print("[DRY-RUN] Skipping real creation")
        return {"campaign_id":"dry_camp","adset_id":"dry_adset","ad_ids":["dry_ad_1"],"run_id":run_id}

    if not META_PAGE_ID or META_PAGE_ID == "YOUR_PAGE_ID":
        print("WARNING: Set META_PAGE_ID env var to your FB Page ID - creative creation will fail without it")
        print("Get it via: curl https://graph.facebook.com/v21.0/me/accounts?access_token=$TOKEN")

    # 1. Campaign PAUSED
    camp = meta_post(f"{account_id}/campaigns", {
        "name": f"Swarm {run_id} - {hypothesis[:40]}",
        "objective": "OUTCOME_TRAFFIC",
        "status": "PAUSED",
        "special_ad_categories": "[]",
        "is_adset_budget_sharing_enabled": "false"
    })
    campaign_id = camp["id"]
    print(f"  -> Campaign {campaign_id}")

    # 2. AdSet
    adset = meta_post(f"{account_id}/adsets", {
        "name": f"AdSet {run_id}",
        "campaign_id": campaign_id,
        "daily_budget": 2000,
        "billing_event": "IMPRESSIONS",
        "optimization_goal": "LINK_CLICKS",
        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "targeting": json.dumps({
            "geo_locations": {"countries": ["US"]},
            "age_min": 18, "age_max": 65,
        }),
        "status": "PAUSED"
    })
    adset_id = adset["id"]
    print(f"  -> AdSet {adset_id}")

    ad_ids = []
    for i, cr in enumerate(creatives[:3]):
        try:
            creative = meta_post(f"{account_id}/adcreatives", {
                "name": f"Creative {run_id}-{i}",
                "object_story_spec": json.dumps({
                    "page_id": META_PAGE_ID,
                    "link_data": {
                        "message": f"{cr.get('hook','')} \n\n{cr.get('body','')}",
                        "link": META_LINK,
                        "call_to_action": {"type": cr.get('cta','SHOP_NOW')},
                        "name": cr.get('hook','Swarm Ad')[:40]
                    }
                })
            })
            ad = meta_post(f"{account_id}/ads", {
                "name": f"Ad {run_id}-{i}",
                "adset_id": adset_id,
                "creative": json.dumps({"creative_id": creative["id"]}),
                "status": "PAUSED"
            })
            ad_ids.append(ad["id"])
            print(f"  -> Ad {ad['id']}")
        except Exception as e:
            print(f"  !! Creative {i} failed: {e} (check PAGE_ID)")

    return {"campaign_id":campaign_id, "adset_id":adset_id, "ad_ids":ad_ids, "run_id":run_id}

def poll_insights(account_id, days=7):
    if not META_TOKEN:
        return [{"spend":20,"purchase_roas":[{"value":2.3}],"ctr":1.8,"cpc":0.9,"actions":[]}]
    print(f"[INSIGHTS] Async job for {account_id} last {days}d")
    job = meta_post(f"{account_id}/insights", {
        "fields": json.dumps(["campaign_name","spend","purchase_roas","ctr","cpc","actions"]),
        "level": "campaign",
        "date_preset": f"last_{days}d"
    })
    report_id = job.get("report_run_id")
    if not report_id:
        return []
    for _ in range(15):
        status = meta_get(f"{report_id}", {"fields":"async_status,async_percent_completion"})
        print(f"  {status.get('async_status')} {status.get('async_percent_completion')}%")
        if status.get("async_status") == "Job Completed":
            break
        time.sleep(3)
    data = meta_get(f"{report_id}/insights", {"limit": 100})
    return data.get("data", [])

# ---------- 6. LEARNING ----------
def compute_score(roas, hold_rate, cpa_vol=0):
    return 0.6*roas + 0.3*hold_rate - 0.1*cpa_vol

def extract_learning(client, hypothesis, creatives, insights):
    prompt = f"""Hypothesis: {hypothesis}
Creatives: {json.dumps(creatives)[:2000]}
Insights: {json.dumps(insights)[:2000]}
Return JSON: {{"learning":"<25 words concrete","what_worked":"...","what_failed":"...","next_test":"..."}}"""
    raw = crusoe_generate(client, prompt, system="You are a concise analyst.")
    try:
        s = raw.find("{"); e = raw.rfind("}")+1
        if s>=0: return json.loads(raw[s:e])
    except: pass
    return {"learning": raw[:200], "what_worked":"unknown", "what_failed":"unknown", "next_test":"iterate hook"}

# ---------- MAIN ----------
def main():
    parser = argparse.ArgumentParser(description="Local-first ad swarm")
    parser.add_argument("--hypothesis", type=str, default="UGC testimonial with price anchor")
    parser.add_argument("--account", type=str, default=AD_ACCOUNT_ENV, help="ad account id act_...")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list-accounts", action="store_true", help="List your ad accounts via Python")
    parser.add_argument("--list-runs", action="store_true")
    parser.add_argument("--learn", action="store_true", help="Poll insights and update latest run")
    args = parser.parse_args()

    conn = init_memory()

    if args.list_accounts:
        list_ad_accounts()
        return

    if args.list_runs:
        rows = conn.execute("SELECT id,timestamp,hypothesis,roas,ctr,score,learning FROM runs ORDER BY timestamp DESC LIMIT 20").fetchall()
        for r in rows:
            print(f"{r['timestamp']} | {r['id']} | ROAS {r['roas']:.2f} Score {r['score']:.2f} | {r['hypothesis']} -> {r['learning']}")
        return

    if args.learn:
        acct = args.account or AD_ACCOUNT_ENV
        if not acct:
            print("Need --account act_... or META_AD_ACCOUNT_ID env")
            return
        insights = poll_insights(acct)
        print(f"Got {len(insights)} rows")
        latest = conn.execute("SELECT * FROM runs ORDER BY timestamp DESC LIMIT 1").fetchone()
        if latest and insights:
            roas = 2.1
            try:
                pr = insights[0].get("purchase_roas")
                if isinstance(pr, list) and pr: roas = float(pr[0].get("value",2.1))
                elif pr: roas = float(pr)
            except: pass
            conn.execute("UPDATE runs SET roas=?, score=?, raw_insights=? WHERE id=?",
                         (roas, compute_score(roas,0.4), json.dumps(insights)[:5000], latest["id"]))
            conn.commit()
            print(f"Updated {latest['id']} with ROAS {roas}")
        return

    # Normal swarm run
    account_id = args.account or AD_ACCOUNT_ENV
    if not account_id or account_id=="act_":
        print("\nNo META_AD_ACCOUNT_ID set. Listing accounts first...")
        accs = list_ad_accounts()
        if accs:
            account_id = accs[0]["id"]
            print(f"\nUsing first account: {account_id}")
        else:
            print("Set export META_AD_ACCOUNT_ID=act_... and rerun, or use --dry-run")
            if not args.dry_run: return

    client = get_crusoe_client()
    past = get_best_learnings(conn)
    print(f"\n=== SWARM RUN: {args.hypothesis} ===")
    print(f"[MEMORY] {conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0]} past runs")
    print(f"[PAST]\n{past}\n")

    angle, sources = you_research(f"best Meta ad angles for {args.hypothesis} 2026", effort="standard")
    search_hints = you_search(f"Meta Ads Library {args.hypothesis}", 3)

    gen_prompt = f"""Hypothesis: {args.hypothesis}
N+1 PAST WINNERS:
{past}

FRESH RESEARCH (You.com):
{angle}

COMPETITOR HINTS:
{json.dumps(search_hints)[:1500]}

Task: Generate 5 Meta ad variants that BUILD on past winners.
Each: hook (<10w), body (<30w), cta (SHOP_NOW etc), visual_idea, why_this_builds_on_past
Return JSON array."""

    raw = crusoe_generate(client, gen_prompt)
    try:
        s = raw.find("["); e = raw.rfind("]")+1
        creatives = json.loads(raw[s:e]) if s>=0 else []
    except:
        creatives = [{"hook":args.hypothesis, "body":raw[:200], "cta":"SHOP_NOW"}]

    print(f"\n[GENERATED {len(creatives)} variants]")
    print(json.dumps(creatives, indent=2)[:3000])

    stack = create_campaign_stack(account_id, args.hypothesis, creatives, dry_run=args.dry_run)
    run_doc = {
        "id": stack["run_id"],
        "timestamp": datetime.now().isoformat(),
        "hypothesis": args.hypothesis,
        "youcom_angle": angle[:2000],
        "youcom_sources": json.dumps(sources)[:2000],
        "creatives_json": json.dumps(creatives),
        "targeting": "US 18-65 broad",
        "campaign_id": stack["campaign_id"],
        "adset_id": stack["adset_id"],
        "spend":0,"roas":0,"ctr":0,"hold_rate":0,"cpa":0,"score":0,
        "learning":"pending insights",
        "raw_insights":""
    }
    save_run(conn, run_doc)
    print(f"\nDone. Files: {DB_PATH} + {JSONL_PATH}")
    print(f"Next: python {sys.argv[0]} --learn --account {account_id}  (after 4h)")

if __name__ == "__main__":
    main()
