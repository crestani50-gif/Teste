import random
import pandas as pd
import app

run_seed = 3022  # Seed exata do caso crítico (run_idx = 22)

print("=" * 80)
print(f"AUTÓPSIA FORENSE DETERMINÍSTICA — SEED {run_seed}")
print("=" * 80)

# 1. Reconstrói o universo idêntico ao Monte Carlo
s_df, q_perfect, p_dates, _ = app.build_chronological_universe(seed=run_seed)
rng = random.Random(run_seed)
q_corrupted, gt = app.inject_adversarial_suite(q_perfect, p_dates, rng)

# 2. Executa a reconciliação real
res = app.run_forensic_reconciliation(s_df, q_corrupted)
det = res['detections']
met = app.evaluate_bipartite_forensic_metrics(det, gt)

print("\n--- MÉTRICAS CONSOLIDADAS ---")
for k in ['detection_recall', 'financial_recall', 'financial_precision', 'missed_value']:
    print(f"  {k}: {met.get(k)}")
print(f"  Residual: {res.get('residual')}")

print("\n" + "=" * 80)
print("GROUND TRUTH (INJETADAS)")
print("=" * 80)
df_gt = pd.DataFrame(gt)
print(df_gt.to_string())

print("\n" + "=" * 80)
print("DETECÇÕES DO AUDITOR")
print("=" * 80)
df_det = pd.DataFrame(det)
print(df_det.to_string())
print("\n" + "=" * 80)
print("DECOMPOSIÇÃO DO FECHAMENTO CONTÁBIL")
print("=" * 80)
print(f"QBO Balance (total_qbo_net) : {res.get('qbo_balance')}")
print(f"Net Explained (detecções)   : {res.get('net_explained')}")
print(f"Quarantined Amount          : {res.get('quarantined_amount')}")
print(f"Unexplained Residual        : {res.get('unexplained_residual')}")
print(f"Residual Reportado          : {res.get('residual')}")
print(f"Audit Status                : {res.get('audit_status')}")