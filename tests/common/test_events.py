import pytest
import asyncio
import json
import os
from aaac.common.events import EventLogger

@pytest.fixture
def tmp_results(tmp_path):
    orig_cwd = os.getcwd()
    os.chdir(tmp_path)
    yield tmp_path
    os.chdir(orig_cwd)

@pytest.mark.asyncio
async def test_event_logger_flush_on_count(tmp_results):
    logger = EventLogger("test_run", "aaac")
    
    # Write 100 events
    for i in range(100):
        await logger.log("JOIN", ticket_id=f"t_{i}")
        
    # At 100 it should flush synchronously (unlocked)
    # Wait slightly to ensure thread completion since write is in to_thread
    await asyncio.sleep(0.1)
    
    # The file should exist and have 100 lines
    assert os.path.exists(logger.filepath)
    with open(logger.filepath, "r") as f:
        lines = f.readlines()
        assert len(lines) == 100
        
    await logger.close()

@pytest.mark.asyncio
async def test_event_logger_flush_on_timeout(tmp_results):
    logger = EventLogger("test_run_2", "aaac")
    
    await logger.log("ESTIMATE", ticket_id="t_1", bytes=100)
    
    # Buffer has 1 item, file shouldn't be written yet (or could be in progress)
    # Let's wait for the timeout (500ms) plus a bit for thread completion
    await asyncio.sleep(0.7)
    
    assert os.path.exists(logger.filepath)
    with open(logger.filepath, "r") as f:
        lines = f.readlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["event"] == "ESTIMATE"
        assert data["bytes"] == 100
        
    await logger.close()

@pytest.mark.asyncio
async def test_event_logger_invalid_event():
    logger = EventLogger("test", "aaac")
    with pytest.raises(ValueError, match="Unknown event name"):
        await logger.log("INVALID_EVENT")
    await logger.close()
