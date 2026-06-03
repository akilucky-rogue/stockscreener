import logging
from qsde.db.connection import read_sql, execute_sql

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("check_indexes")

def check():
    log.info("Checking indexes on factor_pit...")
    try:
        indexes = read_sql("""
            SELECT indexname, indexdef 
            FROM pg_indexes 
            WHERE tablename = 'factor_pit'
        """)
        log.info(f"Indexes on factor_pit:\n{indexes.to_string()}")
    except Exception as e:
        log.error(f"Error checking indexes: {e}")

if __name__ == "__main__":
    check()
