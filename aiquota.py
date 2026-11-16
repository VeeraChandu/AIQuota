#!/usr/bin/env python3
import sys
import os
import json
import base64
import urllib.request
import urllib.error
import subprocess
import datetime
from concurrent.futures import ThreadPoolExecutor

ENDPOINT_BASE = "https://daily-cloudcode-pa.googleapis.com/v1internal"
USER_AGENT = "antigravity/2.23.1"

def get_keychain_payload():
    try:
        out = subprocess.check_output(
            ["security", "find-generic-password", "-s", "gemini", "-a", "antigravity", "-w"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        if out.startswith("go-keyring-base64:"):
            raw = base64.b64decode(out.split(":", 1)[1])
            return json.loads(raw)
    except Exception:
        pass
    return {}

def extract_email(keychain_data):
    id_token = keychain_data.get("id_token")
    if id_token:
        try:
            payload = id_token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.b64decode(payload))
            if claims.get("email"):
                return claims["email"]
        except Exception:
            pass
    # Fallback to app_storage.json
    storage_path = os.path.expanduser("~/Library/Application Support/Antigravity/app_storage.json")
    if os.path.exists(storage_path):
        try:
            with open(storage_path) as f:
                d = json.load(f)
                email = d.get("jetski.onboarding.lastLoginUsername")
                if email:
                    return email
        except Exception:
            pass
    return None

def refresh_token_via_cli():
    try:
        subprocess.run(["agy", "models"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        return get_keychain_payload().get("token", {}).get("access_token")
    except Exception:
        return None

def call_api(endpoint, token):
    req = urllib.request.Request(
        f"{ENDPOINT_BASE}:{endpoint}",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT
        }
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())

def parse_time_remaining(reset_time_str):
    if not reset_time_str:
        return "N/A"
    try:
        dt = datetime.datetime.fromisoformat(reset_time_str.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        diff = dt - now
        if diff.total_seconds() <= 0:
            return "Ready / Resetting"
        total_sec = int(diff.total_seconds())
        days, rem = divmod(total_sec, 86400)
        hours, rem = divmod(rem, 3600)
        mins, _ = divmod(rem, 60)
        if days > 0:
            return f"{days}d {hours}h"
        elif hours > 0:
            return f"{hours}h {mins}m"
        else:
            return f"{mins}m"
    except Exception:
        return reset_time_str

def make_bar(fraction, width=16):
    if fraction is None:
        return " " * width
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    empty = width - filled
    return "█" * filled + "░" * empty

def get_color(fraction):
    if fraction is None:
        return "\033[90m" # Gray
    if fraction >= 0.5:
        return "\033[92m" # Green
    if fraction >= 0.2:
        return "\033[93m" # Yellow
    return "\033[91m"     # Red

def fetch_all():
    keychain_data = get_keychain_payload()
    token = keychain_data.get("token", {}).get("access_token")
    if not token:
        token = refresh_token_via_cli()
        keychain_data = get_keychain_payload()
    if not token:
        raise RuntimeError("Could not find or refresh Antigravity authentication token.")

    email = extract_email(keychain_data)
    endpoints = ["retrieveUserQuotaSummary", "loadCodeAssist"]
    results = {"email": email}

    def fetch_one(ep):
        nonlocal token
        try:
            return ep, call_api(ep, token)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                new_token = refresh_token_via_cli()
                if new_token:
                    token = new_token
                    return ep, call_api(ep, token)
            raise

    with ThreadPoolExecutor(max_workers=2) as executor:
        for ep, data in executor.map(fetch_one, endpoints):
            results[ep] = data

    return results

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_BUCKETS = [  # (api key, label, window)
    ("five_hour", "Five Hour Limit Remaining", "5h"),
    ("seven_day", "Weekly Limit Remaining", "weekly"),
    ("seven_day_opus", "Weekly Opus Remaining", "weekly"),
    ("seven_day_sonnet", "Weekly Sonnet Remaining", "weekly"),
]

def fetch_claude():
    """Claude Code quota via its OAuth token in the keychain, shaped like the --json Antigravity output."""
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            stderr=subprocess.DEVNULL).decode()
        oauth = json.loads(raw)["claudeAiOauth"]
    except Exception:
        raise RuntimeError("Claude Code credentials not found in Keychain (run `claude` and log in).")
    req = urllib.request.Request(CLAUDE_USAGE_URL, headers={
        "Authorization": f"Bearer {oauth['accessToken']}",
        "anthropic-beta": "oauth-2025-04-20",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            usage = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # ponytail: no token refresh here; Claude Code renews it on next use
        raise RuntimeError("Claude token expired - open Claude Code once to refresh." if e.code == 401 else f"HTTP {e.code}")
    buckets = []
    for key, label, window in CLAUDE_BUCKETS:
        b = usage.get(key)
        if b and b.get("utilization") is not None:
            buckets.append({
                "id": key, "name": label, "window": window,
                "remainingFraction": max(0.0, 1 - b["utilization"] / 100),
                "resetTime": b.get("resets_at"),
                "resetsIn": parse_time_remaining(b.get("resets_at")),
                "disabled": False, "description": None,
            })
    plan = (oauth.get("subscriptionType") or "").capitalize()
    return {
        "email": None,
        "tier": {"name": f"Claude {plan}".strip(), "id": oauth.get("subscriptionType")},
        "availableCredits": 0,
        "groups": [{"name": "Claude Code", "description": "", "buckets": buckets}],
    }

def print_claude(payload, use_color):
    BOLD = "\033[1m" if use_color else ""
    RESET = "\033[0m" if use_color else ""
    DIM = "\033[90m" if use_color else ""
    print(f"\n{BOLD}\033[96m✦ Claude Code Usage Limits{RESET} {DIM}({payload['tier']['name']}){RESET}" if use_color
          else f"\n✦ Claude Code Usage Limits ({payload['tier']['name']})")
    print(f"{DIM}{'─' * 56}{RESET}")
    for b in payload["groups"][0]["buckets"]:
        rem = b["remainingFraction"]
        color = get_color(rem) if use_color else ""
        print(f"  {b['name']:<26} {color}[{make_bar(rem)}]{RESET} {BOLD}{rem*100:>3.0f}%{RESET}  {DIM}resets in {b['resetsIn']}{RESET}")
    print(f"{DIM}{'─' * 56}{RESET}\n")

def main():
    use_color = sys.stdout.isatty() and "--no-color" not in sys.argv
    is_json = "--json" in sys.argv
    is_short = "--short" in sys.argv

    if "--claude" in sys.argv:
        try:
            payload = fetch_claude()
        except Exception as e:
            print(json.dumps({"error": str(e)}) if is_json else f"Error fetching limits: {e}", file=sys.stdout if is_json else sys.stderr)
            sys.exit(1)
        if is_json:
            print(json.dumps(payload, indent=2))
        elif is_short:
            print("Claude Code: " + " | ".join(f"{b['window']} {b['remainingFraction']*100:.0f}% ({b['resetsIn']})" for b in payload["groups"][0]["buckets"][:2]))
        else:
            print_claude(payload, use_color)
        return

    try:
        data = fetch_all()
    except Exception as e:
        if is_json:
            print(json.dumps({"error": str(e)}))
        else:
            print(f"Error fetching limits: {e}", file=sys.stderr)
        sys.exit(1)

    quota = data.get("retrieveUserQuotaSummary", {})
    tier_info = data.get("loadCodeAssist", {})
    email = data.get("email")

    paid_tier = tier_info.get("paidTier") or {}
    curr_tier = tier_info.get("currentTier") or {}

    tier_name = paid_tier.get("name") or curr_tier.get("name") or "Antigravity"
    tier_id = paid_tier.get("id") or curr_tier.get("id") or "free-tier"
    credits_list = paid_tier.get("availableCredits") or curr_tier.get("availableCredits") or []
    ai_credits = 0
    for c in credits_list:
        if "creditAmount" in c:
            ai_credits = int(c["creditAmount"])

    # Extract all groups
    groups = quota.get("groups", [])
    
    # Extract short summaries for top bar
    gemini_short = "N/A"
    claude_short = "N/A"

    for g in groups:
        gname = g.get("displayName", "")
        if "gemini" in gname.lower():
            # Find 5h or weekly
            for b in g.get("buckets", []):
                if b.get("window") == "5h":
                    rem = b.get("remainingFraction")
                    if rem is not None:
                        pct = f"{rem*100:.0f}%"
                        reset = parse_time_remaining(b.get("resetTime"))
                        gemini_short = f"{pct} ({reset})"
        elif "claude" in gname.lower() or "gpt" in gname.lower():
            for b in g.get("buckets", []):
                if b.get("window") == "weekly":
                    rem = b.get("remainingFraction")
                    if rem is not None:
                        pct = f"{rem*100:.0f}%"
                        reset = parse_time_remaining(b.get("resetTime"))
                        claude_short = f"{pct} ({reset})"

    if is_json:
        payload = {
            "email": email,
            "tier": {"name": tier_name, "id": tier_id},
            "availableCredits": ai_credits,
            "groups": []
        }
        for g in groups:
            g_data = {
                "name": g.get("displayName"),
                "description": g.get("description"),
                "buckets": []
            }
            for b in g.get("buckets", []):
                g_data["buckets"].append({
                    "id": b.get("bucketId"),
                    "name": b.get("displayName"),
                    "window": b.get("window"),
                    "remainingFraction": b.get("remainingFraction"),
                    "resetTime": b.get("resetTime"),
                    "resetsIn": parse_time_remaining(b.get("resetTime")),
                    "disabled": b.get("disabled", False),
                    "description": b.get("description")
                })
            payload["groups"].append(g_data)
        print(json.dumps(payload, indent=2))
        return

    if is_short:
        print(f"Gemini: {gemini_short} | Claude/GPT: {claude_short}")
        return

    # Terminal formatting
    BOLD = "\033[1m" if use_color else ""
    RESET = "\033[0m" if use_color else ""
    DIM = "\033[90m" if use_color else ""
    CYAN = "\033[96m" if use_color else ""
    GREEN = "\033[92m" if use_color else ""

    bar_len = 16
    print()
    user_str = f" · {email}" if email else ""
    print(f"{BOLD}{CYAN}✦ Antigravity Usage Limits{RESET} {DIM}({tier_name}{user_str}){RESET}")
    print(f"{DIM}Available AI Credits: {ai_credits}{RESET}")
    print(f"{DIM}{'─' * 56}{RESET}")

    for g in groups:
        gname = g.get("displayName", "Group")
        gdesc = g.get("description", "")
        print(f" {BOLD}{gname}{RESET} {DIM}{gdesc}{RESET}")
        
        for b in g.get("buckets", []):
            bname = b.get("displayName", "")
            rem = b.get("remainingFraction")
            disabled = b.get("disabled", False)
            reset_str = parse_time_remaining(b.get("resetTime"))
            
            if disabled:
                color = DIM
                bar = make_bar(rem, bar_len)
                pct_str = f"{rem*100:.0f}%" if rem is not None else "N/A"
                print(f"  {bname:<26} {color}[{bar}]{RESET} {DIM}{pct_str:>4}  (paused){RESET}")
            else:
                color = get_color(rem) if use_color else ""
                bar = make_bar(rem, bar_len)
                pct_str = f"{rem*100:.0f}%" if rem is not None else "N/A"
                print(f"  {bname:<26} {color}[{bar}]{RESET} {BOLD}{pct_str:>4}{RESET}  {DIM}resets in {reset_str}{RESET}")
        print()

    print(f"{DIM}{'─' * 56}{RESET}")
    print()

if __name__ == "__main__":
    main()
