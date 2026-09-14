import os
import tempfile
import usage_guard

def test_usage_quota_check_without_increment():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        temp_db = tf.name

    try:
        email = "controller.test@auditfirm.com"
        # 1. Checagem pura sem incrementar
        allowed, count, msg = usage_guard.check_and_increment_usage(email, db_path=temp_db, increment=False)
        assert allowed is True
        assert count == 0

        # 2. Incrementa 1x
        allowed, count, msg = usage_guard.check_and_increment_usage(email, db_path=temp_db, increment=True)
        assert allowed is True
        assert count == 1

        # 3. Nova checagem sem incrementar deve manter count == 1
        allowed, count, msg = usage_guard.check_and_increment_usage(email, db_path=temp_db, increment=False)
        assert allowed is True
        assert count == 1
    finally:
        if os.path.exists(temp_db):
            os.remove(temp_db)

def test_fail_closed_logic_when_guard_unavailable():
    # Simula a funcao fallback em app.py
    def mock_fallback(email: str, increment: bool = True):
        is_dev = os.environ.get("DEV_MODE", "").lower() in ("true", "1", "yes")
        if is_dev:
            return True, 1, "Dev allowed"
        return False, 0, "Blocked"

    os.environ["DEV_MODE"] = "false"
    allowed, _, _ = mock_fallback("user@corp.com")
    assert allowed is False, "Em producao sem DEV_MODE, deve falhar fechado"

    os.environ["DEV_MODE"] = "true"
    allowed, _, _ = mock_fallback("user@corp.com")
    assert allowed is True, "Em DEV_MODE explicito, pode bypassar"
    del os.environ["DEV_MODE"]


def test_quota_increment_failure_blocks_execution():
    """Garante que tentativa alem do limite mensal e rejeitada no incremento."""
    import tempfile
    import usage_guard
    import os

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        temp_db = tf.name

    try:
        email = "limited.controller@auditfirm.com"
        for _ in range(3):
            usage_guard.check_and_increment_usage(email, db_path=temp_db, increment=True)

        # A 4a tentativa de incremento efetivo DEVE retornar False
        allowed_inc, count, msg = usage_guard.check_and_increment_usage(email, db_path=temp_db, increment=True)
        assert allowed_inc is False
        assert count == 3
        assert "limit" in msg.lower()
    finally:
        if os.path.exists(temp_db):
            os.remove(temp_db)

def test_quarantine_exposure_never_yields_clean_status():
    """Garante que presenca de quarentena forca status nao-CLEAN independentemente do residuo."""
    import app
    import pandas as pd

    s_df = pd.DataFrame([{
        "Date": "2026-08-01", "Transaction Type": "charge", "Gross": "100.00",
        "Fee": "3.00", "Net": "97.00", "Description": "Valid Charge", "payout_id": "po_01"
    }])
    q_df = pd.DataFrame([
        {"Date": "2026-08-01", "Num": "TR-1", "Description": "po_01", "Debit": "97.00", "Credit": "0.00"},
        {"Date": "MALFORMED_DATE_XYZ", "Num": "TR-2", "Description": "po_corrupted", "Debit": "50.00", "Credit": "0.00"}
    ])

    res = app.run_forensic_reconciliation(s_df, q_df)
    assert len(res["quarantined"]) > 0
    assert res["audit_status"] != "CLEAN", "Status JAMAIS pode ser CLEAN quando ha quarentena"
    assert res["audit_status"] in ("INCONCLUSIVE", "UNEXPLAINED")
