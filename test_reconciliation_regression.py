import random
from decimal import Decimal

import pandas as pd
import pytest

import app


# ============================================================
# REGRESSÃO PERMANENTE — STRIPE/QBO CLOSE AUDITOR v0.1.0
# ============================================================
#
# Duas camadas:
#   1. Smoke: 20 seeds, feedback rápido durante desenvolvimento.
#   2. Full: 100 seeds, baseline completo para pre-push/CI.
#
# O objetivo desta suíte NÃO é testar implementação interna.
# Ela congela o comportamento observável do motor:
#   - fechamento algébrico;
#   - recuperação financeira das anomalias injetadas;
#   - ausência de falso positivo no universo limpo;
#   - tratamento direcional de payout negativo.
#
# IMPORTANTE:
# Os nomes das chaves abaixo seguem o contrato FINAL esperado
# do motor v0.1.0. Se app.py ainda expuser apenas "residual" em
# vez de "unexplained_residual", o teste deve falhar: isso indica
# divergência de contrato entre motor e suíte de regressão.
# ============================================================


def _assert_zero(value, label):
    assert value == Decimal("0.00"), f"{label}: esperado 0.00, obtido {value}"


def _build_adversarial_case(seed):
    """Constrói exatamente o mesmo caso usado pela bancada científica."""
    s_df, q_perf, p_dates, _ = app.build_chronological_universe(seed=seed)
    rng = random.Random(seed)
    q_corr, gt = app.inject_adversarial_suite(q_perf, p_dates, rng)
    return s_df, q_corr, gt


# ============================================================
# 1. BATERIA RÁPIDA — 20 SEEDS
# ============================================================

@pytest.mark.parametrize("seed", range(1000, 1020))
def test_fast_regression_seeds(seed):
    """
    Smoke test de invariantes.

    Para cada seed:
      - o motor deve executar;
      - U deve fechar exatamente em zero;
      - Financial Recall deve ser 100%;
      - Financial Precision deve ser 100%.
    """
    s_df, q_corr, gt = _build_adversarial_case(seed)

    res = app.run_forensic_reconciliation(s_df, q_corr)
    assert res is not None, f"Falha de execução no seed {seed}"

    assert "unexplained_residual" in res, (
        "Contrato v0.1.0 ausente: run_forensic_reconciliation() "
        "deve retornar 'unexplained_residual'."
    )

    assert "delta_balance" in res, (
        "Contrato v0.1.0 ausente: run_forensic_reconciliation() "
        "deve retornar 'delta_balance'."
    )

    assert "net_explained" in res
    assert "quarantined_exposure" in res

    _assert_zero(
        res["unexplained_residual"],
        f"Quebra de resíduo no seed {seed}"
    )

    metrics = app.evaluate_bipartite_forensic_metrics(
        res["detections"], gt
    )

    assert metrics["financial_recall"] == 100.0, (
        f"Queda no Financial Recall no seed {seed}: "
        f"{metrics['financial_recall']:.4f}%"
    )

    assert metrics["financial_precision"] == 100.0, (
        f"Queda na Financial Precision no seed {seed}: "
        f"{metrics['financial_precision']:.4f}%"
    )


# ============================================================
# 2. BATERIA COMPLETA — 100 SEEDS
# ============================================================

@pytest.mark.slow
@pytest.mark.parametrize("seed", range(1000, 1100))
def test_full_stochastic_baseline_100_seeds(seed):
    """
    Baseline estocástico congelado da v0.1.0.

    Este teste é o guardião permanente da vitória 100/100:
      - fechamento algébrico;
      - Financial Recall;
      - Financial Precision;
      - Value Accuracy.

    Rodar no pre-push/CI, não em cada ciclo curto de edição.
    """
    s_df, q_corr, gt = _build_adversarial_case(seed)

    res = app.run_forensic_reconciliation(s_df, q_corr)
    assert res is not None, f"Falha de execução no seed {seed}"

    _assert_zero(
        res["unexplained_residual"],
        f"Quebra de resíduo no seed {seed}"
    )

    metrics = app.evaluate_bipartite_forensic_metrics(
        res["detections"], gt
    )

    assert metrics["financial_recall"] == 100.0, (
        f"Financial Recall < 100% no seed {seed}: "
        f"{metrics['financial_recall']:.4f}%"
    )

    assert metrics["financial_precision"] == 100.0, (
        f"Financial Precision < 100% no seed {seed}: "
        f"{metrics['financial_precision']:.4f}%"
    )

    assert metrics["value_accuracy"] == 100.0, (
        f"Value Accuracy < 100% no seed {seed}: "
        f"{metrics['value_accuracy']:.4f}%"
    )


# ============================================================
# 3. UNIVERSO LIMPO — ZERO FALSO POSITIVO
# ============================================================

def test_clean_state_invariance():
    """
    Universo canônico sem nenhuma anomalia injetada.

    Espera-se:
      - Delta_B = 0.00
      - U = 0.00
      - status = CLEAN
      - zero detecções.
    """
    s_df, q_perf, _, _ = app.build_chronological_universe(seed=1001)

    res = app.run_forensic_reconciliation(
        s_df,
        pd.DataFrame(q_perf),
    )

    assert res is not None

    _assert_zero(res["delta_balance"], "Delta_B do universo limpo")
    _assert_zero(
        res["unexplained_residual"],
        "U do universo limpo"
    )

    assert res["audit_status"] == "CLEAN"

    assert len(res["detections"]) == 0, (
        "Falsos positivos detectados no universo limpo: "
        f"{res['detections']}"
    )


# ============================================================
# 4. DIREÇÃO DO PAYOUT NEGATIVO
# ============================================================

def test_negative_payout_directional_invariance():
    """
    Regra específica para o caso de payout com saldo negativo.

    Seed 1063 é congelado como caso de regressão porque exercita
    a direção contábil do payout negativo.

    O teste não aceita que uma diferença puramente direcional
    seja classificada como "Erro de Valor / Digitação".
    """
    seed = 1063
    s_df, q_corr, gt = _build_adversarial_case(seed)

    res = app.run_forensic_reconciliation(s_df, q_corr)

    assert res is not None
    _assert_zero(
        res["unexplained_residual"],
        f"U no seed direcional {seed}"
    )

    tr_5119_errors = [
        d
        for d in res["detections"]
        if "TR-5119" in str(d.get("documento", ""))
        and d["categoria"] == "Erro de Valor / Digitação"
    ]

    assert len(tr_5119_errors) == 0, (
        "Payout negativo TR-5119 foi erroneamente classificado "
        "como discrepância de valor."
    )


# ============================================================
# 5. INVARIANTE DO ORÁCULO
# ============================================================

def test_oracle_metrics_are_bounded():
    """
    Métricas do avaliador nunca podem ultrapassar 100%.

    Este teste protege contra regressões no próprio avaliador,
    especialmente em cálculos de Financial Recall/Precision.
    """
    s_df, q_corr, gt = _build_adversarial_case(1000)
    res = app.run_forensic_reconciliation(s_df, q_corr)

    metrics = app.evaluate_bipartite_forensic_metrics(
        res["detections"], gt
    )

    for key in (
        "detection_recall",
        "classification_accuracy",
        "value_accuracy",
        "financial_recall",
        "financial_precision",
    ):
        assert 0.0 <= metrics[key] <= 100.0, (
            f"Métrica {key} fora do intervalo [0,100]: {metrics[key]}"
        )
