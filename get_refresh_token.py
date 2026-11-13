"""
ONE-TIME script. Run this on YOUR OWN computer (not on GitHub).

1. Put your downloaded client_secret_*.json in the same folder and rename it
   to: client_secret.json
2. pip install google-auth-oauthlib
3. python get_refresh_token.py
4. Log in in the browser, click Advanced -> "Go to ... (unsafe)" -> Allow.
5. Copy the printed values into GitHub repo secrets.

Never commit client_secret.json or the refresh token.
"""
import json
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]

flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
creds = flow.run_local_server(
    port=0,
    access_type="offline",
    prompt="consent",  # forces Google to return a refresh token
)

with open("client_secret.json") as f:
    info = json.load(f)["installed"]

print("\n=== Add these as GitHub repo secrets ===")
print("GDRIVE_CLIENT_ID     =", info["client_id"])
print("GDRIVE_CLIENT_SECRET =", info["client_secret"])
print("GDRIVE_REFRESH_TOKEN =", creds.refresh_token)
