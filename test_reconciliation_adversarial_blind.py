import io
import pandas as pd
from decimal import Decimal
import app

def test_csv_structural_bad_lines_forces_inconclusive():
    """Valida que linhas sintaticamente corrompidas no CSV entram em quarentena e tornam o status INCONCLUSIVE."""
    malformed_csv = (
        "Date,Transaction Type,Gross,Fee,Net,Description,payout_id\n"
        "2026-08-01,charge,100.00,3.00,97.00,Valid entry,po_01\n"
        "2026-08-02,Broken,Row,With,Too,Many,Separators,Extra,Fields,po_01\n"
    )
    s_df = app.robust_read_csv(io.StringIO(malformed_csv))
    
    q_df = pd.DataFrame([
        {"Date": "2026-08-05", "Num": "TR-1", "Description": "po_01", "Debit": "97.00", "Credit": "0.00"}
    ])
    
    res = app.run_forensic_reconciliation(s_df, q_df)
    
    assert len(res["quarantined"]) >= 1, "Linha quebrada precisa estar listada em quarantined"
    assert any("STRUCTURAL_CSV_SYNTAX_ERROR" in q.get("motivo_erro", "") for q in res["quarantined"])
    assert res["audit_status"] == "INCONCLUSIVE", "Arquivo com linha estruturalmente corrompida nunca pode receber CLEAN"

def test_ledger_with_generic_memos_and_negative_payout():
    """Valida resiliência algébrica com memo genérico e débito direto de ajuste."""
    s_rows = [
        {"id": "txn_01", "type": "charge", "amount": "100.00", "fee": "3.00", "net": "97.00", "payout_id": "po_neg_01"},
        {"id": "txn_02", "type": "adjustment", "amount": "-150.00", "fee": "0.00", "net": "-150.00", "payout_id": "po_neg_01"},
    ]
    s_df = pd.DataFrame(s_rows)
    
    q_rows = [
        {"Date": "2026-08-05", "Num": "TR-9001", "Description": "Stripe generic bank debit", "Debit": "0.00", "Credit": "53.00"}
    ]
    q_df = pd.DataFrame(q_rows)
    
    res = app.run_forensic_reconciliation(s_df, q_df)
    assert res is not None
    assert "audit_status" in res
    assert isinstance(res["unexplained_residual"], Decimal)

def test_unexplained_residual_not_zero_when_arbitrary_leak_occurs():
    """Garante que lançamentos sem equivalência algébrica caem no resíduo não explicado."""
    s_df = pd.DataFrame([
        {"id": "txn_10", "type": "charge", "amount": "200.00", "fee": "6.00", "net": "194.00", "payout_id": "po_10"}
    ])
    # QBO tem lançamento avulso sem correspondência
    q_df = pd.DataFrame([
        {"Date": "2026-08-10", "Num": "TR-999", "Description": "Unrelated bank adjustment", "Debit": "45.00", "Credit": "0.00"}
    ])
    res = app.run_forensic_reconciliation(s_df, q_df)
    assert res is not None
    assert res["unexplained_residual"] != Decimal("0.00") or len(res["detections"]) > 0
