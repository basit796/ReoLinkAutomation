#!/usr/bin/env python3
"""
One-time Google Drive OAuth consent -> token.json.

Run this ONCE on a machine that has a web browser (e.g. your Windows PC), sign in
with the Google account whose Drive quota you want to use, and it writes a
token.json holding a refresh token. Copy that token.json to the server; the
pipeline then uploads headlessly and refreshes the token on its own.

Prerequisites:
  1. In Google Cloud Console (any project), APIs & Services -> Credentials ->
     Create Credentials -> OAuth client ID -> Application type "Desktop app".
     Download the JSON and save it next to this file as  oauth_client.json.
  2. APIs & Services -> OAuth consent screen -> add your Google account under
     "Test users", then click "PUBLISH APP" (so the refresh token does NOT expire
     after 7 days). The Drive API must be enabled for the project.

Usage:
  python gdrive_auth.py
"""
import os
import sys

import config

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def main():
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_file = getattr(config, "GDRIVE_OAUTH_CLIENT_FILE", "oauth_client.json")
    token_file = getattr(config, "GDRIVE_OAUTH_TOKEN_FILE", "token.json")

    if not os.path.exists(client_file):
        print(f"ERROR: {client_file} not found.\n"
              "Download an OAuth client ID (Desktop app) JSON from Google Cloud "
              "Console and save it there. See the docstring at the top of this file.")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(client_file, SCOPES)
    # Opens a browser and catches the redirect on a local port. On the consent
    # screen: pick the right Google account, and if you see "Google hasn't
    # verified this app", click "Advanced" -> "Go to <app> (unsafe)" -> Allow.
    creds = flow.run_local_server(port=0, prompt="consent",
                                  authorization_prompt_message="")

    with open(token_file, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    print(f"\nOK -> wrote {token_file}. Copy it to the server next to config.py.")
    print(f"Signed in as: {creds.token and 'token acquired'}")


if __name__ == "__main__":
    main()
