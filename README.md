# dm-ci

A FastAPI service that updates configured Docker Compose projects on demand.

## Request an update

Configure API keys and project directories in the `[folders]` section of
`config.ini`. Each directory must contain a regular `docker-compose.yml` file.
Relative paths are resolved relative to `app.py`.

```sh
curl -X POST 'http://localhost:8000/update?api_key=example_key'
```

A valid request returns HTTP **202 Accepted** immediately, for example:

```json
{
  "status": "queued",
  "job_id": "c44c5fb1-0fb1-4ad7-9937-26d891641420",
  "project_dir": "/root/docker/example_service"
}
```

Acceptance does not indicate a successful deployment. Use the job ID to
correlate queue, start, completion and failure messages in the updater logs.
There is no job-status endpoint.

Invalid keys return 401, missing or invalid project configuration returns 500,
and a full queue (or shutdown) returns 503. Requests missing the required
`api_key` parameter receive FastAPI's usual validation error (422).

## Execution and shutdown

- One in-process FIFO queue holds at most 100 pending jobs, in addition to
  the active job. Duplicate requests are queued independently.
- A single worker runs `docker compose pull` followed by
  `docker compose up -d` for each project. No two jobs execute concurrently;
  Docker Compose's own internal parallelism is unchanged.
- Each command has a 600-second timeout. Failed pulls skip the up command.
  Failures are logged and the next job proceeds; there are no automatic retries.
- Run only one Uvicorn worker and one updater instance per Docker host.
  Independent processes or replicas have independent queues.
- The queue is not persistent. On graceful shutdown, pending jobs are
  discarded and the active job is allowed to finish. The supplied Compose
  configuration grants 21 minutes for shutdown, covering both command
  timeouts plus a margin. Forced termination or a self-update can interrupt
  the active job.
- Uvicorn access logging is disabled in the supplied launch commands to avoid
  logging query-string API keys. Configure any reverse proxy accordingly.

## Tests

```sh
python -m pip install -r requirements.txt httpx
python -m unittest discover -s tests -v
```

The tests mock Docker execution and do not require a Docker daemon.
