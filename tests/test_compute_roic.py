"""Unit tests for `_compute_roic` — the heart of the ROIC calculation.

Covers:
  - NOPAT / Invested Capital path (preferred)
  - OCF / EV fallback when balance-sheet inputs are missing
  - Sector-based suppression (Financial Services, Real Estate)
  - Edge cases: negative invested capital, missing inputs, clamping,
    tax-rate override
"""

from __future__ import annotations

import pytest

from server.pipeline import _NOPAT_ROIC_INELIGIBLE_SECTORS, _compute_roic


# ─── NOPAT / Invested Capital path ──────────────────────────────────────────


class TestNopatPath:
    """When all balance-sheet inputs are present and IC > 0, use NOPAT/IC."""

    def test_msft_shape(self):
        # MSFT-like: ~130B EBIT, ~80B debt, 500B assets, 200B liab, 80B cash
        # Book equity = 500-200 = 300; IC = 80+300-80 = 300
        # NOPAT = 130 * 0.79 = 102.7; ROIC = 102.7 / 300 = 0.342
        roic = _compute_roic(
            ebit=130_000, total_debt=80_000,
            total_assets=500_000, total_liabilities=200_000,
            total_cash=80_000, ocf=120_000, ev=2_800_000,
        )
        assert roic == pytest.approx(0.3423, abs=1e-4)

    def test_nopat_takes_precedence_over_ocf_ev(self):
        # Even though OCF/EV would give 0.10, NOPAT path wins.
        roic = _compute_roic(
            ebit=10_000, total_debt=5_000,
            total_assets=50_000, total_liabilities=20_000,
            total_cash=5_000, ocf=10_000, ev=100_000,  # OCF/EV = 0.10
        )
        # IC = 5+30-5 = 30; NOPAT = 10*0.79 = 7.9; 7.9/30 = 0.2633
        assert roic == pytest.approx(0.2633, abs=1e-4)

    def test_negative_ebit_yields_negative_roic(self):
        roic = _compute_roic(
            ebit=-2_000, total_debt=5_000,
            total_assets=50_000, total_liabilities=20_000,
            total_cash=5_000, ocf=None, ev=None,
        )
        # IC = 30; NOPAT = -2*0.79 = -1.58; -1.58/30 = -0.0527
        assert roic == pytest.approx(-0.0527, abs=1e-4)

    def test_custom_tax_rate(self):
        # tax_rate=0.0 → NOPAT = EBIT
        roic = _compute_roic(
            ebit=10_000, total_debt=5_000,
            total_assets=50_000, total_liabilities=20_000,
            total_cash=5_000, ocf=None, ev=None,
            tax_rate=0.0,
        )
        # IC = 30; NOPAT = 10; 10/30 = 0.3333
        assert roic == pytest.approx(0.3333, abs=1e-4)

    def test_total_cash_none_treated_as_zero(self):
        roic = _compute_roic(
            ebit=10_000, total_debt=5_000,
            total_assets=50_000, total_liabilities=20_000,
            total_cash=None, ocf=None, ev=None,
        )
        # IC = 5+30-0 = 35; NOPAT = 7.9; 7.9/35 = 0.2257
        assert roic == pytest.approx(0.2257, abs=1e-4)


# ─── OCF / EV fallback ──────────────────────────────────────────────────────


class TestOcfEvFallback:
    """When NOPAT inputs are incomplete, fall back to OCF/EV."""

    def test_missing_ebit(self):
        roic = _compute_roic(
            ebit=None, total_debt=5_000,
            total_assets=50_000, total_liabilities=20_000,
            total_cash=5_000, ocf=10_000, ev=100_000,
        )
        assert roic == pytest.approx(0.10, abs=1e-4)

    def test_missing_total_debt(self):
        roic = _compute_roic(
            ebit=10_000, total_debt=None,
            total_assets=50_000, total_liabilities=20_000,
            total_cash=5_000, ocf=10_000, ev=100_000,
        )
        assert roic == pytest.approx(0.10, abs=1e-4)

    def test_missing_total_assets(self):
        roic = _compute_roic(
            ebit=10_000, total_debt=5_000,
            total_assets=None, total_liabilities=20_000,
            total_cash=5_000, ocf=10_000, ev=100_000,
        )
        assert roic == pytest.approx(0.10, abs=1e-4)

    def test_missing_total_liabilities(self):
        roic = _compute_roic(
            ebit=10_000, total_debt=5_000,
            total_assets=50_000, total_liabilities=None,
            total_cash=5_000, ocf=10_000, ev=100_000,
        )
        assert roic == pytest.approx(0.10, abs=1e-4)

    def test_negative_invested_capital_falls_back(self):
        # Cash-rich, debt-light: IC = 0 + (5000-1000) - 10000 = -6000 (negative)
        # NOPAT path skipped, falls back to OCF/EV.
        roic = _compute_roic(
            ebit=2_000, total_debt=0,
            total_assets=5_000, total_liabilities=1_000,
            total_cash=10_000, ocf=2_200, ev=20_000,
        )
        assert roic == pytest.approx(0.11, abs=1e-4)

    def test_zero_invested_capital_falls_back(self):
        # IC exactly zero — also falls back.
        roic = _compute_roic(
            ebit=2_000, total_debt=0,
            total_assets=5_000, total_liabilities=1_000,
            total_cash=4_000, ocf=2_200, ev=20_000,
        )
        assert roic == pytest.approx(0.11, abs=1e-4)


