# The Fitzflix runbook

This file tells you what to do when the operation of Fitzflix goes wrong. Each section starts with what you see, then gives the steps.

The README tells you how to install Fitzflix. Its section "Disaster recovery" tells you how to rebuild Fitzflix after the loss of the machine. The file `ARCHITECTURE.md` tells you where the code is.

Run each command from the project root.

## Where to look first

| Question | Where to look |
| --- | --- |
| Do all the programs run? | `supervisorctl status 'fitzflix:*'`. Each of the 11 lines must say RUNNING. |
| What did the application write? | `logs/fitzflix.log`. All the programs write to this file. Search it for ERROR. |
| Why does a program not start? | `supervisorctl tail fitzflix:fitzflix-web_00 stderr`. Use the name of the program from the status list. |
| Which tasks failed? | The table "Failed tasks" of the System page. Each row has a Requeue button and a Forget button. |
| Where is a file in the import? | The Queue page. |
| Did a scheduled task run? | The table "Scheduled tasks" of the System page. It shows the last run of each task. |
| Does a task wait or run now? | `venv/bin/python scripts/idle_check.py`. It prints each busy queue. |

Put quotes around `'fitzflix:*'`. Without the quotes, the shell reads the star as a file pattern, and the command does not run.

## The steps of a normal deploy

1. Run the tests: `venv/bin/python -m pytest -q -p no:cacheprovider`.
2. Commit the change.
3. Run `scripts/deploy`.
4. Run `venv/bin/python scripts/drill_workers.py`.

The README section "Updating" tells you what the 2 scripts do. Do not deploy while a person changes or deletes files on the pages.

A change of only the templates or of the files of `app/static/` needs only the web program:

```
supervisorctl restart 'fitzflix:fitzflix-web_00'
```

## The deploy script says that the queues are busy

The script restarted nothing. A restart ends a job that runs, and an import can run for hours.

The simple answer is to wait and to run the script again. If the queues are never idle, restart 1 program at a time. This is safe only for a change that has no database migration.

| Program | A restart while a job runs |
| --- | --- |
| `fitzflix-import`, `fitzflix-file-operation` | The worker finishes its job first. Supervisor waits 10 minutes for it. |
| `fitzflix-transcode` | The worker finishes its job first. Supervisor waits 30 minutes for it. |
| `fitzflix-sql`, `fitzflix-user-request`, `fitzflix-maintenance` | The job ends. Restart these programs only when their queue is idle. |
| `fitzflix-rqscheduler` | No job runs in this program. You can restart it at any time. |
| `fitzflix-web` | A page request that is in progress has 10 seconds to finish. |

The idle check names the busy queues. The queues of the third row have the names `sql`, `request`, and `maintenance` in its output.

A program with 2 processes has 2 names. Restart them together:

```
supervisorctl restart 'fitzflix:fitzflix-import_00' 'fitzflix:fitzflix-import_01'
```

A job that is longer than the wait of its program ends when the wait ends. The section "A restart ended a job that ran" tells you what occurs then.

A change with a database migration needs all the programs stopped. For such a change, wait until the queues are idle.

## A migration failed in a deploy

The deploy script printed the error and left all the programs stopped. The pages do not answer until you start the programs.

1. Read the error. It is above the message of the script.
2. Find the migration that the database has: `venv/bin/flask db current`. Find the newest migration of the code: `venv/bin/flask db heads`.
3. MariaDB does not undo the part of a migration that ran. Thus, a migration that failed can be half done. Compare the tables with the file of the migration in `migrations/versions/`.
4. Correct the cause. Then run `venv/bin/flask db upgrade` again.
5. Start the programs: `supervisorctl start 'fitzflix:*'`. Then run the drill.

If you cannot correct the migration, go back to the code before the change. The section "A program does not start after a deploy" has the steps.

Each `flask db` command changes the live database. There is no test mode. Before a migration that removes or rewrites data, make a backup. On the System page, find "Backing up the database" in the table "Scheduled tasks" and use its Run now button. Then the folder `backups/` holds a dump of this minute. The README section "Disaster recovery" tells you how to load a dump.

## A program does not start after a deploy

1. Find the program that does not run: `supervisorctl status 'fitzflix:*'`.
2. Read its error: `supervisorctl tail <name of the program> stderr`. The last lines are usually a Python error with a file name and a line number.
3. If you can correct the error quickly, correct it, commit, and deploy again.
4. If you cannot, go back to the last good code:

```
git log --oneline -5
```

```
git revert --no-edit <the commit of the change>
```

```
scripts/deploy
```

```
git push
```

The command `git revert` makes a new commit that undoes the change. The history stays complete.

Two cases need 1 more step:

- The change had a database migration, and the migration ran. Run `venv/bin/flask db downgrade` before the revert. The downgrade needs the file of the migration, and the revert removes that file.
- The change moved or renamed a task. After the revert, rename the stored jobs back. The section "A change moved or renamed a task" has the command. Give the 2 names in the opposite order.

If the deploy script says that the queues are busy, restart only the program that does not run.

## A restart ended a job that ran

Fitzflix does most of the cleanup by itself. Each worker looks for the jobs of dead workers when it starts. It does these things for such a job:

