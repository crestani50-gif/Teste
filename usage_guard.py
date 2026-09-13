import json
import os
from datetime import datetime

MAX_AUDITS_PER_MONTH = 3
UNLIMITED_USERS = [
    "xcrestani@hotmail.com",
    "crestani50@gmail.com"
]

def check_and_increment_usage(email: str, db_path: str = "audit_usage.json", increment: bool = True):
    """
    Validates and increments monthly usage quota.
    Default: 3 audits/month in Beta. Unlimited for administrators.
    """
    if not email or not isinstance(email, str):
        return False, 0, "A valid email address is required."

    clean_email = email.strip().lower()

    # Checagem de acesso ilimitado / Admin
    for admin_pattern in UNLIMITED_USERS:
        if admin_pattern.lower() in clean_email:
            return True, 0, "Authorized (Unlimited Admin Access Tier)."

    current_month = datetime.now().strftime("%Y-%m")
    usage = {}

    if os.path.exists(db_path):
        try:
            with open(db_path, "r", encoding="utf-8") as f:
                usage = json.load(f)
        except Exception:
            usage = {}

    if clean_email not in usage:
        usage[clean_email] = {}
    if current_month not in usage[clean_email]:
        usage[clean_email][current_month] = 0

    current_count = usage[clean_email][current_month]

    if current_count >= MAX_AUDITS_PER_MONTH:
        return False, current_count, f"You have reached the monthly limit of {MAX_AUDITS_PER_MONTH} audits for {current_month}. Contact enterprise support for unlimited access."

    if not increment:
        return True, current_count, f"Authorized. Current monthly usage: {current_count}/{MAX_AUDITS_PER_MONTH}"

    if not increment:
        return True, current_count, f"Authorized. Current monthly usage: {current_count}/{MAX_AUDITS_PER_MONTH}"

    usage[clean_email][current_month] = current_count + 1

    try:
        with open(db_path, "w", encoding="utf-8") as f:
            json.dump(usage, f, indent=2)
    except Exception:
        pass

    return True, usage[clean_email][current_month], f"Audit authorized. Monthly usage: {usage[clean_email][current_month]}/{MAX_AUDITS_PER_MONTH}"
