# fitzflix
A media library manager

## Installation

```
python3 -m venv venv &&
source venv/bin/activate &&
pip install -r requirements.txt &&
pip install gunicorn pymysql &&
flask db upgrade
```

## Running

### Redis

#### Scheduler

```
source venv/bin/activate &&
rqscheduler
```

#### Workers

```
source venv/bin/activate &&
rq worker fitzflix-tasks
```

```
source venv/bin/activate &&
rq worker fitzflix-transcode
```

Run a max of 1 SQL worker.

```
source venv/bin/activate &&
rq worker fitzflix-sql
```

### Flask

```
flask run
```