- It marks the job as failed. The trail of the file on the Queue page shows the failed stage.
- For a stage of an import, it releases the lock of the title and the reserved space of the staging area.
- It leaves the source file where it is.

The hourly sweep queues a file of the import directory again. The hourly retry queues a download of Sonarr or Radarr in the library again, at most 3 times in 7 days.

Your steps:

1. Make sure that all the programs run. The cleanup occurs at the start of a worker.
2. Open the System page. In the table "Failed tasks", use Requeue for a task that you want again, and Forget for the other tasks.
3. If you do not want to wait for the hourly sweep, run `venv/bin/flask scan`.

## A title stays locked

A task that changes the files of a title holds a lock for that title. A second task for the same title waits and tries again later. This is normal while the first task runs.

The problem is a lock with no task. The log then repeats 1 of these texts for 1 file, and the Queue page shows no task that runs for the title. The first text is from a stage of an import. The second text is from each other task:

```
Another task holds the title lock (wait 3), returning to queue after 30 minutes
```

```
Lock exists, returning to queue after 5 minutes
```

The cause is a task that ended in a restart and that was not a stage of an import. Examples are an upload to the archive and an edit of the tracks. Such a lock ends by itself after `LOCALIZATION_TASK_TIMEOUT`. The default is 1 day.

To release the lock now:

1. Make sure that no task runs for the title. The Queue page and the idle check tell you.
2. List the locks. Each lock is 1 key in Redis, and the name of the key is a JSON text with the title:

```
redis-cli --scan --pattern '{*'
```

3. Delete the key of the title. Put single quotes around the key, because the key has double quotes in it:

```
redis-cli del '<the key>'
```

The task that waits takes the lock at its next try. If `REDIS_URL` names a database other than 0, add `-n <number>` to each `redis-cli` command.

## A change moved or renamed a task

A task is a function that a worker runs as a job. Redis holds each job with the name of its function as text, for example `app.media.tracks.mkvmerge_task`. When you move the function to an other module, or give it an other name, the jobs in Redis keep the old name. A worker with the new code cannot find that name, and the job fails.

In the code, change the name in each of these places. Search for the old name to find them:

- the key of the task in `TASKS` in `app/tasks.py`
- each call of `enqueue_task` with the name
- the table of `cron_table` in `app/__init__.py`, if the task has a schedule
- `STAGES` in `app/pipeline.py`, if the task is a stage of the trail
- the tests

The test file `tests/test_task_registry.py` fails when a name in the code is not the module that defines the function.

Deploy such a change with these steps. Do not use `scripts/deploy` for it, because the rename must occur while the programs are stopped.

```
venv/bin/python scripts/idle_check.py
```

```
supervisorctl stop 'fitzflix:*'
```

```
git pull
```

```
venv/bin/python scripts/rename_stored_jobs.py <old name> <new name>
```

```
venv/bin/python scripts/rename_stored_jobs.py <old name> <new name> --apply
```

```
venv/bin/flask db upgrade
```

```
supervisorctl start 'fitzflix:*'
```

```
venv/bin/python scripts/drill_workers.py
```

Go on to the stop only if the idle check prints "idle". The first run of the rename script only reports. Read the report, then run the script with `--apply`.

A name can be 1 task or 1 module. The name of a module renames each task of that module. You can give more than 1 pair of names in 1 command.

If you forgot the rename, the table "Failed tasks" of the System page shows rows with `AttributeError` or `ModuleNotFoundError`. Run the script with `--apply` then. After that, use Requeue on each of those rows.

## A change of the supervisor settings

Supervisor does not read `fitzflix_supervisor.ini` of the repository. It reads a copy in its own folder. With Homebrew, that folder is `/opt/homebrew/etc/supervisor.d/`. The copy can have an other name than the file of the repository. Look in the folder first, and copy over the file that is there. A second file with an other name gives each program 2 definitions.

```
ls /opt/homebrew/etc/supervisor.d/
```

```
cp fitzflix_supervisor.ini /opt/homebrew/etc/supervisor.d/<name of the copy>
```

```
supervisorctl reread
```

```
supervisorctl update
```

The command `update` restarts each program whose settings changed. Thus, run it when the queues are idle.

## A scheduled task does not run

1. Open the System page. The table "Scheduled tasks" shows the last run and the next run of each task.
2. Make sure that the scheduler runs: `supervisorctl status 'fitzflix:fitzflix-rqscheduler_00'`.
3. Restart the scheduler: `supervisorctl restart 'fitzflix:fitzflix-rqscheduler_00'`. This is safe at any time, because the scheduler only puts jobs on the queues.
4. To run 1 task now, use its Run now button in the table.

Only 1 scheduler can work at a time. A second scheduler waits for the first one. Thus, a scheduler that you start by hand does nothing while the program of supervisor runs.

## A deploy cut a page request

The web program has 10 seconds to finish its requests at a restart. A request that is longer ends in the middle.

The request that matters most is the delete of a file. The delete removes the record first. Then it deletes the archive of the file in S3. A restart between these 2 steps leaves an archive that no record claims. The weekly audit of the bucket handles such an archive, and its mail names it. You do not need to do anything, but expect that name in the mail.

To prevent this, look at the end of `logs/access.log` before a restart of the web program. If the last lines are from this minute, a person uses the pages.
