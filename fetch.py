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
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
import botocore.exceptions
import requests
from requests_aws4auth import AWS4Auth
from pycognito import Cognito
from pycognito.aws_srp import AWSSRP

import anomaly
import pentair
from db import store_snapshot
from publish import export as export_history
from alerts import check_and_alert
from chlorine_forecast import export_forecast
from digest import maybe_send_digest
from weather import export_weather
from trend_summary import export_summaries
from swim_advisor import export_advice

REGION = "us-west-2"
POOL_ID = "us-west-2_icsnuWQWw"
IDENTITY_POOL_ID = "us-west-2:691e3287-5776-40f2-a502-759de65a8f1c"
CLIENT_ID = "7pk5du7fitqb419oabb3r92lni"
IDP_POOL = f"cognito-idp.{REGION}.amazonaws.com/{POOL_ID}"
LAMBDA_URL = "https://lambda.us-west-2.amazonaws.com/2015-03-31/functions/prod-getDashboardView/invocations"

HERE = Path(__file__).resolve().parent
HISTORY_FILE = HERE / "data" / "history.jsonl"
LATEST_FILE = HERE / "data" / "latest.json"

# A dropped connection or a 5xx from Cognito shouldn't cost a whole 12-hour
# slot, but this API isn't meant to be hammered - so: few attempts, long waits.
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (30, 120)

# Errors where retrying is pointless and potentially harmful: a wrong password
# retried three times is three failed logins against the account, not a fix.
FATAL_COGNITO_ERRORS = {
    "NotAuthorizedException",
    "UserNotFoundException",
    "UserNotConfirmedException",
    "PasswordResetRequiredException",
}


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


def _is_fatal(exc: Exception) -> bool:
    if isinstance(exc, botocore.exceptions.ClientError):
        return exc.response.get("Error", {}).get("Code") in FATAL_COGNITO_ERRORS
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        # 4xx means the request itself is wrong; only 5xx and transport errors
        # are worth a second go.
        return 400 <= exc.response.status_code < 500
    return False


def fetch_with_retries(user: str, password: str, attempts: int = MAX_ATTEMPTS, sleep=time.sleep) -> dict:
    last_error = None
    for attempt in range(attempts):
        try:
            return fetch_dashboard(user, password)
        except Exception as e:  # noqa: BLE001 - re-raised below once retries run out
            if _is_fatal(e):
                raise
            last_error = e
            if attempt < attempts - 1:
                wait = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
                print(f"fetch attempt {attempt + 1} failed ({e}); retrying in {wait}s", file=sys.stderr)
                sleep(wait)
    raise last_error


def main():
    load_dotenv(HERE / ".env")
    user = os.environ.get("WG_USER")
    password = os.environ.get("WG_PASS")
    if not user or not password:
        print("Missing WG_USER / WG_PASS. Copy .env.example to .env and fill it in.", file=sys.stderr)
        sys.exit(1)

    data = fetch_with_retries(user, password)

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {"fetched_at": datetime.now(timezone.utc).isoformat(), "data": data}

    with HISTORY_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")
    LATEST_FILE.write_text(json.dumps(record, indent=2))

    rows = store_snapshot(record["fetched_at"], data)
    for row in rows:
        print(f"OK - {row['name']}: status={row['status']} freeCl={row['free_cl']} ph={row['ph']} temp={row['water_temp']}")

    site_data = HERE / "site" / "data"

    export_history()
    check_and_alert(rows)

    # Each of these enriches the dashboard but none is load-bearing: a failure
    # here should leave the readings published, not take the whole run down.
    for label, step in (
        ("weather export", lambda: export_weather(site_data / "weather.json")),
        ("pool system read", lambda: pentair.export_system(site_data / "system.json")),
        ("pool log", lambda: pentair.export_log(site_data / "pool_log.json")),
        ("anomaly export", lambda: anomaly.export_anomalies(site_data / "anomalies.json")),
        ("trend summary", lambda: export_summaries(site_data / "history.json", site_data / "summary.json")),
        (
            "chlorine forecast",
            lambda: export_forecast(site_data / "weather.json", site_data / "chlorine_forecast.json"),
        ),
        (
            "swim advisor",
            lambda: export_advice(
                site_data / "weather.json",
                site_data / "history.json",
                site_data / "swim_advice.json",
            ),
        ),
        # Last, so the digest sees everything above it.
        ("weekly digest", maybe_send_digest),
    ):
        try:
            step()
        except Exception as e:
            print(f"{label} failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
