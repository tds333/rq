from multiprocessing import Process

from rq import Queue, SimpleWorker, Worker
from rq.job import Dependency, Job, JobStatus
from rq.utils import current_timestamp
from tests import RQTestCase
from tests.fixtures import check_dependencies_are_met, div_by_zero, kill_horse, long_running_job, say_hello


class TestDependencies(RQTestCase):
    def test_allow_failure_is_persisted(self):
        """Ensure that job.allow_dependency_failures is properly set
        when providing Dependency object to depends_on."""
        dep_job = Job.create(func=say_hello, connection=self.connection)

        # default to False, maintaining current behavior
        job = Job.create(func=say_hello, connection=self.connection, depends_on=Dependency([dep_job]))
        job.save()
        Job.fetch(job.id, connection=self.connection)
        self.assertFalse(job.allow_dependency_failures)

        job = Job.create(
            func=say_hello, connection=self.connection, depends_on=Dependency([dep_job], allow_failure=True)
        )
        job.save()
        job = Job.fetch(job.id, connection=self.connection)
        self.assertTrue(job.allow_dependency_failures)

        jobs = Job.fetch_many([job.id], connection=self.connection)
        self.assertTrue(jobs[0].allow_dependency_failures)

    def test_deferred_task_not_enqueued_when_dependencies_are_not_finished(self):

        job_a = Job.create(say_hello, connection=self.connection)
        job_b = Job.create(say_hello, connection=self.connection)
        job_c = Job.create(say_hello, connection=self.connection, depends_on=[job_a, job_b])
        job_a.save()
        job_b.save()
        job_c.save()

        queue = Queue('default', connection=self.connection)
        queue.enqueue_job(job_c)
        self.assertEqual(JobStatus.DEFERRED, job_c.get_status())
        self.assertEqual(0, queue.count)

        queue.enqueue_job(job_a)
        worker = SimpleWorker([queue], connection=queue.connection)
        worker.work(burst=True)
        self.assertEqual(JobStatus.FINISHED, job_a.get_status())

        # Child job is started but should not!!!
        self.assertEqual(JobStatus.DEFERRED, job_c.get_status(refresh=True))

    def test_job_dependency(self):
        """Enqueue dependent jobs only when appropriate"""
        q = Queue(connection=self.connection)
        w = SimpleWorker([q], connection=q.connection)

        # enqueue dependent job when parent successfully finishes
        parent_job = q.enqueue(say_hello)
        job = q.enqueue_call(say_hello, depends_on=parent_job)
        w.work(burst=True)
        job = Job.fetch(job.id, connection=self.connection)
        self.assertEqual(job.get_status(), JobStatus.FINISHED)
        q.empty()

        # don't enqueue dependent job when parent fails
        parent_job = q.enqueue(div_by_zero)
        job = q.enqueue_call(say_hello, depends_on=parent_job)
        w.work(burst=True)
        job = Job.fetch(job.id, connection=self.connection)
        self.assertNotEqual(job.get_status(), JobStatus.FINISHED)
        q.empty()

        # don't enqueue dependent job when Dependency.allow_failure=False (the default)
        parent_job = q.enqueue(div_by_zero)
        dependency = Dependency(jobs=parent_job)
        job = q.enqueue_call(say_hello, depends_on=dependency)
        w.work(burst=True)
        job = Job.fetch(job.id, connection=self.connection)
        self.assertNotEqual(job.get_status(), JobStatus.FINISHED)

        # enqueue dependent job when Dependency.allow_failure=True
        parent_job = q.enqueue(div_by_zero)
        dependency = Dependency(jobs=parent_job, allow_failure=True)
        job = q.enqueue_call(say_hello, depends_on=dependency)

        job = Job.fetch(job.id, connection=self.connection)
        self.assertTrue(job.allow_dependency_failures)

        w.work(burst=True)
        job = Job.fetch(job.id, connection=self.connection)
        self.assertEqual(job.get_status(), JobStatus.FINISHED)

        # When a failing job has multiple dependents, only enqueue those
        # with allow_failure=True
        parent_job = q.enqueue(div_by_zero)
        job_allow_failure = q.enqueue(say_hello, depends_on=Dependency(jobs=parent_job, allow_failure=True))
        job = q.enqueue(say_hello, depends_on=Dependency(jobs=parent_job, allow_failure=False))
        w.work(burst=True, max_jobs=1)
        self.assertEqual(parent_job.get_status(), JobStatus.FAILED)
        self.assertEqual(job_allow_failure.get_status(), JobStatus.QUEUED)
        self.assertEqual(job.get_status(), JobStatus.DEFERRED)
        q.empty()

        # only enqueue dependent job when all dependencies have finished/failed
        first_parent_job = q.enqueue(div_by_zero)
        second_parent_job = q.enqueue(say_hello)
        dependencies = Dependency(jobs=[first_parent_job, second_parent_job], allow_failure=True)
        job = q.enqueue_call(say_hello, depends_on=dependencies)
        w.work(burst=True, max_jobs=1)
        self.assertEqual(first_parent_job.get_status(), JobStatus.FAILED)
        self.assertEqual(second_parent_job.get_status(), JobStatus.QUEUED)
        self.assertEqual(job.get_status(), JobStatus.DEFERRED)

        # When second job finishes, dependent job should be queued
        w.work(burst=True, max_jobs=1)
        self.assertEqual(second_parent_job.get_status(), JobStatus.FINISHED)
        self.assertEqual(job.get_status(), JobStatus.QUEUED)
        w.work(burst=True)
        job = Job.fetch(job.id, connection=self.connection)
        self.assertEqual(job.get_status(), JobStatus.FINISHED)

        # Test dependant is enqueued at front
        q.empty()
        parent_job = q.enqueue(say_hello)
        q.enqueue(say_hello, job_id='fake_job_id_1', depends_on=Dependency(jobs=[parent_job]))
        q.enqueue(say_hello, job_id='fake_job_id_2', depends_on=Dependency(jobs=[parent_job], enqueue_at_front=True))
        w.work(burst=True, max_jobs=1)

        self.assertEqual(q.job_ids, ['fake_job_id_2', 'fake_job_id_1'])

    def test_multiple_jobs_with_dependencies(self):
        """Enqueue dependent jobs only when appropriate"""
        q = Queue(connection=self.connection)
        w = SimpleWorker([q], connection=q.connection)

        # Multiple jobs are enqueued with correct status
        parent_job = q.enqueue(say_hello)
        job_no_deps = Queue.prepare_data(say_hello)
        job_with_deps = Queue.prepare_data(say_hello, depends_on=parent_job)
        jobs = q.enqueue_many([job_no_deps, job_with_deps])
        self.assertEqual(jobs[0].get_status(), JobStatus.QUEUED)
        self.assertEqual(jobs[1].get_status(), JobStatus.DEFERRED)
        w.work(burst=True, max_jobs=1)
        self.assertEqual(jobs[1].get_status(), JobStatus.QUEUED)

        job_with_met_deps = Queue.prepare_data(say_hello, depends_on=parent_job)
        jobs = q.enqueue_many([job_with_met_deps])
        self.assertEqual(jobs[0].get_status(), JobStatus.QUEUED)
        q.empty()

    def test_dependency_list_in_depends_on(self):
        """Enqueue with Dependency list in depends_on"""
        q = Queue(connection=self.connection)
        w = SimpleWorker([q], connection=q.connection)

        # enqueue dependent job when parent successfully finishes
        parent_job1 = q.enqueue(say_hello)
        parent_job2 = q.enqueue(say_hello)
        job = q.enqueue_call(say_hello, depends_on=[Dependency([parent_job1]), Dependency([parent_job2])])
        w.work(burst=True)
        self.assertEqual(job.get_status(), JobStatus.FINISHED)

    def test_enqueue_job_dependency(self):
        """Enqueue via Queue.enqueue_job() with depencency"""
        q = Queue(connection=self.connection)
        w = SimpleWorker([q], connection=q.connection)

        # enqueue dependent job when parent successfully finishes
        parent_job = Job.create(say_hello, connection=self.connection)
        parent_job.save()
        job = Job.create(say_hello, connection=self.connection, depends_on=parent_job)
        q.enqueue_job(job)
        w.work(burst=True)
        self.assertEqual(job.get_status(), JobStatus.DEFERRED)
        q.enqueue_job(parent_job)
        w.work(burst=True)
        self.assertEqual(parent_job.get_status(), JobStatus.FINISHED)
        self.assertEqual(job.get_status(), JobStatus.FINISHED)

    def test_enqueue_job_dependency_sets_ttl(self):
        """Ensures that the TTL of jobs in the deferred queue is set"""
        q = Queue(connection=self.connection)
        parent_job = Job.create(say_hello, connection=self.connection)
        parent_job.save()

        timestamp = current_timestamp()
        ttl = 5
        job = Job.create(say_hello, connection=self.connection, depends_on=parent_job, ttl=ttl)
        q.enqueue_job(job)
        score = self.connection.zscore(q.deferred_job_registry.key, job.id)
        self.assertLess(score, timestamp + ttl + 2)
        self.assertGreater(score, timestamp + ttl - 2)

    def test_dependencies_are_met_if_parent_is_canceled(self):
        """When parent job is canceled, it should be treated as failed"""
        queue = Queue(connection=self.connection)
        job = queue.enqueue(say_hello)
        job.set_status(JobStatus.CANCELED)
        dependent_job = queue.enqueue(say_hello, depends_on=job)
        # dependencies_are_met() should return False, whether or not
        # parent_job is provided
        self.assertFalse(dependent_job.dependencies_are_met(job))
        self.assertFalse(dependent_job.dependencies_are_met())

    def test_can_enqueue_job_if_dependency_is_deleted(self):
        queue = Queue(connection=self.connection)

        dependency_job = queue.enqueue(say_hello, result_ttl=0)

        w = Worker([queue], connection=self.connection)
        w.work(burst=True)

        assert queue.enqueue(say_hello, depends_on=dependency_job)

    def test_dependencies_are_met_if_dependency_is_deleted(self):
        queue = Queue(connection=self.connection)

        dependency_job = queue.enqueue(say_hello, result_ttl=0)
        dependent_job = queue.enqueue(say_hello, depends_on=dependency_job)

        w = Worker([queue], connection=self.connection)
        w.work(burst=True, max_jobs=1)

        assert dependent_job.dependencies_are_met()
        assert dependent_job.get_status() == JobStatus.QUEUED

    def test_dependencies_are_met_at_execution_time(self):
        queue = Queue(connection=self.connection)
        queue.empty()
        queue.enqueue(say_hello, job_id='A')
        queue.enqueue(say_hello, job_id='B')
        job_c = queue.enqueue(check_dependencies_are_met, job_id='C', depends_on=['A', 'B'])

        job_c.dependencies_are_met()
        w = Worker([queue], connection=self.connection)
        w.work(burst=True)
        assert job_c.result

    def test_allow_failures_when_work_horse_killed(self):
        """Ensure that allow_failure is respected when a worker is killed"""
        queue = Queue(connection=self.connection)
        job = queue.enqueue(long_running_job, 10, horse_pid_key='horse_pid_key')
        job2 = queue.enqueue(say_hello, depends_on=Dependency(jobs=job, allow_failure=True))

        # Wait 1 second before killing the horse to simulate horse terminating unexpectedly
        p = Process(target=kill_horse, args=('horse_pid_key', self.connection.connection_pool.connection_kwargs, 1))
        p.start()

        worker = Worker([queue], connection=self.connection)
        worker.work(burst=True)

        self.assertEqual(job.get_status(), JobStatus.FAILED)
        self.assertEqual(job2.get_status(), JobStatus.FINISHED)

    def test_dependency_accepts_single_job(self):
        """Test that Dependency constructor accepts a single Job instance"""
        q = Queue(connection=self.connection)
        w = SimpleWorker([q], connection=q.connection)

        # Test with single Job instance
        parent_job = q.enqueue(say_hello)
        dependency = Dependency(parent_job)  # Single job, not in a list
        job = q.enqueue_call(say_hello, depends_on=dependency)

        w.work(burst=True)
        self.assertEqual(job.get_status(), JobStatus.FINISHED)
        q.empty()

        # Test with single Job instance and allow_failure=True
        parent_job = q.enqueue(div_by_zero)
        dependency = Dependency(parent_job, allow_failure=True)  # Single job with allow_failure
        job = q.enqueue_call(say_hello, depends_on=dependency)

        w.work(burst=True)
        self.assertEqual(job.get_status(), JobStatus.FINISHED)
        q.empty()

        # Test with single job ID string
        parent_job = q.enqueue(say_hello)
        dependency = Dependency(parent_job.id)  # Single job ID string
        job = q.enqueue_call(say_hello, depends_on=dependency)

        w.work(burst=True)
        self.assertEqual(job.get_status(), JobStatus.FINISHED)
