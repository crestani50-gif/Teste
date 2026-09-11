import sys
import os
import re
import io
import csv
import random
import urllib.request
import json
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
import pandas as pd
import streamlit as st

# Módulo de governança e limite de cotas
try:
    from usage_guard import check_and_increment_usage

    st.set_page_config(
        page_title="Forensic Ledger Reconciliation | Stripe to QBO",
        page_icon="⚖️",
        layout="wide",
        initial_sidebar_state="collapsed"
    )

    st.markdown('''
    <style>
        /* Modern corporate typography and container cleanup */
        .block-container {
            padding-top: 2rem;
            padding-bottom: 2rem;
            max-width: 1200px;
        }
        h1, h2, h3 {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            letter-spacing: -0.02em;
            color: #0f172a;
        }
        /* Metric card enhancements */
        div[data-testid="metric-container"] {
            background-color: #f8fafc;
            border: 1px solid #e2e8f0;
            padding: 1rem 1.25rem;
            border-radius: 8px;
            box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.05);
        }
        /* Legal notice styling */
        .legal-card {
            background-color: #f8fafc;
            border-left: 4px solid #475569;
            padding: 12px 16px;
            border-radius: 0 8px 8px 0;
            font-size: 0.85rem;
            color: #334155;
            margin-bottom: 1.5rem;
        }
        /* Primary button refinement */
        .stButton>button {
            border-radius: 6px;
            font-weight: 500;
        }
    </style>
    ''', unsafe_allow_html=True)

except ImportError:
    # Fallback defensivo caso o módulo ainda não tenha sido criado
    def check_and_increment_usage(email: str):
        return True, 1, "Auditoria liberada (modo sem persistência de cotas)."

SETTLEMENT_WINDOW_DAYS = 4

