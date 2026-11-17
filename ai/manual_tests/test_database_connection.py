"""
Manual PostgreSQL database connection test.

Checks:
1. Windows asynchronous event loop compatibility.
2. Tutor AI settings loading.
3. Database URL conversion.
4. PostgreSQL connection.
5. SQL query execution.
6. Connection cleanup.
"""

import asyncio
import sys
from pathlib import Path

# --------------------------------------------------
# 1. Windows event loop compatibility
# --------------------------------------------------

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# --------------------------------------------------
# 2. Add AI project directory to Python import path
# --------------------------------------------------

AI_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AI_ROOT))

# --------------------------------------------------
# 3. Required imports
# --------------------------------------------------

import psycopg
from sqlalchemy.engine import make_url

from tutor_ai.settings import Settings

# --------------------------------------------------
# 4. Database connection test
# --------------------------------------------------


async def main():

    connection = None

    try:
        print("\n================================")
        print(" TUTOR AI DATABASE CONNECTION")
        print("================================\n")

        settings = Settings()

        print("Provider:", settings.provider)

        print(
            "Event loop:",
            type(asyncio.get_running_loop()).__name__,
        )

        print("\nLoading database configuration...")

        # Retrieve the actual URL from Pydantic SecretStr.
        database_url = settings.database_url.get_secret_value()

        # Convert SQLAlchemy URL format to Psycopg format.
        sqlalchemy_url = make_url(database_url)

        psycopg_url = sqlalchemy_url.set(drivername="postgresql")

        connection_string = psycopg_url.render_as_string(hide_password=False)

        print("Database URL format converted successfully.")
        print("Connecting to PostgreSQL...\n")

        # Establish asynchronous database connection.
        connection = await psycopg.AsyncConnection.connect(connection_string)

        print("DATABASE CONNECTION ESTABLISHED!")

        # Execute a simple test query.
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT 1;")

            result = await cursor.fetchone()

        print("\nQuery executed successfully.")
        print("Query result:", result[0])

        print("\n================================")
        print(" DATABASE TEST PASSED")
        print("================================")

    except Exception as exc:
        print("\n================================")
        print(" DATABASE TEST FAILED")
        print("================================")

        print("Error type:", type(exc).__name__)
        print("Error:", str(exc))

    finally:
        if connection is not None:
            await connection.close()

            print("\nDatabase connection closed.")


# --------------------------------------------------
# 5. Application entry point
# --------------------------------------------------

if __name__ == "__main__":
    asyncio.run(main())
