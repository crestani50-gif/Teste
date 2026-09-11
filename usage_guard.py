import json
import os
from datetime import datetime
from typing import Tuple

STORAGE_FILE = "usage_ledger.json"
FREE_MONTHLY_LIMIT = 2

def _load_ledger() -> dict:
    if not os.path.exists(STORAGE_FILE):
        return {}
    try:
        with open(STORAGE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_ledger(ledger: dict) -> None:
    try:
        with open(STORAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2)
    except Exception:
        pass

def check_and_increment_usage(email: str) -> Tuple[bool, int, str]:
    """
    Verifica cota mensal e consome 1 crédito se disponível.
    Retorna: (autorizado, saldo_usado, mensagem)
    """
    clean_email = email.strip().lower()
    if "@" not in clean_email or "." not in clean_email:
        return False, 0, "Por favor, insira um e-mail corporativo válido."

    ledger = _load_ledger()
    current_period = datetime.now().strftime("%Y-%m")
    
    user_record = ledger.get(clean_email, {})
    period_count = user_record.get(current_period, 0)

    if period_count >= FREE_MONTHLY_LIMIT:
        return False, period_count, (
            f"Você atingiu o limite gratuito de {FREE_MONTHLY_LIMIT} auditorias neste mês ({current_period}). "
            "Entre em contato para auditorias ilimitadas."
        )

    # Incrementa uso
    user_record[current_period] = period_count + 1
    user_record["last_seen"] = datetime.now().isoformat()
    ledger[clean_email] = user_record
    _save_ledger(ledger)

    remaining = FREE_MONTHLY_LIMIT - (period_count + 1)
    return True, period_count + 1, f"Auditoria autorizada. Você possui {remaining} análise(s) gratuita(s) restante(s) este mês."
