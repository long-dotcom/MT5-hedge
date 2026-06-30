"""rename venue-specific columns to generic leg_a/leg_b naming

Revision ID: 001_rename_venue_columns
Revises:
Create Date: 2026-06-30
"""

from alembic import op

revision = "001_rename_venue_columns"
down_revision = None
branch_labels = None
depends_on = None


def _rename_column(table: str, old: str, new: str) -> None:
    op.execute(f'ALTER TABLE "{table}" RENAME COLUMN "{old}" TO "{new}"')


def _update_direction_values(table: str) -> None:
    op.execute(
        f"UPDATE \"{table}\" SET direction = 'long_leg_a_short_leg_b' "
        f"WHERE direction = 'long_hyperliquid_short_mt5'"
    )
    op.execute(
        f"UPDATE \"{table}\" SET direction = 'long_leg_b_short_leg_a' "
        f"WHERE direction = 'long_mt5_short_hyperliquid'"
    )


def upgrade() -> None:
    # ── spread_current ─────────────────────────────────────────────────────
    _rename_column("spread_current", "hyperliquid_bid", "leg_a_bid")
    _rename_column("spread_current", "hyperliquid_ask", "leg_a_ask")
    _rename_column("spread_current", "mt5_bid", "leg_b_bid")
    _rename_column("spread_current", "mt5_ask", "leg_b_ask")
    _rename_column("spread_current", "hyperliquid_quantity", "leg_a_quantity")
    _rename_column("spread_current", "mt5_quantity", "leg_b_quantity")
    _update_direction_values("spread_current")

    # ── spread_direction_current ────────────────────────────────────────────
    _rename_column("spread_direction_current", "hyperliquid_bid", "leg_a_bid")
    _rename_column("spread_direction_current", "hyperliquid_ask", "leg_a_ask")
    _rename_column("spread_direction_current", "mt5_bid", "leg_b_bid")
    _rename_column("spread_direction_current", "mt5_ask", "leg_b_ask")
    _rename_column("spread_direction_current", "hyperliquid_quantity", "leg_a_quantity")
    _rename_column("spread_direction_current", "mt5_quantity", "leg_b_quantity")
    _update_direction_values("spread_direction_current")

    # ── spread_snapshots ────────────────────────────────────────────────────
    _rename_column("spread_snapshots", "hyperliquid_bid", "leg_a_bid")
    _rename_column("spread_snapshots", "hyperliquid_ask", "leg_a_ask")
    _rename_column("spread_snapshots", "mt5_bid", "leg_b_bid")
    _rename_column("spread_snapshots", "mt5_ask", "leg_b_ask")
    _rename_column("spread_snapshots", "hyperliquid_quantity", "leg_a_quantity")
    _rename_column("spread_snapshots", "mt5_quantity", "leg_b_quantity")
    _update_direction_values("spread_snapshots")

    # ── arbitrage_opportunities ─────────────────────────────────────────────
    _rename_column("arbitrage_opportunities", "hyperliquid_quantity", "leg_a_quantity")
    _rename_column("arbitrage_opportunities", "mt5_quantity", "leg_b_quantity")
    _rename_column("arbitrage_opportunities", "trigger_hyperliquid_bid", "trigger_leg_a_bid")
    _rename_column("arbitrage_opportunities", "trigger_hyperliquid_ask", "trigger_leg_a_ask")
    _rename_column("arbitrage_opportunities", "trigger_mt5_bid", "trigger_leg_b_bid")
    _rename_column("arbitrage_opportunities", "trigger_mt5_ask", "trigger_leg_b_ask")
    _update_direction_values("arbitrage_opportunities")

    # ── hedge_groups ────────────────────────────────────────────────────────
    _rename_column("hedge_groups", "hyperliquid_quantity", "leg_a_quantity")
    _rename_column("hedge_groups", "mt5_quantity", "leg_b_quantity")
    _rename_column("hedge_groups", "trigger_hyperliquid_bid", "trigger_leg_a_bid")
    _rename_column("hedge_groups", "trigger_hyperliquid_ask", "trigger_leg_a_ask")
    _rename_column("hedge_groups", "trigger_mt5_bid", "trigger_leg_b_bid")
    _rename_column("hedge_groups", "trigger_mt5_ask", "trigger_leg_b_ask")
    _update_direction_values("hedge_groups")

    # ── symbol_mappings ─────────────────────────────────────────────────────
    _rename_column("symbol_mappings", "hyperliquid_symbol", "leg_a_venue_symbol")
    _rename_column("symbol_mappings", "hyperliquid_min_base_size", "leg_a_min_base_size")
    _rename_column("symbol_mappings", "hyperliquid_min_notional", "leg_a_min_notional")

    # ── strategy_settings ───────────────────────────────────────────────────
    _rename_column("strategy_settings", "paper_hyperliquid_latency_ms_min", "paper_leg_a_latency_ms_min")
    _rename_column("strategy_settings", "paper_hyperliquid_latency_ms_max", "paper_leg_a_latency_ms_max")
    _rename_column("strategy_settings", "paper_mt5_latency_ms_min", "paper_leg_b_latency_ms_min")
    _rename_column("strategy_settings", "paper_mt5_latency_ms_max", "paper_leg_b_latency_ms_max")