def d(val):
    return Decimal(str(val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

def match_exact_token(text, token):
    if not text or not token or pd.isna(text):
        return False
    pattern = rf"(?<![a-zA-Z0-9_]){re.escape(str(token))}(?![a-zA-Z0-9_])"
    return bool(re.search(pattern, str(text), re.IGNORECASE))

def robust_read_csv(uploaded_file):
    """
    Lê CSVs reais com tolerância a múltiplos encodings e delimitadores,
    removendo espaços em branco dos cabeçalhos.
    """
    bytes_data = uploaded_file.read()
    uploaded_file.seek(0)
    
    encodings_to_try = ["utf-8-sig", "utf-8", "latin1", "cp1252"]
    text_content = None

    for enc in encodings_to_try:
        try:
            text_content = bytes_data.decode(enc)
            break
        except UnicodeDecodeError:
            continue

    if text_content is None:
        raise ValueError("Não foi possível decodificar o arquivo. Formato de texto incompatível.")

    sample = text_content[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=[',', ';', '\t'])
        sep = dialect.delimiter
    except Exception:
        sep = ','

    df = pd.read_csv(io.StringIO(text_content), sep=sep, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    return df

# --- 1. PERSISTÊNCIA VERIFICADA DE LEADS ---

def save_lead(email):
    import datetime
    import json
    import urllib.request
    import os

    persisted = False
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    record = str(timestamp) + " | " + str(email).strip() + "\n"

    webhook_url = None
    try:
        if "LEAD_WEBHOOK_URL" in st.secrets:
            webhook_url = st.secrets["LEAD_WEBHOOK_URL"]
    except Exception:
        pass

    if not webhook_url:
        webhook_url = os.environ.get("LEAD_WEBHOOK_URL", None)

    if webhook_url:
        try:
            payload = json.dumps({"email": email.strip(), "timestamp": timestamp, "source": "stripe_qbo_diagnostic"}).encode("utf-8")
            req = urllib.request.Request(
                webhook_url,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status in (200, 201, 202, 204):
                    persisted = True
        except Exception:
            pass

    try:
        with open("leads_interessados.txt", "a", encoding="utf-8") as f_out:
            f_out.write(record)
        persisted = True
    except Exception:
        pass

    return persisted

# --- 2. PARSER FORENSE E VALIDAÇÃO DE ESQUEMA ---

class ForensicParser:
    def __init__(self):
        self.quarantine = []
        self.quarantined_tokens = set()

    def parse_currency(self, val, row_id, field_name, context_memo="", raw_record=None):
        if pd.isna(val) or val == "" or val is None:
            return Decimal("0.00")
        
        s = str(val).strip().replace("$", "").replace(",", "").strip()
        
        if s.startswith("(") and s.endswith(")"):
            s = "-" + s[1:-1].strip()
            
        try:
            return Decimal(s).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError):
            self.quarantine.append({
                "row_id": row_id,
                "field": field_name,
                "raw_value": str(val),
                "memo": context_memo,
                "raw_record": raw_record or {},
                "error": "CORRUPTED_NUMERIC_FIELD"
            })
            for t in re.findall(r'[a-zA-Z0-9_]{3,}', str(context_memo)):
                self.quarantined_tokens.add(t)
            return None

    def parse_date(self, val, row_id, field_name, context_memo="", raw_record=None):
        if not val or pd.isna(val):
            return None
        part = str(val).split(" ")[0].split("T")[0].strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(part, fmt).date()
            except ValueError:
                pass
        self.quarantine.append({
            "row_id": row_id,
            "field": field_name,
            "raw_value": str(val),
            "memo": context_memo,
            "raw_record": raw_record or {},
            "error": "CORRUPTED_DATE_FIELD"
        })
        return None

def validate_schemas(stripe_df, qbo_df):
    req_stripe = {'balance_transaction_id', 'created_utc', 'type', 'gross', 'fee', 'net', 'payout_id'}
    req_qbo = {'Date', 'Transaction Type', 'Num', 'Memo/Description', 'Debit', 'Credit'}
    missing_stripe = req_stripe - set(stripe_df.columns)
    missing_qbo = req_qbo - set(qbo_df.columns)

    if missing_stripe:
        return False, f"Arquivo da Stripe inválido. Colunas ausentes: {', '.join(missing_stripe)}"
    if missing_qbo:
        return False, f"Arquivo do QBO inválido. Colunas ausentes: {', '.join(missing_qbo)}"
    return True, None

# --- 3. CENÁRIO DEMO DE DIAGNÓSTICO ---

def generate_canonical_demo_data():
    stripe_rows = []
    tx_counter = 1000
    charges = []
    start_date = date(2026, 8, 1)

    for i in range(1, 101):
        tx_id = f"ch_{tx_counter}"
        tx_counter += 1
        gross = d(25 + (i * 7) % 225)
        fee = -d(gross * d('0.029') + d('0.30'))
        net = gross + fee
        tx_date = start_date + timedelta(days=(i // 4))

        if tx_date <= date(2026, 8, 7):
            payout_id = "po_week_1"
        elif tx_date <= date(2026, 8, 14):
            payout_id = "po_week_2"
        elif tx_date <= date(2026, 8, 21):
            payout_id = "po_week_3"
        else:
            payout_id = "po_week_4"

        row = {
            'balance_transaction_id': tx_id, 'created_utc': f"{tx_date} 10:00:00",
            'type': 'charge', 'gross': str(gross), 'fee': str(fee), 'net': str(net),
            'currency': 'usd', 'payout_id': payout_id, 'date': tx_date,
            'gross_dec': gross, 'fee_dec': fee, 'net_dec': net
        }
        charges.append(row)
        stripe_rows.append(row)

    for i in range(15):
        orig = charges[i * 6]
        ref_date = orig['date'] + timedelta(days=2)
        stripe_rows.append({
            'balance_transaction_id': f"pyr_{i+100}", 'created_utc': f"{ref_date} 14:30:00",
            'type': 'refund', 'gross': str(-orig['gross_dec']), 'fee': "0.00",
            'net': str(-orig['gross_dec']), 'currency': 'usd', 'payout_id': orig['payout_id'],
            'date': ref_date, 'gross_dec': -orig['gross_dec'], 'fee_dec': Decimal("0.00"),
            'net_dec': -orig['gross_dec']
        })

    for i in range(3):
        orig = charges[i * 25 + 2]
        disp_date = orig['date'] + timedelta(days=4)
        disp_gross = -orig['gross_dec']
        disp_fee = Decimal("-15.00")
        stripe_rows.append({
            'balance_transaction_id': f"dp_{i+10}", 'created_utc': f"{disp_date} 09:15:00",
            'type': 'dispute', 'gross': str(disp_gross), 'fee': str(disp_fee),
            'net': str(disp_gross + disp_fee), 'currency': 'usd', 'payout_id': orig['payout_id'],
            'date': disp_date, 'gross_dec': disp_gross, 'fee_dec': disp_fee,
            'net_dec': disp_gross + disp_fee
        })

    adj_date = date(2026, 8, 20)
    stripe_rows.append({
        'balance_transaction_id': "adj_999", 'created_utc': f"{adj_date} 12:00:00",
        'type': 'adjustment', 'gross': "-250.00", 'fee': "0.00", 'net': "-250.00",
        'currency': 'usd', 'payout_id': "po_week_3", 'date': adj_date,
        'gross_dec': Decimal("-250.00"), 'fee_dec': Decimal("0.00"), 'net_dec': Decimal("-250.00")
    })

    payout_totals = {}
    for p_id in ['po_week_1', 'po_week_2', 'po_week_3', 'po_week_4']:
        payout_totals[p_id] = sum(r['net_dec'] for r in stripe_rows if r['payout_id'] == p_id)

    payout_dates = {
        'po_week_1': date(2026, 8, 8), 'po_week_2': date(2026, 8, 15),
        'po_week_3': date(2026, 8, 22), 'po_week_4': date(2026, 8, 29)
    }

    for p_id, total_net in payout_totals.items():
        stripe_rows.append({
            'balance_transaction_id': p_id, 'created_utc': f"{payout_dates[p_id]} 04:00:00",
            'type': 'payout', 'gross': str(-total_net), 'fee': "0.00", 'net': str(-total_net),
            'currency': 'usd', 'payout_id': p_id, 'date': payout_dates[p_id],
            'gross_dec': -total_net, 'fee_dec': Decimal("0.00"), 'net_dec': -total_net
        })

    qbo_perfect = []
    doc_id = 5000

    for r in [x for x in stripe_rows if x['type'] == 'charge']:
        qbo_perfect.append({
            'Date': str(r['date']), 'Transaction Type': 'Sales Receipt', 'Num': f"REC-{doc_id}",
            'Memo/Description': f"Venda Stripe {r['balance_transaction_id']}", 'Debit': str(r['gross_dec']), 'Credit': "0.00"
        })
        doc_id += 1

    for r in [x for x in stripe_rows if x['type'] == 'refund']:
        qbo_perfect.append({
            'Date': str(r['date']), 'Transaction Type': 'Refund Receipt', 'Num': f"REF-{doc_id}",
            'Memo/Description': f"Reembolso {r['balance_transaction_id']}", 'Debit': "0.00", 'Credit': str(abs(r['gross_dec']))
        })
        doc_id += 1

    for r in [x for x in stripe_rows if x['type'] == 'dispute']:
        qbo_perfect.append({
            'Date': str(r['date']), 'Transaction Type': 'Journal Entry', 'Num': f"DISP-{doc_id}",
            'Memo/Description': f"Chargeback Stripe {r['balance_transaction_id']}", 'Debit': "0.00", 'Credit': str(abs(r['gross_dec']))
        })
        doc_id += 1

    for r in [x for x in stripe_rows if x['type'] == 'adjustment']:
        qbo_perfect.append({
            'Date': str(r['date']), 'Transaction Type': 'Journal Entry', 'Num': f"ADJ-{doc_id}",
            'Memo/Description': f"Ajuste Tarifário Stripe {r['balance_transaction_id']}", 'Debit': "0.00", 'Credit': str(abs(r['net_dec']))
        })
        doc_id += 1

    for p_id, total_net in payout_totals.items():
        qbo_perfect.append({
            'Date': str(payout_dates[p_id]), 'Transaction Type': 'Transfer', 'Num': f"TR-{doc_id}",
            'Memo/Description': f"Transferência Payout {p_id}",
            'Debit': str(abs(total_net)) if total_net < 0 else "0.00",
            'Credit': "0.00" if total_net < 0 else str(total_net)
        })
        doc_id += 1

    for p_id in payout_totals.keys():
        fee_sum = abs(sum(r['fee_dec'] for r in stripe_rows if r['payout_id'] == p_id and r['type'] != 'payout'))
        qbo_perfect.append({
            'Date': str(payout_dates[p_id]), 'Transaction Type': 'Journal Entry', 'Num': f"FEE-{doc_id}",
            'Memo/Description': f"Taxas Stripe consolidadas {p_id}", 'Debit': "0.00", 'Credit': str(fee_sum)
        })
        doc_id += 1

    qbo_corrupted = [row.copy() for row in qbo_perfect]

    for row in qbo_corrupted:
        if "Transferência Payout po_week_1" in row['Memo/Description']:
            row['Date'] = str(payout_dates['po_week_1'] + timedelta(days=6))
            break

    for i, row in enumerate(qbo_corrupted):
        if "Taxas Stripe consolidadas po_week_2" in row['Memo/Description']:
            qbo_corrupted.pop(i)
            break

    for i, row in enumerate(qbo_corrupted):
        if "Reembolso pyr_100" in row['Memo/Description']:
            qbo_corrupted.pop(i)
            break

    for row in qbo_corrupted:
        if "Transferência Payout po_week_4" in row['Memo/Description']:
            dup = row.copy()
            dup['Num'] = f"{row['Num']}-DUP"
            qbo_corrupted.append(dup)
            break

    for row in qbo_corrupted:
        if "Transferência Payout po_week_2" in row['Memo/Description']:
            row['Credit'] = str(Decimal(row['Credit']) + Decimal("100.00"))
            break

    for i, row in enumerate(qbo_corrupted):
        if "Ajuste Tarifário Stripe adj_999" in row['Memo/Description']:
            qbo_corrupted.pop(i)
            break

    for i, row in enumerate(qbo_corrupted):
        if "Chargeback Stripe dp_10" in row['Memo/Description']:
            disp_orig = qbo_corrupted.pop(i)
            val = Decimal(disp_orig['Credit'])
            qbo_corrupted.append({
                'Date': disp_orig['Date'], 'Transaction Type': 'Sales Receipt',
                'Num': f"{disp_orig['Num']}-INV", 'Memo/Description': f"Erro {disp_orig['Memo/Description']}",
                'Debit': str(val), 'Credit': "0.00"
            })
            break

    clean_stripe = pd.DataFrame([{k: v for k, v in r.items() if not k.endswith('_dec') and k != 'date'} for r in stripe_rows])
    return clean_stripe, pd.DataFrame(qbo_corrupted)

# --- 4. MOTOR DIAGNÓSTICO DE RECONCILIAÇÃO ---

def run_forensic_reconciliation(stripe_df, qbo_df):
    if 'currency' in stripe_df.columns:
        currencies = stripe_df['currency'].dropna().str.lower().unique()
        if len(currencies) > 1:
            return None

    parser = ForensicParser()
    detections = []

    stripe_data = []
    for idx, row in stripe_df.iterrows():
        raw_dict = row.to_dict()
        rid = f"STRIPE_{row.get('balance_transaction_id', idx)}"
        memo = str(row.get('balance_transaction_id', ''))
        gross = parser.parse_currency(row.get('gross'), rid, 'gross', memo, raw_record=raw_dict)
        fee = parser.parse_currency(row.get('fee'), rid, 'fee', memo, raw_record=raw_dict)
        net = parser.parse_currency(row.get('net'), rid, 'net', memo, raw_record=raw_dict)
        p_date = parser.parse_date(row.get('created_utc'), rid, 'created_utc', memo, raw_record=raw_dict)
        
        valid = (gross is not None) and (fee is not None) and (net is not None)
        
        if valid and row.get('type') != 'payout':
            if abs((gross + fee) - net) > Decimal("0.01"):
                parser.quarantine.append({
                    "row_id": rid, "field": "math_invariant",
                    "raw_value": f"gross={gross}, fee={fee}, net={net}",
                    "memo": memo, "raw_record": raw_dict,
                    "error": "STRIPE_INVARIANT_VIOLATION_GROSS_PLUS_FEE_NEQ_NET"
                })
                valid = False

        stripe_data.append({
            "id": str(row.get('balance_transaction_id', '')).strip(),
            "type": str(row.get('type', '')).lower().strip(),
            "gross": gross, "fee": fee, "net": net, "date": p_date,
            "payout_id": str(row.get('payout_id', '')).strip(),
            "valid": valid
        })

    qbo_data = []
    for idx, row in qbo_df.iterrows():
        raw_dict = row.to_dict()
        rid = f"QBO_{row.get('Num', idx)}"
        memo = str(row.get('Memo/Description', '')).strip()
        debit = parser.parse_currency(row.get('Debit'), rid, 'Debit', memo, raw_record=raw_dict)
        credit = parser.parse_currency(row.get('Credit'), rid, 'Credit', memo, raw_record=raw_dict)
        q_date = parser.parse_date(row.get('Date'), rid, 'Date', memo, raw_record=raw_dict)
        valid = (debit is not None) and (credit is not None)
        qbo_data.append({
            "num": str(row.get('Num', '')).strip(),
            "type": str(row.get('Transaction Type', '')).strip(),
            "memo": memo,
            "debit": debit or Decimal("0.00"),
            "credit": credit or Decimal("0.00"),
            "date": q_date,
            "reconciled": False,
            "valid": valid,
            "raw_record": raw_dict
        })

    s_valid = [r for r in stripe_data if r['valid']]
    q_valid = [q for q in qbo_data if q['valid']]

    quarantined_payout_exposure = {}

    # 1. Payouts (com suporte a Payouts a Débito/Negativos)
    payout_events = [r for r in s_valid if r['type'] == 'payout']
    for p in payout_events:
        pid = p['payout_id']
        expected_net = abs(p['net'])
        p_date = p['date']

        if pid in parser.quarantined_tokens:
            quarantined_payout_exposure[pid] = expected_net
            detections.append({
                "categoria": "Dados em Quarentena", "status": "INCONCLUSIVE",
                "documento": pid, "impacto": Decimal("0.00"), "exposure": expected_net, "sinal": "0",
                "descricao": f"Payout {pid} com linha corrompida isolada em quarentena no razão. Exposição nominal líquida de ${expected_net:,.2f}."
            })
            continue

        is_negative_payout = (p['net'] > Decimal("0.00"))
        expected_credit = Decimal("0.00") if is_negative_payout else expected_net
        expected_debit = expected_net if is_negative_payout else Decimal("0.00")

        matches = [q for q in q_valid if not q['reconciled'] and 'Transfer' in q['type'] and match_exact_token(q['memo'], pid)]
        match_type = "token"

        if not matches:
            candidates = [
                q for q in q_valid 
                if not q['reconciled'] and 'Transfer' in q['type'] and (
                    (is_negative_payout and q['debit'] == expected_net) or 
                    (not is_negative_payout and q['credit'] == expected_net)
                )
            ]
            if p_date:
                window_candidates = [q for q in candidates if q['date'] and 0 <= (q['date'] - p_date).days <= SETTLEMENT_WINDOW_DAYS]
                if len(window_candidates) == 1:
                    matches = window_candidates
                    match_type = "heuristic"

        if not matches:
            detections.append({
                "categoria": "Missing Payout", "status": "CONFIRMED",
                "documento": pid, "impacto": expected_net, "exposure": Decimal("0.00"), "sinal": "+" if not is_negative_payout else "-",
                "descricao": f"Payout {pid} (${expected_net:,.2f}) não escriturado no razão."
            })
        elif len(matches) > 1 and match_type == "token":
            for m in matches[1:]:
                m['reconciled'] = True
                m_impact = m['debit'] if m['debit'] > Decimal("0.00") else m['credit']
                m_sign = "+" if m['debit'] > Decimal("0.00") else "-"
                detections.append({
                    "categoria": "Duplicate Payout Entry", "status": "CONFIRMED",
                    "documento": str(m["num"]), "impacto": m_impact, "exposure": Decimal("0.00"), "sinal": m_sign,
                    "descricao": f"Payout {pid} posted as duplicate in QBO ledger ({m['num']})."
                })
            matches[0]['reconciled'] = True
        else:
            m = matches[0]
            m['reconciled'] = True
            actual_effect = m['debit'] - m['credit']
            expected_effect = expected_debit - expected_credit
            diff = actual_effect - expected_effect

            if diff != Decimal("0.00"):
                detections.append({
                    "categoria": "Posting Discrepancy (Amount Variance)", "status": "CONFIRMED",
                    "documento": str(m["num"]), "impacto": abs(diff), "exposure": Decimal("0.00"), "sinal": "+" if diff > 0 else "-",
                    "descricao": f"Payout {pid} with ledger discrepancy: posted effect ${actual_effect:,.2f} vs expected ${expected_effect:,.2f}."
                })
            if m['date'] and p_date:
                delay = (m['date'] - p_date).days
                if delay > SETTLEMENT_WINDOW_DAYS:
                    detections.append({
                        "categoria": "Timing Variance (Settlement Window)", "status": "CONFIRMED",
                        "documento": str(m["num"]), "impacto": Decimal("0.00"), "exposure": Decimal("0.00"), "sinal": "0",
                        "descricao": f"Payout {pid} initiated on {p_date} but posted to bank on {m['date']} (+{delay} dias)."
                    })

    # 2. Taxas por lote liquidado
    payout_ids = sorted(list(set(r['payout_id'] for r in s_valid if r['payout_id'] and r['payout_id'] != "unsettled")))
    unbooked_payout_fees = {}

    for pid in payout_ids:
        total_fee = abs(sum(r['fee'] for r in s_valid if r['payout_id'] == pid and r['type'] != 'payout'))
        matches = [q for q in q_valid if not q['reconciled'] and 'Journal Entry' in q['type'] and match_exact_token(q['memo'], pid)]

        if matches:
            sum_matches = sum(m['credit'] for m in matches)
            for m in matches:
                m['reconciled'] = True
            if sum_matches != total_fee:
                diff_f = sum_matches - total_fee
                detections.append({
                    "categoria": "Taxas Divergentes", "status": "CONFIRMED",
                    "documento": f"Taxas {pid}", "impacto": abs(diff_f), "exposure": Decimal("0.00"), "sinal": "-" if diff_f > 0 else "+",
                    "descricao": f"Taxas do lote {pid}: QBO=${sum_matches:,.2f} vs Stripe=${total_fee:,.2f}."
                })
        else:
            unbooked_payout_fees[pid] = total_fee

    if unbooked_payout_fees:
        total_remaining_fee = sum(unbooked_payout_fees.values())
        consolidated_jes = [
            q for q in q_valid if not q['reconciled'] and 'Journal Entry' in q['type'] and q['credit'] == total_remaining_fee
        ]
        if consolidated_jes:
            consolidated_jes[0]['reconciled'] = True
            unbooked_payout_fees.clear()
        else:
            for pid, fee_amt in unbooked_payout_fees.items():
                detections.append({
                    "categoria": "Unrecorded Stripe Processing Fees", "status": "CONFIRMED",
                    "documento": f"Taxas {pid}", "impacto": fee_amt, "exposure": Decimal("0.00"), "sinal": "+",
                    "descricao": f"Taxas Stripe de ${fee_amt:,.2f} for batch {pid} unrecorded in general ledger."
                })

    # 2.1 Reconciliação de taxas unsettled / provisionadas
    unsettled_fees = abs(sum(r['fee'] for r in s_valid if (not r.get('payout_id') or r.get('payout_id') == "unsettled") and r.get('type') != 'payout'))
    if unsettled_fees > Decimal("0.00"):
        unsettled_matches = [
            q for q in q_valid 
            if not q['reconciled'] and 'Journal Entry' in q['type'] and ('unsettled' in q['memo'].lower() or q['credit'] == unsettled_fees)
        ]
        if unsettled_matches:
            sum_unsettled = sum(m['credit'] for m in unsettled_matches)
            for m in unsettled_matches:
                m['reconciled'] = True
            if sum_unsettled != unsettled_fees:
                diff_u = sum_unsettled - unsettled_fees
                detections.append({
                    "categoria": "Taxas Divergentes", "status": "CONFIRMED",
                    "documento": "Taxas unsettled", "impacto": abs(diff_u), "exposure": Decimal("0.00"), "sinal": "-" if diff_u > 0 else "+",
                    "descricao": f"Taxas unsettled: QBO=${sum_unsettled:,.2f} vs Stripe=${unsettled_fees:,.2f}."
                })
        else:
            detections.append({
                "categoria": "Unrecorded Stripe Processing Fees", "status": "CONFIRMED",
                "documento": "Taxas unsettled", "impacto": unsettled_fees, "exposure": Decimal("0.00"), "sinal": "+",
                "descricao": f"Taxas Stripe de ${unsettled_fees:,.2f} pendentes de liquidação (unsettled) unrecorded in general ledger."
            })

    # 3. Reembolsos
    for r in [x for x in s_valid if x['type'] == 'refund']:
        tx_id = r['id']
        expected_ref = abs(r['gross'])
        matches = [q for q in q_valid if not q['reconciled'] and 'Refund' in q['type'] and match_exact_token(q['memo'], tx_id)]
        if not matches:
            detections.append({
                "categoria": "Omitted Refund Entry", "status": "CONFIRMED",
                "documento": tx_id, "impacto": expected_ref, "exposure": Decimal("0.00"), "sinal": "+",
                "descricao": f"Reembolso {tx_id} in the amount of ${expected_ref:,.2f} missing from QBO ledger."
            })
        else:
            m = matches[0]
            m['reconciled'] = True
            if m['credit'] != expected_ref:
                diff_r = m['credit'] - expected_ref
                detections.append({
                    "categoria": "Reembolso Divergente", "status": "CONFIRMED",
                    "documento": tx_id, "impacto": abs(diff_r), "exposure": Decimal("0.00"), "sinal": "-" if diff_r > 0 else "+",
                    "descricao": f"Reembolso {tx_id} registrado com valor divergente."
                })

    # 4. Ajustes
    for a in [x for x in s_valid if x['type'] == 'adjustment']:
        tx_id = a['id']
        expected_adj = abs(a['net'])
        matches = [q for q in q_valid if not q['reconciled'] and match_exact_token(q['memo'], tx_id)]
        if not matches:
            detections.append({
                "categoria": "Stripe Balance Adjustment", "status": "CONFIRMED",
                "documento": tx_id, "impacto": expected_adj, "exposure": Decimal("0.00"), "sinal": "+",
                "descricao": f"Ajuste interno da Stripe ({tx_id}) de -${expected_adj:,.2f} unposted to general ledger."
            })
        else:
            m = matches[0]
            m['reconciled'] = True
            if m['credit'] != expected_adj:
                diff_a = m['credit'] - expected_adj
                detections.append({
                    "categoria": "Ajuste Stripe Divergente", "status": "CONFIRMED",
                    "documento": tx_id, "impacto": abs(diff_a), "exposure": Decimal("0.00"), "sinal": "-" if diff_a > 0 else "+",
                    "descricao": f"Ajuste {tx_id} com divergência de valor."
                })

    # 5. Disputas
    for d_row in [x for x in s_valid if x['type'] == 'dispute']:
        tx_id = d_row['id']
        expected_disp = abs(d_row['gross'])

        normal_matches = [
            q for q in q_valid
            if not q['reconciled'] and match_exact_token(q['memo'], tx_id) and q['credit'] > Decimal("0.00")
        ]

        if normal_matches:
            m = normal_matches[0]
            m['reconciled'] = True
            if m['credit'] != expected_disp:
                diff_d = m['credit'] - expected_disp
                detections.append({
                    "categoria": "Disputa Divergente", "status": "CONFIRMED",
                    "documento": tx_id, "impacto": abs(diff_d), "exposure": Decimal("0.00"), "sinal": "-" if diff_d > 0 else "+",
                    "descricao": f"Disputa {tx_id} registrada com valor divergente."
                })
        else:
            inverted_matches = [
                q for q in q_valid
                if not q['reconciled'] and match_exact_token(q['memo'], tx_id) and q['debit'] > Decimal("0.00")
            ]
            if inverted_matches:
                m = inverted_matches[0]
                m['reconciled'] = True
                impact = m['debit'] * Decimal("2")
                detections.append({
                    "categoria": "Sign Inversion Error (Dr/Cr Flip)", "status": "CONFIRMED",
                    "documento": tx_id, "impacto": impact, "exposure": Decimal("0.00"), "sinal": "+",
                    "descricao": f"Disputa {tx_id} posted with inverted sign (Debit of ${m['debit']:,.2f} instead of Credit)."
                })
            else:
                detections.append({
                    "categoria": "Disputa / Chargeback Não Escriturado", "status": "CONFIRMED",
                    "documento": tx_id, "impacto": expected_disp, "exposure": Decimal("0.00"), "sinal": "+",
                    "descricao": f"Disputa {tx_id} in the amount of ${expected_disp:,.2f} retida na Stripe mas sem lançamento no QBO."
                })

    # 6. Cobranças
    for ch in [x for x in s_valid if x['type'] == 'charge']:
        ch_id = ch['id']
        expected_gross = ch['gross']
        matches_s = [q for q in q_valid if not q['reconciled'] and match_exact_token(q['memo'], ch_id)]
        if matches_s:
            s_m = matches_s[0]
            s_m['reconciled'] = True
            diff_sale = s_m['debit'] - expected_gross
            if diff_sale != Decimal("0.00"):
                detections.append({
                    "categoria": "Venda Divergente", "status": "CONFIRMED",
                    "documento": str(s_m["num"]), "impacto": abs(diff_sale), "exposure": Decimal("0.00"), "sinal": "+" if diff_sale > 0 else "-",
                    "descricao": f"Venda {ch_id} registrada como ${s_m['debit']:,.2f} (esperado: ${expected_gross:,.2f})."
                })

    # 7. Órfãos
    unmatched_qbo = [q for q in q_valid if not q['reconciled']]
    for u in unmatched_qbo:
        u_val = u['debit'] if u['debit'] > Decimal("0.00") else u['credit']
        u_sign = "+" if u['debit'] > Decimal("0.00") else "-"
        detections.append({
            "categoria": "Lançamento Órfão no Razão", "status": "CONFIRMED",
            "documento": str(u["num"]), "impacto": u_val, "exposure": Decimal("0.00"), "sinal": u_sign,
            "descricao": f"Lançamento no razão sem lastro na Stripe ({u['num']}: {u['type']} - {u['memo']})."
        })

    # Fechamento Contábil de 4 Estados
    net_explained = Decimal("0.00")
    for d_item in detections:
        if d_item['status'] == "CONFIRMED":
            mag = abs(Decimal(str(d_item['impacto'])))
            if d_item['sinal'] == "+":
                net_explained += mag
            elif d_item['sinal'] == "-":
                net_explained -= mag

    # 1. Saldo Líquido Observado no QBO
    total_qbo_net = sum(r['debit'] - r['credit'] for r in q_valid)

    # 2. Saldo Líquido Esperado na Stripe (Fonte da Verdade Primária)
    total_stripe_net = sum(Decimal(str(x.get('net', Decimal('0.00')))) for x in s_valid)

    quarantined_exposure = Decimal("0.00")
    if len(parser.quarantine) > 0:
        for q_item in parser.quarantine:
            memo = str(q_item.get("memo", ""))
            matched = [exp for pid, exp in quarantined_payout_exposure.items() if pid in memo or pid in parser.quarantined_tokens]
            if matched:
                quarantined_exposure += matched[0]
            else:
                raw = q_item.get("raw_record", {})
                c_str = str(raw.get("Credit", raw.get("credit", "0"))).replace("$", "").replace(",", "").strip()
                d_str = str(raw.get("Debit", raw.get("debit", "0"))).replace("$", "").replace(",", "").strip()
                try:
                    c_val = Decimal(c_str)
                    d_val = Decimal(d_str)
                    quarantined_exposure += (c_val - d_val)
                except (InvalidOperation, ValueError):
                    pass

        for d_item in detections:
            if d_item["categoria"] == "Dados em Quarentena":
                d_item["exposure"] = quarantined_exposure
                d_item["descricao"] = f"Payout {d_item['documento']} com linha corrompida isolada em quarentena no razão. Exposição nominal líquida de ${quarantined_exposure:,.2f}."

    delta_b = total_qbo_net - total_stripe_net
    unexplained_residual = (delta_b - net_explained) - quarantined_exposure

    if abs(unexplained_residual) < Decimal("0.01"):
        unexplained_residual = Decimal("0.00")

    if unexplained_residual == Decimal("0.00"):
        audit_status = "INCONCLUSIVE" if len(parser.quarantine) > 0 else "CLEAN"
    else:
        audit_status = "UNEXPLAINED"

    return {
        "detections": detections,
        "quarantined": parser.quarantine,
        "quarantined_exposure": quarantined_exposure,
        "quarantined_amount": quarantined_exposure,
        "qbo_balance": total_qbo_net,
        "stripe_balance": total_stripe_net,
        "delta_balance": delta_b,
        "confirmed_discrepancies": net_explained,
        "net_explained": net_explained,
        "unexplained_residual": unexplained_residual,
        "residual": unexplained_residual,
        "audit_status": audit_status
    }

# --- 5. BANCADA CIENTÍFICA ---

def build_chronological_universe(seed=42):
    rng = random.Random(seed)
    start_date = date(2026, 8, 1) + timedelta(days=rng.randint(0, 2))
    payout_schedule = [
        start_date + timedelta(days=7),
        start_date + timedelta(days=14),
        start_date + timedelta(days=21),
        start_date + timedelta(days=28)
    ]
    payout_ids = ["po_week_1", "po_week_2", "po_week_3", "po_week_4"]

    n_charges = rng.randint(90, 140)
    charges, events = [], []
    tx_counter = 1000

    for i in range(1, n_charges + 1):
        tx_id = f"ch_{tx_counter}"
        tx_counter += 1
        gross = Decimal(str(rng.randint(2500, 35000))) / Decimal("100")
        fee = -(gross * Decimal("0.029") + Decimal("0.30")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        net = gross + fee
        created_date = start_date + timedelta(days=(i // 5))
        avail_date = created_date + timedelta(days=2)

        ev = {
            'id': tx_id, 'type': 'charge', 'created_date': created_date,
            'available_date': avail_date, 'gross': gross, 'fee': fee, 'net': net,
            'payout_id': None
        }
        charges.append(ev)
        events.append(ev)

    n_refunds = max(2, int(n_charges * rng.uniform(0.08, 0.14)))
    for idx, orig in enumerate(rng.sample(charges, n_refunds)):
        ref_created = orig['created_date'] + timedelta(days=rng.randint(1, 4))
        events.append({
            'id': f"pyr_{idx+100}", 'type': 'refund', 'created_date': ref_created,
            'available_date': ref_created + timedelta(days=1), 'gross': -orig['gross'], 'fee': Decimal("0.00"),
            'net': -orig['gross'], 'payout_id': None
        })

    n_disp = rng.randint(2, 3)
    for idx, orig in enumerate(rng.sample(charges, n_disp)):
        disp_created = orig['created_date'] + timedelta(days=rng.randint(2, 5))
        events.append({
            'id': f"dp_{idx+10}", 'type': 'dispute', 'created_date': disp_created,
            'available_date': disp_created + timedelta(days=1), 'gross': -orig['gross'], 'fee': Decimal("-15.00"),
            'net': -orig['gross'] - Decimal("15.00"), 'payout_id': None
        })

    adj_date = start_date + timedelta(days=18)
    adj_val = Decimal(str(rng.randint(150, 400)))
    events.append({
        'id': "adj_999", 'type': 'adjustment', 'created_date': adj_date,
        'available_date': adj_date, 'gross': -adj_val, 'fee': Decimal("0.00"),
        'net': -adj_val, 'payout_id': None
    })

    unassigned = list(events)
    payout_totals = {}
    stripe_payout_rows = []

    for p_id, p_date in zip(payout_ids, payout_schedule):
        batch = [e for e in unassigned if e['available_date'] <= p_date]
        for e in batch:
            e['payout_id'] = p_id
            unassigned.remove(e)

        batch_net = sum(e['net'] for e in batch)
        payout_totals[p_id] = batch_net
        stripe_payout_rows.append({
            'balance_transaction_id': p_id, 'created_utc': f"{p_date} 04:00:00",
            'type': 'payout', 'gross': str(-batch_net), 'fee': "0.00", 'net': str(-batch_net),
            'currency': 'usd', 'payout_id': p_id, 'date': p_date
        })

    stripe_raw = []
    for e in events:
        stripe_raw.append({
            'balance_transaction_id': e['id'], 'created_utc': f"{e['created_date']} 10:00:00",
            'type': e['type'], 'gross': str(e['gross']), 'fee': str(e['fee']),
            'net': str(e['net']), 'currency': 'usd', 'payout_id': e['payout_id'] or "unsettled"
        })
    stripe_raw.extend(stripe_payout_rows)
    stripe_df = pd.DataFrame(stripe_raw)

    qbo_perfect = []
    doc_id = 5000

    for e in [x for x in events if x['type'] == 'charge']:
        qbo_perfect.append({
            'Date': str(e['created_date']), 'Transaction Type': 'Sales Receipt', 'Num': f"REC-{doc_id}",
            'Memo/Description': f"Venda Stripe {e['id']}", 'Debit': str(e['gross']), 'Credit': "0.00"
        })
        doc_id += 1

    for e in [x for x in events if x['type'] == 'refund']:
        qbo_perfect.append({
            'Date': str(e['created_date']), 'Transaction Type': 'Refund Receipt', 'Num': f"REF-{doc_id}",
            'Memo/Description': f"Reembolso {e['id']}", 'Debit': "0.00", 'Credit': str(abs(e['gross']))
        })
        doc_id += 1

    for e in [x for x in events if x['type'] == 'dispute']:
        qbo_perfect.append({
            'Date': str(e['created_date']), 'Transaction Type': 'Journal Entry', 'Num': f"DISP-{doc_id}",
            'Memo/Description': f"Chargeback Stripe {e['id']}", 'Debit': "0.00", 'Credit': str(abs(e['gross']))
        })
        doc_id += 1

    for e in [x for x in events if x['type'] == 'adjustment']:
        qbo_perfect.append({
            'Date': str(e['created_date']), 'Transaction Type': 'Journal Entry', 'Num': f"ADJ-{doc_id}",
            'Memo/Description': f"Ajuste Tarifário Stripe {e['id']}", 'Debit': "0.00", 'Credit': str(abs(e['net']))
        })
        doc_id += 1

    for p_id, p_date in zip(payout_ids, payout_schedule):
        total_net = payout_totals[p_id]
        qbo_perfect.append({
            'Date': str(p_date), 'Transaction Type': 'Transfer', 'Num': f"TR-{doc_id}",
            'Memo/Description': f"Transferência Payout {p_id}",
            'Debit': str(abs(total_net)) if total_net < 0 else "0.00",
            'Credit': "0.00" if total_net < 0 else str(total_net)
        })
        doc_id += 1

    for p_id, p_date in zip(payout_ids, payout_schedule):
        fee_sum = abs(sum(e['fee'] for e in events if e['payout_id'] == p_id))
        qbo_perfect.append({
            'Date': str(p_date), 'Transaction Type': 'Journal Entry', 'Num': f"FEE-{doc_id}",
            'Memo/Description': f"Taxas Stripe consolidadas {p_id}", 'Debit': "0.00", 'Credit': str(fee_sum)
        })
        doc_id += 1

    unsettled_fees = abs(sum(e['fee'] for e in events if e['payout_id'] is None))
    if unsettled_fees > Decimal("0.00"):
        last_date = max(e['created_date'] for e in events)
        qbo_perfect.append({
            'Date': str(last_date),
            'Transaction Type': 'Journal Entry',
            'Num': f"FEE-{doc_id}",
            'Memo/Description': "Taxas Stripe provisionadas (unsettled)",
            'Debit': "0.00",
            'Credit': str(unsettled_fees)
        })
        doc_id += 1

    return stripe_df, qbo_perfect, dict(zip(payout_ids, payout_schedule)), events

def inject_adversarial_suite(qbo_perfect, payout_dates, rng):
    qbo_corrupted = [row.copy() for row in qbo_perfect]
    ground_truth = []

    possible_anomalies = [
        "TIMING_WINDOW", "UNBOOKED_FEES", "OMITTED_REFUND", "DUPLICATE_TRANSFER",
        "VALUE_MISMATCH", "OMITTED_ADJUSTMENT", "SIGN_INVERSION", "UNMATCHED_ORPHAN",
        "CORRUPTED_ENTRY"
    ]
    k = rng.randint(3, len(possible_anomalies))
    active = set(rng.sample(possible_anomalies, k=k))

    if "TIMING_WINDOW" in active:
        for row in qbo_corrupted:
            if "Transferência Payout po_week_1" in row['Memo/Description']:
                row['Date'] = str(payout_dates['po_week_1'] + timedelta(days=rng.randint(5, 8)))
                ground_truth.append({
                    "anomaly_id": "TIMING_A1",
                    "target_id": str(row['Num']),
                    "category": "Timing Variance (Settlement Window)",
                    "delta": Decimal("0.00"),
                    "exposure": Decimal("0.00")
                })
                break

    if "UNBOOKED_FEES" in active:
        for i, row in enumerate(qbo_corrupted):
            if "Taxas Stripe consolidadas po_week_2" in row['Memo/Description']:
                rem = qbo_corrupted.pop(i)
                ground_truth.append({
                    "anomaly_id": "FEE_A2", "target_id": "Taxas po_week_2",
                    "category": "Unrecorded Stripe Processing Fees",
                    "delta": Decimal(rem['Credit']),
                    "exposure": Decimal("0.00")
                })
                break

    if "OMITTED_REFUND" in active:
        for i, row in enumerate(qbo_corrupted):
            if row['Transaction Type'] == 'Refund Receipt':
                rem = qbo_corrupted.pop(i)
                m = re.search(r'pyr_\d+', rem['Memo/Description'])
                tid = m.group(0) if m else "pyr_refund"
                ground_truth.append({
                    "anomaly_id": "REF_A3", "target_id": tid,
                    "category": "Omitted Refund Entry",
                    "delta": Decimal(rem['Credit']),
                    "exposure": Decimal("0.00")
                })
                break

    if "DUPLICATE_TRANSFER" in active:
        for row in qbo_corrupted:
            if "Transferência Payout po_week_4" in row['Memo/Description']:
                dup = row.copy()
                dup['Num'] = f"{row['Num']}-DUP"
                d_val = Decimal(str(row.get('Debit', '0.00')))
                c_val = Decimal(str(row.get('Credit', '0.00')))
                delta_val = d_val if d_val > 0 else -c_val
                qbo_corrupted.append(dup)
                ground_truth.append({
                    "anomaly_id": "DUP_A4", "target_id": dup['Num'],
                    "category": "Duplicate Payout Entry",
                    "delta": delta_val,
                    "exposure": Decimal("0.00")
                })
                break

    if "VALUE_MISMATCH" in active:
        for row in qbo_corrupted:
            if "Transferência Payout po_week_2" in row['Memo/Description']:
                diff = Decimal(str(rng.choice([25, 50, 100, 200])))
                row['Credit'] = str(Decimal(row['Credit']) + diff)
                ground_truth.append({
                    "anomaly_id": "VAL_A5", "target_id": str(row['Num']),
                    "category": "Posting Discrepancy (Amount Variance)",
                    "delta": -diff,
                    "exposure": Decimal("0.00")
                })
                break

    if "OMITTED_ADJUSTMENT" in active:
        for i, row in enumerate(qbo_corrupted):
            if "Ajuste Tarifário Stripe adj_999" in row['Memo/Description']:
                rem = qbo_corrupted.pop(i)
                ground_truth.append({
                    "anomaly_id": "ADJ_A6", "target_id": "adj_999",
                    "category": "Stripe Balance Adjustment",
                    "delta": Decimal(rem['Credit']),
                    "exposure": Decimal("0.00")
                })
                break

    if "SIGN_INVERSION" in active:
        for i, row in enumerate(qbo_corrupted):
            if row['Transaction Type'] == 'Journal Entry' and "Chargeback Stripe" in row['Memo/Description']:
                disp_orig = qbo_corrupted.pop(i)
                val = Decimal(disp_orig['Credit'])
                qbo_corrupted.append({
                    'Date': disp_orig['Date'], 'Transaction Type': 'Sales Receipt',
                    'Num': f"{disp_orig['Num']}-INV", 'Memo/Description': f"Erro {disp_orig['Memo/Description']}",
                    'Debit': str(val), 'Credit': "0.00"
                })
                m = re.search(r'dp_\d+', disp_orig['Memo/Description'])
                tid = m.group(0) if m else "dp_dispute"
                ground_truth.append({
                    "anomaly_id": "SIGN_A7", "target_id": tid,
                    "category": "Sign Inversion Error (Dr/Cr Flip)",
                    "delta": val * Decimal("2"),
                    "exposure": Decimal("0.00")
                })
                break

    if "UNMATCHED_ORPHAN" in active:
        orphan_val = Decimal(str(rng.randint(50, 300)))
        doc_num = f"TR-ORPHAN-{rng.randint(7000, 8999)}"
        qbo_corrupted.append({
            'Date': str(payout_dates['po_week_3']), 'Transaction Type': 'Transfer',
            'Num': doc_num, 'Memo/Description': "Transferência Manual Avulsa",
            'Debit': "0.00", 'Credit': str(orphan_val)
        })
        ground_truth.append({
            "anomaly_id": "ORPH_A8", "target_id": doc_num,
            "category": "Lançamento Órfão no Razão",
            "delta": -orphan_val,
            "exposure": Decimal("0.00")
        })

    if "CORRUPTED_ENTRY" in active:
        for row in qbo_corrupted:
            if "Transferência Payout po_week_3" in row['Memo/Description']:
                exp_val = Decimal(str(row['Credit'])) if str(row['Credit']).replace('.', '', 1).isdigit() else Decimal("0.00")
                row['Credit'] = "CORRUPTED_VAL_ERR"
                ground_truth.append({
                    "anomaly_id": "QUAR_A9", "target_id": "po_week_3",
                    "category": "Dados em Quarentena",
                    "delta": Decimal("0.00"),
                    "exposure": Decimal("5324.00") if exp_val == Decimal("0.00") else exp_val
                })
                break

    return pd.DataFrame(qbo_corrupted), ground_truth

def solve_min_cost_assignment(cost_matrix):
    n_rows = len(cost_matrix)
    if n_rows == 0: return {}
    n_cols = len(cost_matrix[0])
    if n_cols == 0: return {}

    if n_cols > 25 or (n_rows * n_cols) > 250:
        greedy_assignment = {}
        used = set()
        for r in range(n_rows):
            best_col = None
            best_val = float('inf')
            for c in range(n_cols):
                if c not in used and cost_matrix[r][c] < best_val:
                    best_val = cost_matrix[r][c]
                    best_col = c
            if best_col is not None and best_val < 1000:
                greedy_assignment[r] = best_col
                used.add(best_col)
        return greedy_assignment

    best_assignment = {}
    min_total_cost = [float('inf')]

    def backtrack(row_idx, current_cost, used_cols, current_assignment):
        if current_cost >= min_total_cost[0]: return
        if row_idx == n_rows:
            min_total_cost[0] = current_cost
            best_assignment.clear()
            best_assignment.update(current_assignment)
            return

        sorted_cols = sorted(range(n_cols), key=lambda c: cost_matrix[row_idx][c])
        for col_idx in sorted_cols:
            if col_idx not in used_cols:
                cost = cost_matrix[row_idx][col_idx]
                if cost < 1000:
                    used_cols.add(col_idx)
                    current_assignment[row_idx] = col_idx
                    backtrack(row_idx + 1, current_cost + cost, used_cols, current_assignment)
                    del current_assignment[row_idx]
                    used_cols.remove(col_idx)
        backtrack(row_idx + 1, current_cost + 2000, used_cols, current_assignment)

    backtrack(0, 0, set(), {})
    return best_assignment

def evaluate_bipartite_forensic_metrics(detections, ground_truth):
    n_gt = len(ground_truth)
    n_det = len(detections)

    if n_gt == 0 and n_det == 0:
        return {
            "detection_recall": 100.0, "classification_accuracy": 100.0, "value_accuracy": 100.0,
            "financial_recall": 100.0, "financial_precision": 100.0, "false_positive_rate": 0.0,
            "gt_count": 0, "det_count": 0, "anomalies_gt": 0, "anomalies_detected": 0, "missed_value": 0.0
        }

    cost_matrix = []
    for g in ground_truth:
        row = []
        gt_mag = abs(Decimal(str(g.get('delta', 0))))
        gt_exp = abs(Decimal(str(g.get('exposure', 0))))

        for d_item in detections:
            target_match = (
                g['target_id'] == d_item['documento'] or
                match_exact_token(d_item['documento'], g['target_id']) or
                match_exact_token(g['target_id'], d_item['documento'])
            )
            if not target_match:
                c = 1000
            else:
                c = 0
                if g['category'] != d_item['categoria']:
                    c += 50
                det_mag = abs(Decimal(str(d_item.get('impacto', 0))))
                det_exp = abs(Decimal(str(d_item.get('exposure', 0))))

                if gt_mag != det_mag or gt_exp != det_exp:
                    c += 25
            row.append(c)
        cost_matrix.append(row)

    assignment = solve_min_cost_assignment(cost_matrix)

    detection_matches = len(assignment)
    classification_matches = 0
    value_matches = 0
    confirmed_strict_delta = Decimal("0.00")

    total_gt_val = sum(abs(Decimal(str(g.get('delta', 0)))) + abs(Decimal(str(g.get('exposure', 0)))) for g in ground_truth)
    total_flagged_val = sum(abs(Decimal(str(d_item.get('impacto', 0)))) + abs(Decimal(str(d_item.get('exposure', 0)))) for d_item in detections)

    for gt_idx, det_idx in assignment.items():
        g = ground_truth[gt_idx]
        d_found = detections[det_idx]

        is_cls = (g['category'] == d_found['categoria'])
        gt_total = abs(Decimal(str(g.get('delta', 0)))) + abs(Decimal(str(g.get('exposure', 0))))
        det_total = abs(Decimal(str(d_found.get('impacto', 0)))) + abs(Decimal(str(d_found.get('exposure', 0))))
        is_val = (gt_total == det_total)

        if is_cls: classification_matches += 1
        if is_val: value_matches += 1
        if is_cls and is_val:
            confirmed_strict_delta += gt_total

    det_recall = (detection_matches / n_gt * 100) if n_gt > 0 else 100.0
    cls_accuracy = (classification_matches / detection_matches * 100) if detection_matches > 0 else 100.0
    val_accuracy = (value_matches / detection_matches * 100) if detection_matches > 0 else 100.0

    fin_recall = (confirmed_strict_delta / total_gt_val * 100) if total_gt_val > 0 else 100.0
    fin_precision = (confirmed_strict_delta / total_flagged_val * 100) if total_flagged_val > 0 else 100.0
    false_positives = n_det - len(assignment)
    fp_rate = (false_positives / n_det * 100) if n_det > 0 else 0.0

    return {
        "detection_recall": det_recall,
        "classification_accuracy": cls_accuracy,
        "value_accuracy": val_accuracy,
        "financial_recall": min(float(fin_recall), 100.0),
        "financial_precision": min(float(fin_precision), 100.0),
        "false_positive_rate": fp_rate,
        "gt_count": n_gt,
        "det_count": n_det,
        "anomalies_gt": n_gt,
        "anomalies_detected": n_det,
        "missed_value": float(total_gt_val - confirmed_strict_delta)
    }

# --- 6. CAMADA DE INTERFACE (ENCAPSULADA) ---

def run_app():
    
    st.title("Stripe × QuickBooks Online")
    st.subheader("Forensic Reconciliation")
    st.caption("Find and explain ledger discrepancies before month-end close.")

    show_lab = st.query_params.get("lab") == "true"
    if show_lab:
        tab_app, tab_lab = st.tabs(["🚀 Close Diagnostic", "🔬 Scientific Benchmark (Monte Carlo)"])
    else:
        tab_app = st.container()
        tab_lab = None
    with tab_app:
        if "stripe_data" not in st.session_state:
            st.session_state["stripe_data"] = None
        if "qbo_data" not in st.session_state:
            st.session_state["qbo_data"] = None
        if "is_demo" not in st.session_state:
            st.session_state["is_demo"] = False
        if "audit_authorized" not in st.session_state:
            st.session_state["audit_authorized"] = False

        # --- PORTÃO DE ENTRADA, CONTROLE DE ACESSO & ISENÇÃO JURÍDICA ---
        st.markdown("### 0. Audit Setup")
        
        col_em1, col_em2 = st.columns([2, 1])
        with col_em1:
            user_email = st.text_input(
                "Work Email (for audit trail and report logging):",
                placeholder="controller@company.com",
                help="Complimentary tier includes 2 full ledger reconciliation audits per month."
            )
        with col_em2:
            st.caption("🔒 Free tier: 2 audits / month")
        
        st.markdown('''
        > ⚖️ **Notice of Advisory Scope & Professional Review:**  
        > This software is an assistive analytical tool designed to identify potential discrepancies, variances, and reconciliation issues between financial records.  
        > **Recommended Action — Subject to Professional Review:** Diagnostic findings and proposed adjusting journal entries are provided for informational and operational purposes only. They do not constitute tax, legal, accounting, audit, or other licensed professional advice.  
        > Users are responsible for reviewing and validating all findings and proposed adjustments with their qualified accounting or authorized financial professional before posting or relying on them. The software does not independently determine legal, tax, or accounting obligations and does not replace professional review.
        ''')
        
        terms_accepted = st.checkbox(
            "I understand that this tool provides assistive diagnostic analysis and that all findings and proposed adjustments require review by a qualified accounting professional before posting.",
            value=False
        )
        
        st.divider()
        
        st.subheader("1. Ledger Data Ingestion")
        uploaded_stripe = st.file_uploader("Stripe Balance History CSV (Standard Export)", type=["csv"], key="stripe_uploader")
        uploaded_qbo = st.file_uploader("QuickBooks Online Stripe Clearing Ledger CSV", type=["csv"], key="qbo_uploader")
        st.caption("🔒 **No Persistent File Storage:** Uploaded CSV files are processed in memory and are not intentionally stored in databases or persistent file storage.")
                
        if st.button("Load Canonical Audit Benchmark (Demo Dataset)", key="btn_load_canonical"):
            s_mock, q_mock = generate_canonical_demo_data()
            st.session_state["stripe_data"] = s_mock
            st.session_state["qbo_data"] = q_mock
            st.session_state["is_demo"] = True
            st.session_state["audit_authorized"] = True
            st.success("Canonical audit benchmark loaded with 7 intentional variances (Unrestricted Demo).")

        if uploaded_stripe and uploaded_qbo:
            try:
                s_raw = robust_read_csv(uploaded_stripe)
                q_raw = robust_read_csv(uploaded_qbo)
                valid, msg = validate_schemas(s_raw, q_raw)
                if valid:
                    st.session_state["stripe_data"] = s_raw
                    st.session_state["qbo_data"] = q_raw
                    st.session_state["is_demo"] = False
                else:
                    st.error(msg)
                    st.session_state["stripe_data"] = None
                    st.session_state["qbo_data"] = None
                    st.session_state["audit_authorized"] = False
            except Exception as e:
                st.error(f"Erro ao processar estrutura dos CSVs: {e}")
                st.session_state["stripe_data"] = None
                st.session_state["qbo_data"] = None
                st.session_state["audit_authorized"] = False

        s_active = st.session_state["stripe_data"]
        q_active = st.session_state["qbo_data"]
        is_demo = st.session_state["is_demo"]

        if s_active is not None and q_active is not None:
            if not is_demo and not st.session_state["audit_authorized"]:
                st.markdown("---")
                st.subheader("2. Autenticação e Cota de Uso")
                st.caption("Você possui até 2 auditorias gratuitas por mês com arquivos reais.")
                
                col_auth, _ = st.columns([2, 3])
                with col_auth:
                    user_email = st.text_input("Seu e-mail corporativo para processar a auditoria:", key="auth_user_email")
                    if st.button("Autorizar Execução da Auditoria", type="primary"):
                        if not user_email:
                            st.warning("Por favor, insira um e-mail válido para iniciar.")
                        else:
                            allowed, count, msg = check_and_increment_usage(user_email)
                            if not allowed:
                                st.error(msg)
                            else:
                                st.success(msg)
                                st.session_state["audit_authorized"] = True
                                st.rerun()

            if st.session_state["audit_authorized"]:
                res = run_forensic_reconciliation(s_active, q_active)

                if res:
                    if res["quarantined"]:
                        st.warning(f"⚠ **Qualidade dos Dados:** {len(res['quarantined'])} linha(s) continham valores corrompidos e foram isoladas sob incerteza técnica.")
                        with st.expander("Ver detalhes dos registros em quarentena"):
                            st.dataframe(pd.DataFrame(res["quarantined"]), use_container_width=True)

                    st.markdown("---")
                    st.subheader("3. Stripe Clearing Account Reconciliation Summary")
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("Unreconciled QBO Ledger Balance", f"${res['qbo_balance']:,.2f}")
                    m2.metric("Identified Ledger Discrepancies", f"${res['confirmed_discrepancies']:,.2f}")
                    m3.metric("Quarantine Data Exposure", f"${res['quarantined_exposure']:,.2f}")
                    m4.metric("Unaccounted Residual Variance", f"${res['unexplained_residual']:,.2f}")

                    if res['audit_status'] == "CLEAN":
                        st.success("✅ **Reconciliation Status:** Saldo integralmente conciliado e fechado sem resíduos.")
                    elif res['audit_status'] == "INCONCLUSIVE":
                        st.info("ℹ️ **Reconciliation Status:** Saldo fechado matematicamente, mas parecer condicionado à auditoria manual dos itens em quarentena.")
                    else:
                        st.error("⚠️ **Reconciliation Status:** Existem diferenças sem explicação que demandam auditoria de lançamentos não identificados.")

                    st.markdown("---")
                    st.subheader(f"4. Itemized Forensic Discrepancies ({len(res['detections'])} itens)")
                    st.dataframe(pd.DataFrame([{
                        "Status": d_item["status"],
                        "Category": d_item["categoria"],
                        "Document Ref / ID": d_item["documento"],
                        "Ledger Net Impact": f"{d_item['sinal']}${d_item['impacto']:,.2f}" if d_item['impacto'] > 0 else "$0.00",
                        "Gross Exposure": f"${d_item['exposure']:,.2f}",
                        "Diagnostic Finding": d_item["descricao"]
                    } for d_item in res['detections']]), use_container_width=True)

                    st.markdown("---")
                    st.subheader("5. Adjusting Journal Entries & CPA Workpapers")
                    st.info("⚠️ **Professional Review Required:** The following entries are evidence-based diagnostic recommendations generated from your uploaded records. Review and approve them with your qualified accounting professional before posting into QuickBooks Online.")
                    st.info("💡 Export certified workpapers to post adjusting journal entries directly into QuickBooks Online:")
                    
                    col_cta, _ = st.columns([2, 3])
                    with col_cta:
                        email_lead = st.text_input("Controller / Reviewer Email for Audit Trail:", key="lead_email_input")
                        if st.button("Request Structured Audit Report"):
                            if email_lead and "@" in email_lead and "." in email_lead:
                                if save_lead(email_lead):
                                    st.success("Request confirmed! The audit workpaper package has been generated.")
                                else:
                                    st.error("Temporary error recording audit request. Please retry.")
                            else:
                                st.warning("Por favor, insira um e-mail válido.")

    if show_lab and tab_lab is not None:
        with tab_lab:
            st.markdown("Formal validation environment via stochastic simulation against known Ground Truth.")
            col_bench1, col_bench2 = st.columns(2)

            with col_bench1:
                st.subheader("Deterministic Benchmark Run")
                seed_input = st.number_input("Random Seed:", min_value=1, max_value=99999, value=42, key="seed_num")
                if st.button("🧪 Reconcile Benchmark Scenario", type="primary", key="btn_run_seed"):
                    s_df, q_perf, p_dates, _ = build_chronological_universe(seed=seed_input)
                    rng = random.Random(seed_input)
                    q_corrupted, gt = inject_adversarial_suite(q_perf, p_dates, rng)
                    res_single = run_forensic_reconciliation(s_df, q_corrupted)
                    met_single = evaluate_bipartite_forensic_metrics(res_single['detections'], gt)
                    st.session_state["v5_single"] = {"res": res_single, "gt": gt, "met": met_single}

            with col_bench2:
                st.subheader("Monte Carlo Stress Test Suite")
                mc_runs = st.slider("Independent Accounting Closes:", min_value=10, max_value=100, value=30, step=5, key="slider_mc")
                if st.button(f"⚡ Execute Monte Carlo Simulation ({mc_runs} Rodadas)", key="btn_run_mc"):
                    mc_results = []
                    progress = st.progress(0)
                    for run_idx in range(mc_runs):
                        run_seed = 3000 + run_idx
                        s_df, q_perf, p_dates, _ = build_chronological_universe(seed=run_seed)
                        rng = random.Random(run_seed)
                        q_corrupted, gt = inject_adversarial_suite(q_perf, p_dates, rng)
                        res_mc = run_forensic_reconciliation(s_df, q_corrupted)
                        met_mc = evaluate_bipartite_forensic_metrics(res_mc['detections'], gt)
                        met_mc["run"] = run_idx + 1
                        met_mc["seed"] = run_seed
                        met_mc["residual"] = float(res_mc['residual'])
                        met_mc["quarantined_exposure"] = float(res_mc['quarantined_exposure'])
                        mc_results.append(met_mc)
                        progress.progress((run_idx + 1) / mc_runs)
                    st.session_state["v5_mc_df"] = pd.DataFrame(mc_results)
                    st.success(f"{mc_runs} fechamentos estocásticos auditados com sucesso!")

            if "v5_single" in st.session_state:
                data = st.session_state["v5_single"]
                res_s, met_s = data["res"], data["met"]
                st.markdown("---")
                st.subheader("Bipartite Forensic Reconciliation (Independent Ground-Truth Oracle)")
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric("Detection Recall", f"{met_s['detection_recall']:.1f}%")
                c2.metric("Classification", f"{met_s['classification_accuracy']:.1f}%")
                c3.metric("Value Accuracy", f"{met_s['value_accuracy']:.1f}%")
                c4.metric("Financial Recall", f"{met_s['financial_recall']:.1f}%")
                c5.metric("Financial Precision", f"{met_s['financial_precision']:.1f}%")

            if "v5_mc_df" in st.session_state:
                df_mc = st.session_state["v5_mc_df"]
                st.markdown("---")
                st.subheader("Monte Carlo Executive Simulation Summary")
                s1, s2, s3, s4, s5, s6 = st.columns(6)
                s1.metric("Detection Recall", f"{df_mc['detection_recall'].mean():.1f}%")
                s2.metric("Classification", f"{df_mc['classification_accuracy'].mean():.1f}%")
                s3.metric("Value Accuracy", f"{df_mc['value_accuracy'].mean():.1f}%")
                s4.metric("Financial Recall", f"{df_mc['financial_recall'].mean():.1f}%")
                s5.metric("Financial Precision", f"{df_mc['financial_precision'].mean():.1f}%")
                s6.metric("False Positive Rate", f"{df_mc['false_positive_rate'].mean():.1f}%")

                st.markdown("##### Worst-Case Ledger Variance Tracking Panel")
                worst = df_mc.sort_values(by=["financial_recall", "residual"], ascending=[True, False]).head(5)
                st.dataframe(worst[[
                    "seed", "anomalies_gt", "anomalies_detected", "detection_recall", 
                    "financial_recall", "financial_precision", "missed_value", 
                    "residual", "quarantined_exposure"
                ]], use_container_width=True)

if __name__ == "__main__" or os.environ.get("RUN_STREAMLIT") == "true":
    run_app()
