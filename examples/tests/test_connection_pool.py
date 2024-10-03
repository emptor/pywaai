import pytest
import asyncio
import os
from db_utils import ConnectionPool
import pytest_asyncio
import sqlite3
from sqlcipher3 import dbapi2 as sqlcipher
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def event_loop():
    policy = asyncio.get_event_loop_policy()
    loop = policy.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="module")
async def connection_pool():
    # Use a temporary database file for testing
    db_path = "test_connection_pool.db"
    pool = ConnectionPool(db_path, pool_size=3)
    yield pool
    # Clean up: close all connections and remove the test database
    await pool.close_all()
    os.remove(db_path)


@pytest.mark.asyncio
async def test_connection_pool_initialization(connection_pool):
    assert connection_pool.db_path == "test_connection_pool.db"
    assert connection_pool.pool_size == 3
    assert connection_pool.pool.qsize() == 3


@pytest.mark.asyncio
async def test_get_and_release_connection(connection_pool):
    # Get a connection
    conn1 = await connection_pool.get_connection()
    assert connection_pool.pool.qsize() == 2

    # Get another connection
    conn2 = await connection_pool.get_connection()
    assert connection_pool.pool.qsize() == 1

    # Release the first connection
    await connection_pool.release_connection(conn1)
    assert connection_pool.pool.qsize() == 2

    # Release the second connection
    await connection_pool.release_connection(conn2)
    assert connection_pool.pool.qsize() == 3


@pytest.mark.asyncio
async def test_connection_functionality(connection_pool):
    conn = await connection_pool.get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS test_table (id INTEGER PRIMARY KEY, value TEXT)"
        )
        cursor.execute("INSERT INTO test_table (value) VALUES (?)", ("test_value",))
        conn.commit()

        cursor.execute("SELECT value FROM test_table WHERE id = 1")
        result = cursor.fetchone()
        assert result[0] == "test_value"
    finally:
        await connection_pool.release_connection(conn)


@pytest.mark.asyncio
async def test_close_all_connections(connection_pool):
    logger.debug("Starting test_close_all_connections")

    # Get all connections
    connections = []
    for _ in range(connection_pool.pool_size):
        conn = await connection_pool.get_connection()
        connections.append(conn)

    logger.debug(f"Got {len(connections)} connections")

    # Close all connections
    try:
        await asyncio.wait_for(connection_pool.close_all(), timeout=10.0)
        logger.debug("Closed all connections")
    except asyncio.TimeoutError:
        logger.error("Timeout while closing connections")
        raise

    # Verify that the pool is empty
    assert connection_pool.pool.empty()
    logger.debug("Pool is empty")

    # Verify that we can't get any more connections
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(connection_pool.get_connection(), timeout=1.0)
    logger.debug("Unable to get new connections")

    # Verify that the connections are closed by trying to use them
    for i, conn in enumerate(connections, start=1):
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            pytest.fail(f"Connection {i} is still open")
        except (sqlite3.ProgrammingError, sqlcipher.ProgrammingError):
            logger.debug(f"Connection {i} is closed")

    logger.debug("Test completed successfully")
