"""Unit tests for Queue Resilience, Self-Healing, and Auto-Advancement."""
import json
import tempfile
from pathlib import Path
from manager.queue import QueueService

def test_queue_resilience():
    with tempfile.TemporaryDirectory() as tmp_dir:
        q_path = Path(tmp_dir) / ".test-queue.json"
        service = QueueService(storage_path=q_path)

        # 1. Add 3 jobs
        j1 = service.add_job(Path("file1.xlsx"), 0, "Sheet1", total_records=100)
        j2 = service.add_job(Path("file2.xlsx"), 0, "Sheet1", total_records=200)
        j3 = service.add_job(Path("file3.xlsx"), 0, "Sheet1", total_records=300)

        assert j1.status == "pending"
        assert j2.status == "pending"
        assert j3.status == "pending"

        # 2. Start Job 1
        service.mark_job_active(j1.id)
        assert service.get_job(j1.id).status == "active"

        # 3. Next pending job while j1 is active should be j2
        next_job = service.get_next_pending_job(exclude_job_id=j1.id)
        assert next_job is not None
        assert next_job.id == j2.id

        # 4. Simulate j1 completed
        service.mark_job_completed(j1.id)
        assert service.get_job(j1.id).status == "completed"

        # 5. Simulate j2 was stuck in 'active' (due to a crash or race condition) with 0 records done
        service.mark_job_active(j2.id)
        # Even though j2 is marked 'active', get_next_pending_job should heal and return it!
        recovered = service.get_next_pending_job(exclude_job_id=j1.id)
        assert recovered is not None
        assert recovered.id == j2.id
        assert recovered.status == "pending"

        # 6. Test Startup Sanitation on Disk Reload
        # Set j3 to 'active' on disk
        service.mark_job_active(j3.id)
        # Now simulate application restart with a fresh QueueService instance
        service2 = QueueService(storage_path=q_path)
        reloaded_j3 = service2.get_job(j3.id)
        # Should be self-healed to 'pending'
        assert reloaded_j3.status == "pending", f"Expected 'pending', got '{reloaded_j3.status}'"

        # 7. Complete all jobs and ensure get_next_pending_job returns None
        service2.mark_job_completed(j2.id)
        service2.mark_job_completed(j3.id)
        assert service2.get_next_pending_job() is None

        print("✔ ALL QUEUE RESILIENCE & SELF-HEALING TESTS PASSED!")

if __name__ == "__main__":
    test_queue_resilience()