# ─── Sector exclusions ──────────────────────────────────────────────────────


class TestSectorExclusion:
    """Financial Services and Real Estate suppress ROIC entirely (return None)."""

    def test_ineligible_set_contents(self):
        assert _NOPAT_ROIC_INELIGIBLE_SECTORS == {"Financial Services", "Real Estate"}

    def test_financial_services_returns_none_with_full_inputs(self):
        # Even with all NOPAT inputs available, financials return None —
        # because the formula doesn't apply to banks.
        roic = _compute_roic(
            ebit=50_000, total_debt=300_000,
            total_assets=3_500_000, total_liabilities=3_200_000,
            total_cash=1_000_000, ocf=80_000, ev=1_200_000,
            sector="Financial Services",
        )
        assert roic is None

    def test_real_estate_returns_none_with_full_inputs(self):
        roic = _compute_roic(
            ebit=10_000, total_debt=20_000,
            total_assets=50_000, total_liabilities=30_000,
            total_cash=2_000, ocf=12_000, ev=80_000,
            sector="Real Estate",
        )
        assert roic is None

    def test_financial_services_returns_none_even_with_only_ocf_ev(self):
        # Don't even let the OCF/EV fallback fire — banks have negative OCF
        # from deposit changes which gives misleading values.
        roic = _compute_roic(
            ebit=None, total_debt=None,
            total_assets=None, total_liabilities=None,
            total_cash=None, ocf=-150_000, ev=300_000,
            sector="Financial Services",
        )
        assert roic is None

    def test_eligible_sector_string_uses_nopat(self):
        roic = _compute_roic(
            ebit=10_000, total_debt=5_000,
            total_assets=50_000, total_liabilities=20_000,
            total_cash=5_000, ocf=None, ev=None,
            sector="Technology",
        )
        # IC = 30; NOPAT = 7.9; 0.2633
        assert roic == pytest.approx(0.2633, abs=1e-4)

    def test_empty_sector_string_uses_nopat(self):
        # Default empty sector should not trigger exclusion.
        roic = _compute_roic(
            ebit=10_000, total_debt=5_000,
            total_assets=50_000, total_liabilities=20_000,
            total_cash=5_000, ocf=None, ev=None,
        )
        assert roic == pytest.approx(0.2633, abs=1e-4)


# ─── No data at all ─────────────────────────────────────────────────────────


class TestMissingData:
    def test_all_none_returns_none(self):
        assert _compute_roic(
            ebit=None, total_debt=None,
            total_assets=None, total_liabilities=None,
            total_cash=None, ocf=None, ev=None,
        ) is None

    def test_only_ocf_no_ev_returns_none(self):
        assert _compute_roic(
            ebit=None, total_debt=None,
            total_assets=None, total_liabilities=None,
            total_cash=None, ocf=10_000, ev=None,
        ) is None

    def test_only_ev_no_ocf_returns_none(self):
        assert _compute_roic(
            ebit=None, total_debt=None,
            total_assets=None, total_liabilities=None,
            total_cash=None, ocf=None, ev=100_000,
        ) is None

    def test_zero_ev_falls_through_to_none(self):
        # ev=0 fails the `ev > 0` guard; no other path → None.
        assert _compute_roic(
            ebit=None, total_debt=None,
            total_assets=None, total_liabilities=None,
            total_cash=None, ocf=10_000, ev=0,
        ) is None


# ─── Clamping ───────────────────────────────────────────────────────────────


class TestClamping:
    """Result is clamped to [-5.0, 5.0] to suppress noise from tiny IC."""

    def test_extreme_positive_clamped(self):
        # Tiny IC → exploding ROIC → clamped to 5.0.
        roic = _compute_roic(
            ebit=1_000_000, total_debt=1,
            total_assets=10, total_liabilities=8,
            total_cash=0, ocf=None, ev=None,
        )
        # IC = 1+2-0 = 3; NOPAT = 790_000; 790_000/3 = 263_333 → clamped
        assert roic == 5.0

    def test_extreme_negative_clamped(self):
        roic = _compute_roic(
            ebit=-1_000_000, total_debt=1,
            total_assets=10, total_liabilities=8,
            total_cash=0, ocf=None, ev=None,
        )
        assert roic == -5.0

    def test_ocf_ev_fallback_also_clamped(self):
        # Tiny EV → exploding OCF/EV → clamped.
        roic = _compute_roic(
            ebit=None, total_debt=None,
            total_assets=None, total_liabilities=None,
            total_cash=None, ocf=1_000_000, ev=1,
        )
        assert roic == 5.0
