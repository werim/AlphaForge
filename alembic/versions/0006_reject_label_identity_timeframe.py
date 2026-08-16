"""Add reject decision identity and timeframe-aware pending labels.

Revision ID: 0006_reject_label_identity_timeframe
Revises: 0005_core_identifier_normalization
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_reject_label_identity_timeframe"
down_revision = "0005_core_identifier_normalization"
branch_labels = None
depends_on = None


def _add(table: str, name: str, column: sa.Column) -> None:
    inspector=sa.inspect(op.get_bind())
    if inspector.has_table(table) and name not in {c['name'] for c in inspector.get_columns(table)}:
        op.add_column(table,column)


def upgrade() -> None:
    _add('rejected_signal_reviews','reject_decision_id',sa.Column('reject_decision_id',sa.String()))
    _add('rejected_signal_reviews','execution_invalidated',sa.Column('execution_invalidated',sa.Boolean()))
    _add('rejected_signal_reviews','outcome_ambiguous',sa.Column('outcome_ambiguous',sa.Boolean()))
    _add('rejected_signal_reviews','evidence_complete',sa.Column('evidence_complete',sa.Boolean()))
    _add('burnin_pending_reject_labels','timeframe',sa.Column('timeframe',sa.String()))
    _add('burnin_pending_reject_labels','horizon_bars',sa.Column('horizon_bars',sa.Integer()))
    _add('burnin_pending_reject_labels','claim_token',sa.Column('claim_token',sa.String()))
    _add('burnin_pending_reject_labels','claimed_at',sa.Column('claimed_at',sa.String()))
    inspector=sa.inspect(op.get_bind())
    if inspector.has_table('rejected_signal_reviews'):
        indexes={i['name'] for i in inspector.get_indexes('rejected_signal_reviews')}
        if 'ux_rejected_reviews_decision_id' not in indexes:
            op.create_index('ux_rejected_reviews_decision_id','rejected_signal_reviews',['reject_decision_id'],unique=True,sqlite_where=sa.text('reject_decision_id IS NOT NULL'))


def downgrade() -> None:
    pass
