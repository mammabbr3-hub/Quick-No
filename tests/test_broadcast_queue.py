from pathlib import Path

def test_broadcast_handler_queues_instead_of_sending_inline():
    text = Path('app/admin/handlers.py').read_text()
    section = text.split("@router.message(Command('broadcast'))", 1)[1].split("@router.message(Command('schedule'))", 1)[0]
    assert "status='queued'" in section
    assert 'process_broadcasts' not in section

def test_worker_has_persistent_broadcast_processor():
    text = Path('app/worker.py').read_text()
    assert 'async def process_broadcasts(bot):' in text
    assert "Broadcast.status=='queued'" in text
