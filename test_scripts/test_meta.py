import os, requests, json

TOKEN = os.getenv("META_ACCESS_TOKEN")  # your System User token from Business Settings
VERSION = "v21.0"

if not TOKEN:
    print("Set META_ACCESS_TOKEN first: export META_ACCESS_TOKEN='EAAxxx...'")
    exit(1)

# 1. LIST ALL AD ACCOUNTS YOU HAVE ACCESS TO
url = f"https://graph.facebook.com/{VERSION}/me/adaccounts"
params = {
    "fields": "id,name,account_status,balance,amount_spent,currency,timezone_name",
    "access_token": TOKEN,
    "limit": 50
}
r = requests.get(url, params=params)
r.raise_for_status()
accounts = r.json().get("data", [])

print(f"\nFound {len(accounts)} ad accounts:\n")
for acc in accounts:
    # id comes back as act_123456789
    print(f"  {acc['id']} | {acc['name']} | status={acc['account_status']} | spent={acc.get('amount_spent')} {acc.get('currency')}")

if not accounts:
    exit(0)

# 2. PICK FIRST ACCOUNT AND GET DETAILS + CAMPAIGNS
ad_account_id = accounts[0]['id']  # e.g. act_123456789
print(f"\n--- Details for {ad_account_id} ---")

details = requests.get(
    f"https://graph.facebook.com/{VERSION}/{ad_account_id}",
    params={"fields": "id,name,account_status,balance,amount_spent,spend_cap", "access_token": TOKEN}
).json()
print(json.dumps(details, indent=2))

# 3. LIST CAMPAIGNS IN THAT ACCOUNT
camps = requests.get(
    f"https://graph.facebook.com/{VERSION}/{ad_account_id}/campaigns",
    params={"fields": "id,name,status,objective,daily_budget", "access_token": TOKEN, "limit": 20}
).json()

print(f"\n--- Campaigns ({len(camps.get('data',[]))}) ---")
for c in camps.get("data", []):
    print(f"  {c['id']} | {c['name']} | {c['status']} | {c['objective']}")

# 4. OPTIONAL - using facebook-business SDK (same token)
# pip install facebook-business
"""
from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.adaccount import AdAccount

FacebookAdsApi.init(app_id=None, app_secret=None, access_token=TOKEN)
account = AdAccount(ad_account_id)
print(account.api_get(fields=['name','account_status']))
for camp in account.get_campaigns(fields=['id','name']):
    print(camp)
"""