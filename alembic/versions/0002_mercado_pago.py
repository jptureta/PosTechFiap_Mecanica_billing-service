"""Add Mercado Pago fields to payments."""
from alembic import op
import sqlalchemy as sa

revision = "0002_mercado_pago"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("payments")}
    if "qr_data" not in columns:
        op.add_column("payments", sa.Column("qr_data", sa.Text(), nullable=True))
    if "provider" not in columns:
        op.add_column("payments", sa.Column("provider", sa.String(length=50), nullable=True))


def downgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("payments")}
    if "provider" in columns:
        op.drop_column("payments", "provider")
    if "qr_data" in columns:
        op.drop_column("payments", "qr_data")