def downgrade() -> None:
    # ── strategy_settings ───────────────────────────────────────────────────
    _rename_column("strategy_settings", "paper_leg_a_latency_ms_min", "paper_hyperliquid_latency_ms_min")
    _rename_column("strategy_settings", "paper_leg_a_latency_ms_max", "paper_hyperliquid_latency_ms_max")
    _rename_column("strategy_settings", "paper_leg_b_latency_ms_min", "paper_mt5_latency_ms_min")
    _rename_column("strategy_settings", "paper_leg_b_latency_ms_max", "paper_mt5_latency_ms_max")

    # ── symbol_mappings ─────────────────────────────────────────────────────
    _rename_column("symbol_mappings", "leg_a_venue_symbol", "hyperliquid_symbol")
    _rename_column("symbol_mappings", "leg_a_min_base_size", "hyperliquid_min_base_size")
    _rename_column("symbol_mappings", "leg_a_min_notional", "hyperliquid_min_notional")

    # ── hedge_groups ────────────────────────────────────────────────────────
    _rename_column("hedge_groups", "leg_a_quantity", "hyperliquid_quantity")
    _rename_column("hedge_groups", "leg_b_quantity", "mt5_quantity")
    _rename_column("hedge_groups", "trigger_leg_a_bid", "trigger_hyperliquid_bid")
    _rename_column("hedge_groups", "trigger_leg_a_ask", "trigger_hyperliquid_ask")
    _rename_column("hedge_groups", "trigger_leg_b_bid", "trigger_mt5_bid")
    _rename_column("hedge_groups", "trigger_leg_b_ask", "trigger_mt5_ask")
    op.execute(
        "UPDATE \"hedge_groups\" SET direction = 'long_hyperliquid_short_mt5' "
        "WHERE direction = 'long_leg_a_short_leg_b'"
    )
    op.execute(
        "UPDATE \"hedge_groups\" SET direction = 'long_mt5_short_hyperliquid' "
        "WHERE direction = 'long_leg_b_short_leg_a'"
    )

    # ── arbitrage_opportunities ─────────────────────────────────────────────
    _rename_column("arbitrage_opportunities", "leg_a_quantity", "hyperliquid_quantity")
    _rename_column("arbitrage_opportunities", "leg_b_quantity", "mt5_quantity")
    _rename_column("arbitrage_opportunities", "trigger_leg_a_bid", "trigger_hyperliquid_bid")
    _rename_column("arbitrage_opportunities", "trigger_leg_a_ask", "trigger_hyperliquid_ask")
    _rename_column("arbitrage_opportunities", "trigger_leg_b_bid", "trigger_mt5_bid")
    _rename_column("arbitrage_opportunities", "trigger_leg_b_ask", "trigger_mt5_ask")
    op.execute(
        "UPDATE \"arbitrage_opportunities\" SET direction = 'long_hyperliquid_short_mt5' "
        "WHERE direction = 'long_leg_a_short_leg_b'"
    )
    op.execute(
        "UPDATE \"arbitrage_opportunities\" SET direction = 'long_mt5_short_hyperliquid' "
        "WHERE direction = 'long_leg_b_short_leg_a'"
    )

    # ── spread_snapshots ────────────────────────────────────────────────────
    _rename_column("spread_snapshots", "leg_a_bid", "hyperliquid_bid")
    _rename_column("spread_snapshots", "leg_a_ask", "hyperliquid_ask")
    _rename_column("spread_snapshots", "leg_b_bid", "mt5_bid")
    _rename_column("spread_snapshots", "leg_b_ask", "mt5_ask")
    _rename_column("spread_snapshots", "leg_a_quantity", "hyperliquid_quantity")
    _rename_column("spread_snapshots", "leg_b_quantity", "mt5_quantity")
    op.execute(
        "UPDATE \"spread_snapshots\" SET direction = 'long_hyperliquid_short_mt5' "
        "WHERE direction = 'long_leg_a_short_leg_b'"
    )
    op.execute(
        "UPDATE \"spread_snapshots\" SET direction = 'long_mt5_short_hyperliquid' "
        "WHERE direction = 'long_leg_b_short_leg_a'"
    )

    # ── spread_direction_current ────────────────────────────────────────────
    _rename_column("spread_direction_current", "leg_a_bid", "hyperliquid_bid")
    _rename_column("spread_direction_current", "leg_a_ask", "hyperliquid_ask")
    _rename_column("spread_direction_current", "leg_b_bid", "mt5_bid")
    _rename_column("spread_direction_current", "leg_b_ask", "mt5_ask")
    _rename_column("spread_direction_current", "leg_a_quantity", "hyperliquid_quantity")
    _rename_column("spread_direction_current", "leg_b_quantity", "mt5_quantity")
    op.execute(
        "UPDATE \"spread_direction_current\" SET direction = 'long_hyperliquid_short_mt5' "
        "WHERE direction = 'long_leg_a_short_leg_b'"
    )
    op.execute(
        "UPDATE \"spread_direction_current\" SET direction = 'long_mt5_short_hyperliquid' "
        "WHERE direction = 'long_leg_b_short_leg_a'"
    )

    # ── spread_current ─────────────────────────────────────────────────────
    _rename_column("spread_current", "leg_a_bid", "hyperliquid_bid")
    _rename_column("spread_current", "leg_a_ask", "hyperliquid_ask")
    _rename_column("spread_current", "leg_b_bid", "mt5_bid")
    _rename_column("spread_current", "leg_b_ask", "mt5_ask")
    _rename_column("spread_current", "leg_a_quantity", "hyperliquid_quantity")
    _rename_column("spread_current", "leg_b_quantity", "mt5_quantity")
    op.execute(
        "UPDATE \"spread_current\" SET direction = 'long_hyperliquid_short_mt5' "
        "WHERE direction = 'long_leg_a_short_leg_b'"
    )
    op.execute(
        "UPDATE \"spread_current\" SET direction = 'long_mt5_short_hyperliquid' "
        "WHERE direction = 'long_leg_b_short_leg_a'"
    )
