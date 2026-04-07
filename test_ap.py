import json, httpx, warnings
from datetime import date
warnings.filterwarnings('ignore')
with open('sap-smart-agent/.okta_token_cache.json') as f:
    token = json.load(f).get('access_token','')
headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}

# Get all fields first
url = 'https://vhcals4hci.awspoc.club/sap/opu/odata/sap/MM_SUPPLIER_INVOICE_LIST_SRV/SupInvoice'
r = httpx.get(url, headers=headers, params={'$format': 'json', '$top': '1'}, timeout=10, verify=False, follow_redirects=True)
data = r.json()
results = data.get('d', {}).get('results', [])
if results:
    print('All fields:', list(results[0].keys()))
