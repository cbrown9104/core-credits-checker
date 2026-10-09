"""Background jobs for the Full Reconciliation page.

OCR on a batch of shipper scans can run for minutes, longer than a web
request should, so each run happens on a worker thread and the page polls
for progress. One run at a time (the rest wait in line) keeps memory flat.
Uploaded files are deleted as soon as a run ends; result files are kept
for KEEP_SECONDS so they can be downloaded, then removed.
"""
import json
import os
import re
import shutil
import threading
import time
import traceback
import uuid

JOB_ROOT = os.environ.get('RECON_JOB_DIR', '/tmp/recon_jobs')
KEEP_SECONDS = 2 * 60 * 60
JOB_ID_RE = re.compile(r'^[0-9a-f]{32}$')

_lock = threading.Lock()
_one_at_a_time = threading.Semaphore(1)
_jobs = {}
# Who a job belongs to (store, user). Kept apart from the job itself so it
# is never sent to the browser with the job's status.
_meta = {}

NOT_SAVED = ('This run finished, but it could not be saved to your account. '
             'Download the files below now and keep the master. Then run it '
             'again later or contact support.')


def job_dir(job_id):
    return os.path.join(JOB_ROOT, job_id)


def new_job(meta=None):
    job_id = uuid.uuid4().hex
    d = job_dir(job_id)
    os.makedirs(os.path.join(d, 'in'), exist_ok=True)
    os.makedirs(os.path.join(d, 'out'), exist_ok=True)
    with _lock:
        _jobs[job_id] = {
            'id': job_id, 'status': 'queued', 'created': time.time(),
            'started': None, 'finished': None, 'message': 'Waiting to start',
            'log': [], 'result': None, 'error': None,
        }
        if meta is not None:
            _meta[job_id] = dict(meta)
    return job_id, d


def discard(job_id):
    """Forget a job that never started, with its folder."""
    with _lock:
        _jobs.pop(job_id, None)
        _meta.pop(job_id, None)
    shutil.rmtree(job_dir(job_id), ignore_errors=True)


def meta(job_id):
    """Who the job belongs to, or None (also None when accounts are off)."""
    with _lock:
        m = _meta.get(job_id)
        return dict(m) if m is not None else None


def _update(job_id, **kw):
    with _lock:
        job = _jobs.get(job_id)
        if job:
            job.update(kw)


def _progress(job_id):
    def cb(stage, message):
        with _lock:
            job = _jobs.get(job_id)
            if not job:
                return
            job['message'] = message
            if not job['log'] or job['log'][-1] != message:
                job['log'].append(message)
                job['log'] = job['log'][-40:]
    return cb


def start(job_id, work, on_done=None, on_error=None):
    """Run work(progress) on a worker thread.

    on_done(job_id, result, out_dir) is called after a successful run,
    while the result files still exist; whatever dict it returns is merged
    into the result. on_error(job_id, message) is called when the run
    stops. Both are optional (they save the run to the store's account).
    """
    t = threading.Thread(target=_run, args=(job_id, work, on_done, on_error),
                         daemon=True)
    t.start()


def _run(job_id, work, on_done=None, on_error=None):
    with _one_at_a_time:
        _update(job_id, status='running', started=time.time(),
                message='Starting')
        try:
            result = work(_progress(job_id))
            safe = json.loads(json.dumps(result, default=str))
            if on_done is not None:
                _update(job_id, message='Saving to your account')
                try:
                    extra = on_done(job_id, safe,
                                    os.path.join(job_dir(job_id), 'out'))
                    if extra:
                        safe.update(extra)
                except Exception:   # the run itself is still good
                    traceback.print_exc()
                    safe['saved'] = False
                    safe.setdefault('warnings', []).insert(0, NOT_SAVED)
                    if on_error is not None:
                        try:    # so the history does not show it running
                            on_error(job_id, NOT_SAVED)
                        except Exception:
                            traceback.print_exc()
            _update(job_id, status='done', finished=time.time(),
                    result=safe, message='Done')
        except Exception as e:  # report, never crash the worker
            traceback.print_exc()
            message = str(e) or e.__class__.__name__
            _update(job_id, status='error', finished=time.time(),
                    error=message, message='Stopped with an error')
            if on_error is not None:
                try:
                    on_error(job_id, message)
                except Exception:
                    traceback.print_exc()
        finally:
            shutil.rmtree(os.path.join(job_dir(job_id), 'in'),
                          ignore_errors=True)


def get(job_id):
    if not JOB_ID_RE.match(job_id or ''):
        return None
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return None
        out = dict(job)
        out['log'] = list(job['log'])
    now = time.time()
    out['elapsed'] = round((out['finished'] or now) -
                           (out['started'] or out['created']), 1)
    if out['status'] == 'queued':
        with _lock:
            out['ahead'] = sum(1 for j in _jobs.values()
                               if j['status'] in ('queued', 'running') and
                               j['created'] < out['created'])
    return out


def output_path(job_id, name):
    """Absolute path of a result file, or None if it isn't one."""
    job = get(job_id)
    if not job or job['status'] != 'done' or not job['result']:
        return None
    names = {f['name'] for f in job['result'].get('files', [])}
    if name not in names:
        return None
    path = os.path.join(job_dir(job_id), 'out', name)
    return path if os.path.isfile(path) else None


def cleanup():
    now = time.time()
    with _lock:
        stale = [k for k, j in _jobs.items()
                 if j['status'] in ('done', 'error') and
                 now - (j['finished'] or j['created']) > KEEP_SECONDS]
        for k in stale:
            _jobs.pop(k, None)
            _meta.pop(k, None)
    for k in stale:
        shutil.rmtree(job_dir(k), ignore_errors=True)
    # leftovers from a previous process (server restart)
    if os.path.isdir(JOB_ROOT):
        for name in os.listdir(JOB_ROOT):
            p = os.path.join(JOB_ROOT, name)
            with _lock:
                known = name in _jobs
            if not known and now - os.path.getmtime(p) > KEEP_SECONDS:
                shutil.rmtree(p, ignore_errors=True)
