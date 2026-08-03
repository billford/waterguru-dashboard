#!/usr/bin/env python3
"""
Pulls a snapshot from the WaterGuru dashboard API and appends it to data/history.jsonl.

Credentials come from env vars WG_USER / WG_PASS (see .env.example).
Do not run this more than once or twice a day - the auth flow re-does a full
Cognito SRP login every time (no token refresh), and WaterGuru's API is not
meant to be hit more often than that.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
import requests
from requests_aws4auth import AWS4Auth
from pycognito import Cognito
from pycognito.aws_srp import AWSSRP

from db import store_snapshot
from publish import export as export_history
from alerts import check_and_alert

REGION = "us-west-2"
POOL_ID = "us-west-2_icsnuWQWw"
IDENTITY_POOL_ID = "us-west-2:691e3287-5776-40f2-a502-759de65a8f1c"
CLIENT_ID = "7pk5du7fitqb419oabb3r92lni"
IDP_POOL = f"cognito-idp.{REGION}.amazonaws.com/{POOL_ID}"
LAMBDA_URL = "https://lambda.us-west-2.amazonaws.com/2015-03-31/functions/prod-getDashboardView/invocations"

HERE = Path(__file__).resolve().parent
HISTORY_FILE = HERE / "data" / "history.jsonl"
LATEST_FILE = HERE / "data" / "latest.json"


def load_dotenv(path: Path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def fetch_dashboard(user: str, password: str) -> dict:
    boto3.setup_default_session(region_name=REGION)
    client = boto3.client("cognito-idp", region_name=REGION)

    aws = AWSSRP(username=user, password=password, pool_id=POOL_ID, client_id=CLIENT_ID, client=client)
    tokens = aws.authenticate_user()

    id_token = tokens["AuthenticationResult"]["IdToken"]
    refresh_token = tokens["AuthenticationResult"]["RefreshToken"]
    access_token = tokens["AuthenticationResult"]["AccessToken"]

    u = Cognito(POOL_ID, CLIENT_ID, id_token=id_token, refresh_token=refresh_token, access_token=access_token)
    cognito_user = u.get_user()
    user_id = cognito_user._metadata["username"]

    identity_client = boto3.client("cognito-identity", region_name=REGION)
    identity_id = identity_client.get_id(IdentityPoolId=IDENTITY_POOL_ID)["IdentityId"]
    creds = identity_client.get_credentials_for_identity(
        IdentityId=identity_id, Logins={IDP_POOL: id_token}
    )["Credentials"]

    auth = AWS4Auth(creds["AccessKeyId"], creds["SecretKey"], REGION, "lambda", session_token=creds["SessionToken"])
    headers = {"User-Agent": "aws-sdk-iOS/2.24.3 iOS/14.7.1 en_US invoker", "Content-Type": "application/x-amz-json-1.0"}
    body = {"userId": user_id, "clientType": "WEB_APP", "clientVersion": "0.2.3"}

    resp = requests.post(LAMBDA_URL, auth=auth, json=body, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def main():
    load_dotenv(HERE / ".env")
    user = os.environ.get("WG_USER")
    password = os.environ.get("WG_PASS")
    if not user or not password:
        print("Missing WG_USER / WG_PASS. Copy .env.example to .env and fill it in.", file=sys.stderr)
        sys.exit(1)

    data = fetch_dashboard(user, password)

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {"fetched_at": datetime.now(timezone.utc).isoformat(), "data": data}

    with HISTORY_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")
    LATEST_FILE.write_text(json.dumps(record, indent=2))

    rows = store_snapshot(record["fetched_at"], data)
    for row in rows:
        print(f"OK - {row['name']}: status={row['status']} freeCl={row['free_cl']} ph={row['ph']} temp={row['water_temp']}")

    export_history()
    check_and_alert(rows)


if __name__ == "__main__":
    main()
