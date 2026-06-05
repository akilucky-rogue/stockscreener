import logging
from qsde.db.connection import get_sync_conn

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("create_index")

def create():
    log.info("Creating active factor partial index on factor_pit...")
    try:
        with get_sync_conn() as conn:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("""
                CREATE INDEX IF NOT EXISTS factor_pit_active_idx 
                ON factor_pit (symbol, factor_name) 
                WHERE valid_to = 'infinity'::timestamptz;
            """)
            log.info("Index created successfully!")
    except Exception as e:
        log.error(f"Error creating index: {e}")

if __name__ == "__main__":
    create()
