"""baseline schema (SQLAlchemy models as of first release)

Expression indexes (lower(name)) are not reflected by SQLite, so autogenerate skips them;
they are created explicitly below.

Revision ID: 0001
Revises: 
Create Date: 2026-10-10 15:21:28.804243
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('commodities',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('symbol', sa.String(length=80), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_commodities')),
    sa.UniqueConstraint('symbol', name=op.f('uq_commodities_symbol'))
    )
    op.create_table('meta',
    sa.Column('key', sa.String(length=80), nullable=False),
    sa.Column('value', sa.String(), nullable=True),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_meta'))
    )
    op.create_table('systems',
    sa.Column('id64', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=False, nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('x', sa.Float(), nullable=True),
    sa.Column('y', sa.Float(), nullable=True),
    sa.Column('z', sa.Float(), nullable=True),
    sa.Column('population', sa.BigInteger(), nullable=True),
    sa.Column('allegiance', sa.String(length=40), nullable=True),
    sa.Column('government', sa.String(length=40), nullable=True),
    sa.Column('security', sa.String(length=40), nullable=True),
    sa.Column('economy', sa.String(length=40), nullable=True),
    sa.Column('second_economy', sa.String(length=40), nullable=True),
    sa.Column('controlling_faction', sa.String(length=120), nullable=True),
    sa.Column('conflicts', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('factions_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source', sa.String(length=16), server_default='eddn', nullable=False),
    sa.PrimaryKeyConstraint('id64', name=op.f('pk_systems'))
    )
    op.create_table('faction_history',
    sa.Column('system_id64', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('faction', sa.String(length=120), nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('influence', sa.Float(), nullable=True),
    sa.Column('state', sa.String(length=40), nullable=True),
    sa.ForeignKeyConstraint(['system_id64'], ['systems.id64'], name=op.f('fk_faction_history_system_id64_systems'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('system_id64', 'faction', 'ts', name=op.f('pk_faction_history'))
    )
    with op.batch_alter_table('faction_history', schema=None) as batch_op:
        batch_op.create_index('ix_faction_history_ts', ['ts'], unique=False)

    op.create_table('factions',
    sa.Column('system_id64', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('government', sa.String(length=40), nullable=True),
    sa.Column('allegiance', sa.String(length=40), nullable=True),
    sa.Column('influence', sa.Float(), nullable=True),
    sa.Column('state', sa.String(length=40), nullable=True),
    sa.Column('happiness', sa.String(length=40), nullable=True),
    sa.Column('active_states', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('pending_states', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('recovering_states', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('is_controlling', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('source', sa.String(length=16), server_default='eddn', nullable=False),
    sa.CheckConstraint('influence IS NULL OR (influence >= 0 AND influence <= 1)', name=op.f('ck_factions_influence_range')),
    sa.ForeignKeyConstraint(['system_id64'], ['systems.id64'], name=op.f('fk_factions_system_id64_systems'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('system_id64', 'name', name=op.f('pk_factions'))
    )
    op.create_table('markets',
    sa.Column('market_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=False, nullable=False),
    sa.Column('station', sa.String(length=120), nullable=False),
    sa.Column('system_name', sa.String(length=120), nullable=False),
    sa.Column('system_id64', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('station_type', sa.String(length=40), nullable=True),
    sa.Column('is_carrier', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('economies', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('prohibited', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('source', sa.String(length=16), server_default='eddn', nullable=False),
    sa.ForeignKeyConstraint(['system_id64'], ['systems.id64'], name=op.f('fk_markets_system_id64_systems'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('market_id', name=op.f('pk_markets'))
    )
    with op.batch_alter_table('markets', schema=None) as batch_op:
        batch_op.create_index('ix_markets_updated_at', ['updated_at'], unique=False)

    op.create_table('commodity_prices',
    sa.Column('market_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('commodity_id', sa.Integer(), nullable=False),
    sa.Column('buy_price', sa.Integer(), nullable=True),
    sa.Column('sell_price', sa.Integer(), nullable=True),
    sa.Column('mean_price', sa.Integer(), nullable=True),
    sa.Column('stock', sa.BigInteger(), nullable=True),
    sa.Column('demand', sa.BigInteger(), nullable=True),
    sa.Column('stock_bracket', sa.Integer(), nullable=True),
    sa.Column('demand_bracket', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['commodity_id'], ['commodities.id'], name=op.f('fk_commodity_prices_commodity_id_commodities')),
    sa.ForeignKeyConstraint(['market_id'], ['markets.market_id'], name=op.f('fk_commodity_prices_market_id_markets'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('market_id', 'commodity_id', name=op.f('pk_commodity_prices'))
    )
    with op.batch_alter_table('commodity_prices', schema=None) as batch_op:
        batch_op.create_index('ix_commodity_prices_commodity_id', ['commodity_id'], unique=False)

    op.create_table('price_history',
    sa.Column('market_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('commodity_id', sa.Integer(), nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('buy_price', sa.Integer(), nullable=True),
    sa.Column('sell_price', sa.Integer(), nullable=True),
    sa.Column('stock', sa.BigInteger(), nullable=True),
    sa.Column('demand', sa.BigInteger(), nullable=True),
    sa.ForeignKeyConstraint(['commodity_id'], ['commodities.id'], name=op.f('fk_price_history_commodity_id_commodities')),
    sa.ForeignKeyConstraint(['market_id'], ['markets.market_id'], name=op.f('fk_price_history_market_id_markets'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('market_id', 'commodity_id', 'ts', name=op.f('pk_price_history'))
    )
    with op.batch_alter_table('price_history', schema=None) as batch_op:
        batch_op.create_index('ix_price_history_ts', ['ts'], unique=False)


    op.create_index('ix_systems_name_lower', 'systems', [sa.text('lower(name)')])
    op.create_index('ix_factions_name_lower', 'factions', [sa.text('lower(name)')])
    op.create_index('ix_markets_station_lower', 'markets', [sa.text('lower(station)')])
    op.create_index('ix_markets_system_name_lower', 'markets', [sa.text('lower(system_name)')])


def downgrade() -> None:
    op.drop_index('ix_markets_system_name_lower', table_name='markets')
    op.drop_index('ix_markets_station_lower', table_name='markets')
    op.drop_index('ix_factions_name_lower', table_name='factions')
    op.drop_index('ix_systems_name_lower', table_name='systems')
    with op.batch_alter_table('price_history', schema=None) as batch_op:
        batch_op.drop_index('ix_price_history_ts')

    op.drop_table('price_history')
    with op.batch_alter_table('commodity_prices', schema=None) as batch_op:
        batch_op.drop_index('ix_commodity_prices_commodity_id')

    op.drop_table('commodity_prices')
    with op.batch_alter_table('markets', schema=None) as batch_op:
        batch_op.drop_index('ix_markets_updated_at')

    op.drop_table('markets')
    op.drop_table('factions')
    with op.batch_alter_table('faction_history', schema=None) as batch_op:
        batch_op.drop_index('ix_faction_history_ts')

    op.drop_table('faction_history')
    op.drop_table('systems')
    op.drop_table('meta')
    op.drop_table('commodities')
