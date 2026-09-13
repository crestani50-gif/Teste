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
