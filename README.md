# fitzflix
A media library manager. Fitzflix was created by Glenn Fitzpatrick so he would know what was in his family's library when browsing for movies at thrift shops, and to keep track of his movie reviews.

<img width="1208" alt="Screen Shot 2022-05-31 at 11 50 36 AM" src="https://user-images.githubusercontent.com/10539597/171218753-2616f91e-677a-483b-bceb-03048b372df3.png">

Fitzflix takes video files for movies and TV shows, uploads to AWS S3 Glacier Deep-Archive storage for backup, sorts them into a Plex-compatible folder hierarchy, removes non-native languages and subtitles to save space, and lets you easily see what movies and TV shows you have in your library and in what formats to help upgrade their quality.

Files named like these…

<img width="602" alt="Screen Shot 2022-05-31 at 11 59 46 AM" src="https://user-images.githubusercontent.com/10539597/171218705-b31a6263-0fc2-489e-8f9f-efdc3f00fae3.png">

…become sorted like so…

<img width="358" alt="Screen Shot 2022-05-31 at 12 05 56 PM" src="https://user-images.githubusercontent.com/10539597/171219194-941736ed-95e2-4dd5-889d-07de0323c4a7.png">

…and are displayed in the application as…

<img width="1219" alt="Screen Shot 2022-05-31 at 11 55 06 AM" src="https://user-images.githubusercontent.com/10539597/171219305-080c44a5-7455-42d0-8dd5-119fbbf1bd36.png">

<img width="1215" alt="Screen Shot 2022-05-31 at 12 15 25 PM" src="https://user-images.githubusercontent.com/10539597/171221742-e41c84d5-3c3b-47a0-9847-16cdfd65d8b4.png">

…and show associated information from TMDb:

<img width="1208" alt="Screen Shot 2022-05-31 at 11 53 15 AM" src="https://user-images.githubusercontent.com/10539597/171219470-d5d819a0-aa6e-4dc7-a09e-3aa97881936a.png">

It supports reviewing films to help keep track of what you've seen:

<img width="1206" alt="Screen Shot 2022-05-31 at 11 56 40 AM" src="https://user-images.githubusercontent.com/10539597/171219852-9de3c5de-863f-4c9a-b88f-c844186e57ca.png">

It also supports TV shows:

<img width="1204" alt="Screen Shot 2022-05-31 at 11 56 13 AM" src="https://user-images.githubusercontent.com/10539597/171219677-f56fa57b-e55b-4dc1-974e-ddfec5a40f69.png">

And makes a great shopping list for searching for films that aren't as good as they could be (e.g. finding non-fullscreen versions of films, upgrading from DVD to Blu-Ray, etc.):

<img width="1203" alt="Screen Shot 2022-05-31 at 11 55 31 AM" src="https://user-images.githubusercontent.com/10539597/171219618-695489d4-adc7-4af5-97b2-90c47a74e223.png">


## How to use

TODO


## Installation

```
python3 -m venv venv &&
source venv/bin/activate &&
pip install -r requirements.txt &&
pip install gunicorn pymysql &&
flask db upgrade
```

## Running via supervisor

Update `command`, `directory`, and `user` fields in `fitzflix_supervisor.ini` file with installation and user information.

```
brew install supervisor &&
cp fitzflix_supervisor.ini /usr/local/etc/supervisor.d/ &&
brew services start supervisor
```

## Running Manually

### Redis

#### Scheduler

```
source venv/bin/activate &&
rqscheduler
```

#### Workers

```
source venv/bin/activate &&
rq worker fitzflix-user-request
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
