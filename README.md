# Forensic Ledger Reconciliation: Stripe × QuickBooks Online (QBO)

[![Pytest Suite](https://img.shields.io/badge/pytest-123%2F123%20passed-brightgreen.svg)](#test-suite--forensic-verification)
[![Python Version](https://img.shields.io/badge/python-3.10%2B%20%7C%203.14-blue.svg)](https://www.python.org/)
[![Precision Standard](https://img.shields.io/badge/arithmetic-Decimal%20ROUND__HALF__UP-orange.svg)](#forensic-engine-invariants)

An enterprise-grade forensic accounting tool designed to identify, explain, and reconcile discrepancies between **Stripe Balance History** exports and **QuickBooks Online Stripe Clearing Accounts** before month-end close.

Built with strict double-entry invariants, deterministic bipartite detection, automated data quarantine, and exportable CPA-ready Adjusting Journal Entries (AJEs).

---

## Key Capabilities

* **Deterministic Double-Entry Reconciliation:** Tracks initial ledger balances, batch gross disbursements, omitted processing fees, unposted refunds, disputed balance adjustments, duplicate entries, and sign inversion errors.
* **Forensic Precision Engine:** Built exclusively on Python `Decimal` arithmetic (`0.01` quantization with `ROUND_HALF_UP`) to prevent floating-point contamination.
* **Automated Data Quarantine:** Isolates malformed or corrupted rows without crashing the runtime or polluting reconciliation balances.
* **Epistemological Guardrail:** Enforces an `INCONCLUSIVE` opinion whenever residual variance exists or quarantined items remain uninspected.
* **CPA-Ready AJE Export:** Produces formatted CSV workpapers mapped to GAAP/double-entry standards for direct QBO posting.
* **Usage Quotas & Governance:** Beta gatekeeper with automated monthly quota management (3 complimentary production audits/month) and administrative exemptions.

---

## Architectural Flow

```text
[ Stripe CSV Export ]       [ QBO Clearing CSV ]
          │                         │
          ▼                         ▼
┌──────────────────────────────────────────────────┐
│  Data Ingestion & Dialect / Encoding Normalizer   │
└──────────────────────────────────────────────────┘
          │                         │
          ├───────── [Corrupted] ──►│ Forensic Quarantine
          │                         ▼ (Isolated under technical uncertainty)
          ▼
┌──────────────────────────────────────────────────┐
│      Bipartite Forensic Matching Engine           │
│   - Settlement window timing variances           │
│   - Duplicate payout identification              │
│   - Fee & refund omissions                       │
│   - Sign inversion (Dr/Cr flip) detection        │
└──────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────┐
│          Closed-Form Algebraic Tie               │
│ QBO Unreconciled = Identified Discrepancies + 0   │
└──────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────┐
│        CPA Workpapers & AJE CSV Generator        │
└──────────────────────────────────────────────────┘

Installation & Local Execution
1. Clone & Set Up Environment
Bash

git clone [https://github.com/seu-usuario/Teste.git](https://github.com/seu-usuario/Teste.git)
cd Teste
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

2. Run the Streamlit Application
Bash

streamlit run app.py

Open http://localhost:8501 in your browser.
Test Suite & Forensic Verification

The reconciliation engine is guarded by an adversarial Monte Carlo test suite verifying 100 seeds with synthetic injections of timing shifts, fee drops, duplicate payouts, and sign flips:
Bash

# Run the fast regression test suite (20 seeds)
pytest -k "test_fast_regression_seeds"

# Run the complete stochastic baseline (123 tests)
pytest

Guaranteed Invariants:

    unexplained_residual == Decimal("0.00")

    financial_recall == 100.0%

    financial_precision == 100.0%

    value_accuracy == 100.0%

Regulatory & Advisory Notice

This software is an assistive analytical tool designed to identify potential discrepancies and reconciliation issues between financial records. Diagnostic findings and proposed adjusting journal entries are provided for informational and operational purposes only and do not constitute tax, legal, accounting, audit, or licensed professional advice. All findings and proposed entries require validation by a qualified accounting professional prior to posting.
