"""Quick test: verify DB schema and run basic smoke tests."""
from qsde.db import read_sql, check_connection

print("DB Connected:", check_connection())

tables = read_sql(
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema = 'public' ORDER BY table_name"
)
print("\nTables in DB:")
for t in tables["table_name"].tolist():
    print(f"  - {t}")
print(f"\nTotal: {len(tables)} tables")
