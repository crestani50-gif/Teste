import sys
import os
import random
import pandas as pd
from datetime import date, timedelta
from decimal import Decimal

# Impede a inicialização acidental da UI ao importar app.py
os.environ["RUN_STREAMLIT"] = "false"

from app import (
    run_forensic_reconciliation,
    build_chronological_universe,
    validate_schemas,
    ForensicParser
)

def run_deterministic_battery():
    print("=" * 80)
    print("      STRIPE - QBO AUDITOR: BATERIA DETERMINÍSTICA v0.1.0")
    print("=" * 80)
    
    # -------------------------------------------------------------
    # NÍVEL 1: CASO CANÔNICO PERFEITO
    # -------------------------------------------------------------
    def test_001():
        s_df, q_perfect, p_dates, _ = build_chronological_universe(seed=101)
        q_df = pd.DataFrame(q_perfect)
        res = run_forensic_reconciliation(s_df, q_df)
        passed = (len(res['detections']) == 0) and (res['residual'] == Decimal("0.00"))
        return passed, f"Detections={len(res['detections'])}, Residual={res['residual']}"

    # -------------------------------------------------------------
    # NÍVEL 2: CASOS UNITÁRIOS ISOLADOS
    # -------------------------------------------------------------
    def test_single_anomaly(anomaly_key, expected_cat):
        s_df, q_perfect, p_dates, _ = build_chronological_universe(seed=202)
        q_corrupted = [row.copy() for row in q_perfect]
        
        if anomaly_key == "TIMING_WINDOW":
            for row in q_corrupted:
                if "Transferência Payout po_week_1" in row['Memo/Description']:
                    row['Date'] = str(p_dates['po_week_1'] + timedelta(days=7))
                    break
        elif anomaly_key == "UNBOOKED_FEES":
            for i, row in enumerate(q_corrupted):
                if "Taxas Stripe consolidadas po_week_2" in row['Memo/Description']:
                    q_corrupted.pop(i)
                    break
        elif anomaly_key == "OMITTED_REFUND":
            for i, row in enumerate(q_corrupted):
                if row['Transaction Type'] == 'Refund Receipt':
                    q_corrupted.pop(i)
                    break
        elif anomaly_key == "DUPLICATE_TRANSFER":
            for row in q_corrupted:
                if "Transferência Payout po_week_4" in row['Memo/Description']:
                    dup = row.copy()
                    dup['Num'] = f"{row['Num']}-DUP"
                    q_corrupted.append(dup)
                    break
        elif anomaly_key == "VALUE_MISMATCH":
            for row in q_corrupted:
                if "Transferência Payout po_week_2" in row['Memo/Description']:
                    row['Credit'] = str(Decimal(row['Credit']) + Decimal("50.00"))
                    break
        elif anomaly_key == "OMITTED_ADJUSTMENT":
            for i, row in enumerate(q_corrupted):
                if "Ajuste Tarifário Stripe adj_999" in row['Memo/Description']:
                    q_corrupted.pop(i)
                    break
        elif anomaly_key == "SIGN_INVERSION":
            for i, row in enumerate(q_corrupted):
                if row['Transaction Type'] == 'Journal Entry' and "Chargeback Stripe" in row['Memo/Description']:
                    disp = q_corrupted.pop(i)
                    q_corrupted.append({
                        'Date': disp['Date'], 'Transaction Type': 'Sales Receipt',
                        'Num': f"{disp['Num']}-INV", 'Memo/Description': f"Erro {disp['Memo/Description']}",
                        'Debit': str(disp['Credit']), 'Credit': "0.00"
                    })
                    break
        elif anomaly_key == "UNMATCHED_ORPHAN":
            q_corrupted.append({
                'Date': str(p_dates['po_week_3']), 'Transaction Type': 'Transfer',
                'Num': "TR-ORPH-UNIT", 'Memo/Description': "Lançamento Avulso Sem Stripe",
                'Debit': "0.00", 'Credit': "150.00"
            })
        elif anomaly_key == "CORRUPTED_ENTRY":
            for row in q_corrupted:
                if "Transferência Payout po_week_3" in row['Memo/Description']:
                    row['Credit'] = "VALOR_INVALIDO_ERR"
                    break

        res = run_forensic_reconciliation(s_df, pd.DataFrame(q_corrupted))
        cats = [d['categoria'] for d in res['detections']]
        passed = expected_cat in cats
        return passed, f"Esperado='{expected_cat}' | Encontrados={cats}"

    # -------------------------------------------------------------
    # NÍVEL 3 & 4: ADVERSARIAIS, GUARDAS E PARSING
    # -------------------------------------------------------------
    def test_010_multiple_currencies():
        s_df, q_perfect, _, _ = build_chronological_universe(seed=303)
        s_df_multi = s_df.copy()
        s_df_multi.loc[0, 'currency'] = 'eur'
        res = run_forensic_reconciliation(s_df_multi, pd.DataFrame(q_perfect))
        passed = (res is None)
        return passed, f"Retorno abortado={res is None}"

    def test_011_mixed_date_formats():
        parser = ForensicParser()
        d1 = parser.parse_date("2026-08-15", "R1", "Date")
        d2 = parser.parse_date("15/08/2026", "R2", "Date")
        d3 = parser.parse_date("08/15/2026", "R3", "Date")
        passed = (d1 == date(2026, 8, 15)) and (d2 == date(2026, 8, 15)) and (d3 == date(2026, 8, 15))
        return passed, f"Datas: {d1}, {d2}, {d3}"

    def test_012_accounting_parentheses():
        parser = ForensicParser()
        v1 = parser.parse_currency("(150.50)", "R1", "Credit")
        v2 = parser.parse_currency("$ (2,300.00) ", "R2", "Debit")
        passed = (v1 == Decimal("-150.50")) and (v2 == Decimal("-2300.00"))
        return passed, f"Valores: {v1}, {v2}"

    def test_013_schema_validation():
        valid, msg = validate_schemas(pd.DataFrame([{"err": 1}]), pd.DataFrame([{"err": 2}]))
        passed = (valid is False) and ("Colunas ausentes" in msg)
        return passed, f"Bloqueado={not valid}"

    def test_014_stripe_math_invariant():
        s_df, q_perfect, _, _ = build_chronological_universe(seed=404)
        s_corrupt = s_df.copy()
        s_corrupt.loc[0, 'net'] = str(Decimal(s_corrupt.loc[0, 'net']) + Decimal("50.00"))
        res = run_forensic_reconciliation(s_corrupt, pd.DataFrame(q_perfect))
        quar_reasons = [q['error'] for q in res['quarantined']]
        passed = "STRIPE_INVARIANT_VIOLATION_GROSS_PLUS_FEE_NEQ_NET" in quar_reasons
        return passed, f"Capturado em quarentena={passed}"

    suite = [
        ("TEST-001", "Perfect Ledger (Zero Anomalias)", test_001),
        ("TEST-002", "Timing Anomaly Detection", lambda: test_single_anomaly("TIMING_WINDOW", "Janela de Liquidação (Timing)")),
        ("TEST-003", "Unbooked Fee Isolation", lambda: test_single_anomaly("UNBOOKED_FEES", "Taxas Não Escrituradas")),
        ("TEST-004", "Omitted Refund Detection", lambda: test_single_anomaly("OMITTED_REFUND", "Reembolso Omitido")),
        ("TEST-005", "Duplicate Payout Detection", lambda: test_single_anomaly("DUPLICATE_TRANSFER", "Duplicidade de Transferência")),
        ("TEST-006", "Value Mismatch Detection", lambda: test_single_anomaly("VALUE_MISMATCH", "Erro de Valor / Digitação")),
        ("TEST-007", "Sign Inversion Detection", lambda: test_single_anomaly("SIGN_INVERSION", "Erro de Sinal / Inversão Contábil")),
        ("TEST-008", "Orphan QBO Entry Detection", lambda: test_single_anomaly("UNMATCHED_ORPHAN", "Lançamento Órfão no Razão")),
        ("TEST-009", "Corrupted Entry Isolation", lambda: test_single_anomaly("CORRUPTED_ENTRY", "Dados em Quarentena")),
        ("TEST-010", "Multi-Currency Abort Guard", test_010_multiple_currencies),
        ("TEST-011", "Mixed Date Formats Parsing", test_011_mixed_date_formats),
        ("TEST-012", "Accounting Parentheses Negatives", test_012_accounting_parentheses),
        ("TEST-013", "Schema Validation Guard", test_013_schema_validation),
        ("TEST-014", "Stripe Invariant Math Guard", test_014_stripe_math_invariant),
    ]

    all_passed = True
    print(f"{'CÓDIGO':<10} | {'DESCRIÇÃO DO TESTE':<33} | {'STATUS':<8} | {'DETALHES'}")
    print("-" * 80)
    
    for code, desc, fn in suite:
        try:
            passed, detail = fn()
            status = "PASS" if passed else "FAIL"
            if not passed: all_passed = False
            print(f"{code:<10} | {desc:<33} | {status:<8} | {detail}")
        except Exception as e:
            all_passed = False
            print(f"{code:<10} | {desc:<33} | ERROR    | Exceção: {e}")

    print("=" * 80)
    if all_passed:
        print("✅ 14/14 TESTES PASSARAM! Motor validado matematicamente com sucesso.")
    else:
        print("❌ ALGUNS TESTES FALHARAM. Verifique os logs acima.")
    print("=" * 80)

if __name__ == "__main__":
    run_deterministic_battery()
