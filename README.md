# fitzflix
Fitzflix is a media library manager. Its primary use cases are film library, shopping list, and watch history.

<img width="1208" alt="Screen Shot 2022-05-31 at 11 50 36 AM" src="https://user-images.githubusercontent.com/10539597/171218753-2616f91e-677a-483b-bceb-03048b372df3.png">

Fitzflix receives the video files of movies and TV shows. It uploads each file to AWS S3 Glacier Deep Archive storage as a backup. It sorts the files into a folder hierarchy that Plex can read. It removes the audio tracks and the subtitle tracks that are not in your language, which decreases the file size. It shows the movies and the TV shows that are in your library, and the format of each file. This helps you to upgrade the quality of your library.

Fitzflix also adds a personal discovery layer on top of the library. It makes a taste profile from your viewing diary. Each night, this profile makes new recommendation shelves. The layer also includes a watchlist, award data and streaming availability data for each film, and a virtual live-TV dial for Plex. Refer to [Discovery](#discovery-the-landing-page-and-the-recommendation-engine).

Fitzflix sorts files with these names:

<img width="602" alt="Screen Shot 2022-05-31 at 11 59 46 AM" src="https://user-images.githubusercontent.com/10539597/171218705-b31a6263-0fc2-489e-8f9f-efdc3f00fae3.png">

into this structure:

<img width="358" alt="Screen Shot 2022-05-31 at 12 05 56 PM" src="https://user-images.githubusercontent.com/10539597/171219194-941736ed-95e2-4dd5-889d-07de0323c4a7.png">

The application shows the files as follows:

<img width="1219" alt="Screen Shot 2022-05-31 at 11 55 06 AM" src="https://user-images.githubusercontent.com/10539597/171219305-080c44a5-7455-42d0-8dd5-119fbbf1bd36.png">

<img width="1215" alt="Screen Shot 2022-05-31 at 12 15 25 PM" src="https://user-images.githubusercontent.com/10539597/171221742-e41c84d5-3c3b-47a0-9847-16cdfd65d8b4.png">

The application also shows the related information from TMDB:

<img width="1208" alt="Screen Shot 2022-05-31 at 11 53 15 AM" src="https://user-images.githubusercontent.com/10539597/171219470-d5d819a0-aa6e-4dc7-a09e-3aa97881936a.png">

You can write reviews of films to keep a record of the films that you saw. The reviews operate with [Letterboxd](https://letterboxd.com) in two directions. The My Movie Reviews page imports a Letterboxd account-export zip file without changes. The import combines `diary.csv` (watch dates), `ratings.csv`, `reviews.csv`, `likes/films.csv`, and `watchlist.csv` into review records and watchlist records. It matches each film against the library or against TMDB. If you saw a film that you do not own, the import makes a review-only record for it. The import is idempotent: if you import a newer export, the import updates the records and does not make copies.

The export button sends an email with a CSV file in the [Letterboxd import format](https://letterboxd.com/about/importing-data/). You can upload this file to the Letterboxd importer. By default, the export contains only the entries that you added or changed after the last export. A checkbox selects a full export.

Fitzflix keeps the diary in sync with Letterboxd automatically. Enter a Letterboxd username on the Profile page. Fitzflix then reads the public RSS feed of that account two times each hour. Each diary entry or review in the feed has a feed id. Fitzflix uses the feed id to merge the entry into the local diary. A new watch adds a new row.

A review that you changed on Letterboxd updates the related row. If Plex recorded a watch of the same film, Fitzflix adds the Letterboxd data to that row. The data includes the rating, the review text, the like, the rewatch flag, and the spoiler flag. Fitzflix does not make a second row. Thus, one viewing stays as one row, whatever the number of systems that report it.

Reviews keep the inline formatting of Letterboxd. Tags such as `<i>` and `<b>` show as formatting, never as visible tags. Fitzflix stores likes without changes, so a film with a like and a low rating keeps its heart. The CSV export does not include rows that came from the feed. This prevents a loop between the two directions. The CSV import stays the correct method for history that is older than the feed window of approximately 50 items.

<img width="1206" alt="Screen Shot 2022-05-31 at 11 56 40 AM" src="https://user-images.githubusercontent.com/10539597/171219852-9de3c5de-863f-4c9a-b88f-c844186e57ca.png">

Fitzflix also manages TV shows:

<img width="1204" alt="Screen Shot 2022-05-31 at 11 56 13 AM" src="https://user-images.githubusercontent.com/10539597/171219677-f56fa57b-e55b-4dc1-974e-ddfec5a40f69.png">

Fitzflix also makes a shopping list of the films that you can upgrade. For example, the list shows a full screen film that has a widescreen version. It also shows a DVD that you can replace with a Blu-ray:

<img width="1203" alt="Screen Shot 2022-05-31 at 11 55 31 AM" src="https://user-images.githubusercontent.com/10539597/171219618-695489d4-adc7-4af5-97b2-90c47a74e223.png">


## How to use

Put video files into the import directory. The setting `IMPORT_DIR` gives the location. The default location is `../fitzflix/import`, relative to the application. Fitzflix monitors this directory and processes each file that it finds. The process has these steps:

1. Fitzflix parses the filename to identify the movie or the TV episode.
2. Fitzflix finds the standard title on TMDB.
3. Fitzflix removes the audio tracks and the subtitle tracks that are not in your language.
4. Fitzflix moves the file into a folder hierarchy that Plex can read, below the library directory.
5. If you configured AWS, Fitzflix uploads the original file to AWS S3 for archival.

Before the import, Fitzflix makes sure that the copy of the file is complete. If the size of a file continues to change, Fitzflix waits. If the container of a Matroska file or an MP4 file shows truncation, Fitzflix waits until the structure of the file is complete. A partial copy or a stalled copy shows truncation. If Fitzflix cannot probe the format of a file, it waits until the file did not change for approximately two minutes.

When you copy files manually over a slow or unreliable connection, use a temporary name that the importer ignores. A name that starts with a dot, for example `.Movie (2021) - [Bluray-1080p].mkv`, is one option. A name with an extension that is not a video extension, for example `.partial`, is an other option. When the copy is complete, rename the file to its correct name. The directory monitor and the hourly sweep ignore the names that start with a dot. Thus, the rename makes the file visible, and Fitzflix cannot import a partial file.

### Naming movies

```
Title (Year) - [Quality].ext
```

The quality tag must be one of the known quality titles. Examples are `SDTV`, `DVD`, `WEBDL-480p`, `HDTV-720p`, `WEBRip-1080p`, `Bluray-1080p`, and `Bluray-2160p Remux`. Fitzflix rejects a file that has an unknown quality tag. An `{edition-...}` tag identifies an alternative cut of the film. A version string between the title and the quality can identify a full screen version.

A Plex external-id tag can follow the year. The tag is `{tmdb-NNN}`, `{imdb-ttNNN}`, or `{tvdb-NNN}`. The tag and the `{edition-...}` tag can be in either order. The id selects the exact title on TMDB, and Fitzflix does not do a search by name. The library folder and the filename keep the tag in its `{tmdb-NNN}` form. If the name has a `{tmdb-NNN}` tag, you can omit the year. If TMDB does not know the id, Fitzflix rejects the file and does not do a search by title:

| Input filename | Sorted into the library as |
| --- | --- |
| `Jaws (1975) - [Bluray-1080p].mkv` | `Movies/Jaws (1975)/Jaws (1975) - [Bluray-1080p].mkv` |
| `Blade Runner (1982) {edition-Final Cut} - [Bluray-2160p].mkv` | `Movies/Blade Runner (1982) {edition-Final Cut}/Blade Runner (1982) {edition-Final Cut} - [Bluray-2160p].mkv` |
| `The Terminator (1984) - Fullscreen [DVD].mkv` | `Movies/The Terminator (1984)/The Terminator (1984) - Full Screen [DVD].mkv` |
| `Hamilton (2025) {tmdb-556574} - [Bluray-1080p].mkv` | `Movies/Hamilton (2020) {tmdb-556574}/Hamilton (2020) {tmdb-556574} - [Bluray-1080p].mkv` (TMDB corrects the title and the year) |
| `Ran (1985) {imdb-tt0089881} {edition-Criterion} - [Bluray-2160p].mkv` | `Movies/Ran (1985) {tmdb-11645} {edition-Criterion}/Ran (1985) {tmdb-11645} {edition-Criterion} - [Bluray-2160p].mkv` |

### Naming movie special features

Add a special feature type and a name after the title. Fitzflix then puts the video in a special-feature folder in the directory of the movie. The folder name lets Plex show the video as an extra. The special feature types are `Behind The Scenes`, `Deleted Scenes`, `Featurettes`, `Interviews`, `Scenes`, `Shorts`, `Trailers`, and `Other`:

| Input filename | Sorted into the library as |
| --- | --- |
| `Jaws (1975) - Behind The Scenes - The Making of Jaws [DVD].mkv` | `Movies/Jaws (1975)/Behind The Scenes/The Making of Jaws.mkv` |
| `Jaws (1975) - Deleted Scenes - Alternate Ending [DVD].mkv` | `Movies/Jaws (1975)/Deleted Scenes/Alternate Ending.mkv` |
| `Jaws (1975) - Featurettes - From the Set [DVD].mkv` | `Movies/Jaws (1975)/Featurettes/From the Set.mkv` |
| `Jaws (1975) - Interviews - A Conversation with Steven Spielberg [DVD].mkv` | `Movies/Jaws (1975)/Interviews/A Conversation with Steven Spielberg.mkv` |
| `Jaws (1975) - Scenes - Opening Scene [DVD].mkv` | `Movies/Jaws (1975)/Scenes/Opening Scene.mkv` |
| `Jaws (1975) - Shorts - The Shark Is Not Working [DVD].mkv` | `Movies/Jaws (1975)/Shorts/The Shark Is Not Working.mkv` |
| `Jaws (1975) - Trailers - Theatrical Trailer [DVD].mkv` | `Movies/Jaws (1975)/Trailers/Theatrical Trailer.mkv` |
| `Jaws (1975) - Other - Storyboard Gallery [DVD].mkv` | `Movies/Jaws (1975)/Other/Storyboard Gallery.mkv` |

### Naming TV shows

```
Series Title - SxxEyy - Optional Episode Title [Quality].ext
```

Season `00` identifies a special. Fitzflix puts a special into the `Specials` folder of the show. A Plex external-id tag after the series title identifies the exact series. The tag stays on the show folder, where Plex reads it:

| Input filename | Sorted into the library as |
| --- | --- |
| `Doctor Who (2005) - S01E01 - [DVD].mkv` | `TV Shows/Doctor Who (2005)/Season 01/Doctor Who (2005) - S01E01 - [DVD].mkv` |
| `Doctor Who (2005) {tmdb-57243} - S01E01 - [DVD].mkv` | `TV Shows/Doctor Who (2005) {tmdb-57243}/Season 01/Doctor Who (2005) - S01E01 - [DVD].mkv` |
| `Doctor Who (2005) - S00E01 - The Christmas Invasion [HDTV-1080p].mkv` | `TV Shows/Doctor Who (2005)/Specials/Doctor Who (2005) - S00E01 - The Christmas Invasion [HDTV-1080p].mkv` |
| `Planet Earth - S01E05-E06 - [Bluray-1080p].mkv` | `TV Shows/Planet Earth/Season 01/Planet Earth - S01E05-E06 - [Bluray-1080p].mkv` |

### After import

Fitzflix records the quality of each file. Thus, the library pages show the best copy that you own of each title. The shopping list pages show the titles that you can upgrade. For example, the list shows a full screen DVD that you can replace with a widescreen Blu-ray. Fitzflix matches each movie to TMDB for the artwork, the cast and crew, and the review records.

On the file detail page, you can set the default audio track and the default subtitle track. You can also remove unwanted tracks, transcode the file with HandBrake, and manage the AWS archive copy. The track scan also records the video format, the bitrate, and the HDR format of each file. The HDR format includes the Dolby Vision profile (profile 5, 7, 8.1, and so on), which Fitzflix reads from MediaInfo. The file page shows the profile as a badge.

### Using Fitzflix on a phone

Fitzflix follows the light or dark appearance of the system automatically (Bootstrap 5.3 color modes). The installed app has a pull-to-refresh function. Pull down from the top of a page to load the page again.

You can install Fitzflix as a web app for shopping trips. Open Fitzflix in the browser of the phone. Then use **Add to Home Screen** (iOS Safari) or **Install app** (Android Chrome). The installed app opens on the landing page, which shows the recommendation shelves. The app opens in a full-screen window. The setting `start_url` in `app/static/site.webmanifest` gives the start page.

The search box in the navigation bar is always visible. Use it to find out if you own a title. The search results show the seasons and the movies that you can upgrade in amber. The search accepts the `y:` and `year:` modifiers. For example, `jaws y:1975` limits the results to that year, and `y:1980-1989` gives a range of years. A modifier without a title, for example `y:1927`, shows all titles from that year. For a TV show, the year is the year of the first episode.

When you serve Fitzflix over HTTPS, a service worker keeps the pages that you saw recently available offline. Thus, the shopping list opens in a store that has no signal. Over HTTP, you can install and use the app, but the offline cache is not available. Browsers permit service workers only in secure contexts.


## Reviews, the watchlist, and Recommendations

Each film page has a rating ladder, from **Not interested** (zero stars) to **Loved it** (five stars). You can also add a review text and a watch date. A rating of three stars or more flags the film as liked. Until you rate a film, the ladder shows the **estimated rating** of the engine in a lighter gold. The estimate shows fractions: an estimate of 3.75 fills three quarters of the fourth star. Your own ratings are whole stars, but imported Letterboxd ratings can have half stars.

If you rate a film two times on the same day, the second rating replaces the first rating for that day. Fitzflix does not record a second watch. The rating of the newest diary entry is the current rating of the film on all pages. By default, a log entry has no date. A watch from Plex has a correct timestamp, but a film that you saw at an other location usually does not. You can add the date when you know it. You can also log a film that you do not own, from its TMDB page. Fitzflix makes a review-only record for the film, and the usual TMDB refresh adds the details.

Each user also has a **watchlist**. Each movie page has a control that adds the film to the watchlist or removes it. This applies to owned films and to films that you do not own. The My Watchlist page shows the watchlist as a poster gallery. The gallery shows the streaming availability, the Radarr request buttons, and a removal control for each film. When you log a film, Fitzflix removes it from the watchlist. This applies to a manual log, a Plex watch, and a Letterboxd import. The films on the watchlist have an effect on the recommendation shelves (below), but they do not block the shelves. They also add a small interest signal to the taste profile.

The **Recommendations** page gives deep discovery beyond the landing shelves. It shows ten shelves of films that the taste profile ranks. The types of shelf change each day. The types are genre shelves, decade shelves, and co-preference shelves ("liked by people who liked…"). In a co-preference shelf, the two films always have a genre in common. Sometimes a shelf has one anchor film at the front and its nearest neighbors after it. On a phone, the anchor card stays in position, and you swipe through the other cards one at a time.

The Recommendations page replaced the earlier Rate Films drive. Now you add taste data where you browse. The tile and the hover card of each unrated film have the rating ladder with the estimate. Thus, your ratings collect on all pages. The controls **Add to watchlist** and **No Opinion** are always available. Use **No Opinion** for a film that you never saw, or that you saw but do not remember.

Fitzflix shows films as poster walls on most pages. These pages include:

- the landing shelves and rails
- the movie library
- the filmographies
- the movie results of the local search
- the watchlist
- the Criterion spine catalog
- the leaving-Criterion pages
- the suggestion strips

Each poster has a **hover card**. Move the pointer over the poster, or tap the poster on a phone, to open the card. The card shows the credits, the synopsis, the availability badges, the rating ladder, and a watchlist control. The ladder shows your rating, or the estimate of the engine until you rate the film.

On a phone, a tap on the poster only opens or closes the card. Tap at an other location to close the card. Open the page of the film through the title on the card. All controls on the card operate without a change of page. A rating or a watchlist change from a card does not change the "Since you liked…" strip of the rating drive. Only a rating of the featured film, or a rating on the page of a film, changes the drive.

The search results, the TMDB results, the filmographies, and the movie pages show **funnel badges** for each user. The badges are *Might interest you* (taste profile), *On your watchlist* (intent), and *Seen* (diary), in that sequence.

## Discovery: the landing page and the recommendation engine

The landing page answers the question "what do we watch tonight?". The top shelf is **your watchlist, tonight**. It shows only the films on your watchlist that you can watch now: the films that you own, or the films that stream on your services. The films that leave the Criterion Channel this month come first. The other films keep the sequence that you set.

Each rail below the top shelf is discovery only, and does not include films from the watchlist:

- A **library shelf** of owned films. Fitzflix selects the films from a pool that the taste profile ranks. A film does not repeat in approximately one month.
- A **Watch it again** shelf of old favorites that you did not see for two years or more.
- A **streaming shelf** of films on the services that you selected (refer to [Streaming availability](#streaming-availability)).
- **Newly added** shelves of recent arrivals in the catalogs of your services. Today, the shelves cover the Criterion Channel only, but the mechanism operates per provider. The page `/newly-added` shows the full inventory of arrivals.
- For Criterion Channel subscribers, an **On Criterion24/7 now** card. The card shows the film that the 24/7 feed of the Channel plays at this moment. A poller reads [whatsonnow.criterionchannel.com](https://whatsonnow.criterionchannel.com) and reads it again when each film ends. If the director of the match agrees, the card shows the TMDB poster and the rating ladder. The card also shows the credits, with links to the filmographies, and the Watch Live and More links.
- A **Leaving the Criterion Channel** shelf of the films that leave this month. A full inventory page is behind the shelf.

The shelves **stay the same for the calendar day**. The same films keep the same positions all day. If a film is not applicable during the day, for example because you watched it or rejected it, Fitzflix replaces only that film. The other films keep their positions. The next day, Fitzflix selects new films. The runtime filter ("only films that fit your evening") makes a new selection for each shelf immediately. Each card shows *why* Fitzflix selected the film.

The corners of the posters show **folds** that give the status, in the style of Plex. A green fold shows a film that is new on your services, or new in the library. A red fold shows a film that leaves the Criterion Channel soon. A poster has one fold only, and the red fold has priority over the green fold. Each fold has a glyph in addition to its color. An owned film never shows the red fold, because the film does not leave your library.

The recommendation engine is content-based, and it has no machine-learning runtime dependencies. A job runs each night at 01:45. The job makes a taste profile for each user from the diary of that user. The inputs are the likes, the watches that the user selected, the rewatches, and the star ratings (with the mean removed). The job spreads these inputs across the features of the films: the genre, the decade, the language, the director, the actors, the cinematographer, the composer, the writers, the editor, and the keywords. It applies Bayesian shrinkage to the features. Then it scores each owned film that the user did not watch against the profile. Three quality signals add to the score:

- **Awards** are the wins and the nominations. Fitzflix gets them each week from [Wikidata](https://www.wikidata.org). Wikidata records some craft categories, for example Best Director, on the *person* items, with a "for work" qualifier. Fitzflix reads these too. The awards show on the movie pages. They add a limited bonus to the films that the profile already likes. Awards alone never cause a recommendation of a film that does not match your taste.
- **Co-preference** is the signal "people who loved what you loved also loved this". It comes from the [MovieLens](https://grouplens.org/datasets/movielens/) ML-32M dataset, which has 32 million ratings. Fitzflix calculates the item-to-item similarities and stores them in the database. It does this for each MovieLens film that has 50 or more raters. Thus, the signal covers films that are not in the library. A card that uses this signal says so ("liked by people who liked …"). The command `flask recs copref <extracted-ml-32m-dir>` rebuilds the table. Do this only when you adopt a new MovieLens snapshot. The command needs `numpy` and `scipy`. Install them for that command only. They are build-time tools, and they are not in `requirements.txt` on purpose. Fitzflix does not keep the dataset. Download it from GroupLens each time. The dataset has a research and non-commercial license, and you must not distribute it.
- **Watchlist interest** is a small positive weight for a film on your watchlist that you did not watch.
- **Catalog discovery** extends the recommendation universe beyond the library. Fitzflix makes records for the films in the catalogs of your flat-rate services automatically. It does this when the estimate for you is 3.0 stars or more. Thus, the streaming shelf and the newly-added shelves can recommend films that Fitzflix did not know before.

The command `flask recs evaluate` measures the full engine. It does a leave-one-out ranking over your own diary. This command is the gate for changes to the engine. A signal goes into the engine only when the metrics improve. Two signals failed this test: a person-level bonus for craft awards, and a liked flag that came from the mean rating.

## Streaming availability

Each user selects streaming services on the Profile page. All providers in the TMDB registry are available. The movie pages, the TMDB search results, the filmographies, the watchlist, and the streaming shelf then show provider-logo badges. A badge shows that the film streams on *your* services. The badges show rentals only for the films that you do not own. They never show digital purchases, because this house buys physical media only. The availability data comes from JustWatch through TMDB. Fitzflix caches the data for each title for one day. Each page that shows the data has the required credit "Streaming data by JustWatch".

Fitzflix can also **send alerts** about the films on your watchlist. Select this option on the Profile page. Then the nightly availability check sends one email digest to each user. It never sends one email for each film. The digest covers the films on your watchlist that became available. A film becomes available when it gets its first local file, or when it starts to stream on a flat-rate service that you subscribe to. A film that you can only rent counts as available only if you select a separate option, because a rental has a fee. The digest also covers the films that leave the Criterion Channel soon. Each entry starts with the poster. "Recently available" covers the last month. A local file causes an alert only for the first arrival of the film, and never for a quality upgrade.

## Browsing: people and the Criterion Collection

The **People** page (Library → People) is a grid of all persons in the credits of the films in the library. You can filter the grid by cast, by crew, or by both. The default is cast. Each name links to a filmography page. This page shows the full TMDB career of the person, with library badges on the films that you own. The key crew roles (director, writer, cinematographer, composer, and editor) are first-class. They show in the search with badges for the dominant role ("Director · 41 films"). A credit line with more than one role uses the sequence of the closing credits.

The **TV library** page has its own search, with a section for series and a section for episodes. The episode results match the episode titles in the filenames. Thus, you can find an episode by its title, without knowledge of the season numbers of the show.

The **Criterion Collection** page lists the full spine catalog from Wikidata (approximately 1,350 releases), not only the library. For an owned film, the page shows if the copy is *settled*. A settled copy is a disc that you own, with a file that matches the format of the release. If the copy is not settled, the page shows an amber quality badge. This badge means "find the Criterion version". You can add a release that you do not own to the watchlist. Such a release shows a Criterion Channel badge when it streams now. The members of a box set sort at the spine of the set. The filters are: all releases, in library, and owned and settled. A full Wikidata refresh also makes library records for the spine films that Fitzflix did not know before. Thus, the full catalog stays first-class.

## Name That Frame

The library also operates as a guessing game. A nightly task extracts a pool of frames from the films in the library. The task discards black frames and frames with almost no content. An authenticated route serves the pool, so the filename of a frame cannot give a clue to the answer. The **Name That Frame** page shows the frames one at a time. It records the win rate for each difficulty, and you can reset the score.

A multiple-choice round uses **cast-aware distractors**. The wrong answers are films that have actors in common with the correct answer. Thus, the faces do not give the answer. The **Difficult** mode uses free-text guesses with tolerant matching. The match ignores punctuation, converts spelled-out numbers to digits, and accepts a subtitle alone or a title without its subtitle. The **Extra Difficult** mode shows only a small moving crop of the frame, in the active area of the picture. In a round, you can use one zoom-out in place of one guess. After that zoom-out, you can only win or surrender the round. The Easy mode shows only the films that you rated. A filter limits each mode to the films that you saw. A win in the zoomed mode gives a brag image that you can share.

## Importing from Sonarr and Radarr

Fitzflix can operate downstream of Sonarr and Radarr, and import each file that they download. In Sonarr or Radarr, add a **Webhook** connection (Settings → Connect → Webhook) with these values:

- URL: `http://<fitzflix host>:8000/api/sonarr/add` (Sonarr) or `http://<fitzflix host>:8000/api/radarr/add` (Radarr)
- Method: `POST`, with the triggers **On Import** and **On Upgrade**
- Credentials (HTTP Basic authentication): the username is the email address of an **admin** account. The password is the **API key** of that account. The admin page of the user shows the key. The webhook renames and imports files at the paths in the payload. Thus, Fitzflix refuses the key of a member account (403). The endpoint does not accept the password of the account. You can make a new key on the admin page without a change to your login password.

Fitzflix acts only on files below the root folders of the apps, as this host sees them. The settings `RADARR_ROOT_FOLDERS` and `SONARR_ROOT_FOLDERS` in `.env` give the folders, separated by colons. The defaults are the `Movies` and `TV Shows` library directories. If a webhook names a file at a different location, Fitzflix refuses it with a 400 and writes a log line.

When a download completes, Fitzflix renames the file with a *lower* quality title before the import. The quality names for physical media are only for files that come from real discs. Fitzflix does not use `Remux` for downloads:

| Sonarr/Radarr quality | Imported as |
| --- | --- |
| `DVD` | `WEBDL-480p` |
| `Bluray-480p` | `WEBDL-480p` |
| `Bluray-720p` | `WEBDL-720p` |
| `Bluray-1080p` | `WEBDL-1080p` |
| `Bluray-1080p Remux` | `WEBDL-1080p` |

A download with a custom format score below 1600 gets the label `WEBRip` in place of `WEBDL`. Fitzflix imports the file from the folder of the download client, and does not copy it to the import directory. A TV episode that aired in the last 14 days goes to the front of the import queue. After the rename, Fitzflix asks Sonarr to scan the series again, so the Sonarr records stay correct.

For the series that Sonarr manages, Sonarr is also the **episode-data authority**. Fitzflix matches its series to the Sonarr series by the library folder. A file coverage check protects the match, so a folder name that matches by chance cannot claim a show. For a matched series, Fitzflix takes the episode titles from the TVDB records of Sonarr. This function exists for the shows where the TMDB numbers are different from the numbers in the files. Daily shows and panel shows are examples. For these shows, the episode files follow the TVDB sequence.

Before Fitzflix accepts a webhook delivery, it makes sure that the structure of the downloaded file is complete. It uses the same truncation probe as the import directory. Fitzflix does not import an incomplete file, for example a stalled download or a corrupted download. Fitzflix reports the grab as **failed** to Sonarr or Radarr. The application then blocklists that release and searches for an other release. Fitzflix deletes the bad file and sends an email report. If Fitzflix cannot report the failure, the application does not download the file again. In that case, Fitzflix keeps the file in place for manual attention, and the email says so.

## Tracking Plex watches

Fitzflix can record movie watches directly from Plex. Each watch increases the shopping-list priority of the movie for the full household. If a watcher is mapped to a Fitzflix account, Fitzflix also records the watch in the diary of that user as an unrated entry. If the user logged the film before, the entry gets the rewatch flag. Together with the Letterboxd RSS sync above, the full diary loop is automatic. Plex supplies the watch with its timestamp, the Letterboxd review supplies the rating, and the sync merges them into one row.

Two sources send data to the same record logic, and the logic removes the duplicates between them. Thus, you can use the two sources together, and we recommend that you do. The webhook reports the watches immediately. The poller finds the watches that the webhook missed while Fitzflix was down. The two sources never record a Live TV play. A play on the [virtual DVR](#virtual-dvr-live-tv-channels-in-plex) is not a watch, whatever the quality of the Plex match.

### Webhook (real time; requires Plex Pass)

1. Set `PLEX_WEBHOOK_TOKEN` in `.env` to a long random string. For example, use `python3 -c "import secrets; print(secrets.token_hex(24))"` to make one. Then restart Fitzflix.
2. In Plex Web, open **Settings → Account → Webhooks** and add this URL:

   `https://<fitzflix host>/api/plex/webhook/<PLEX_WEBHOOK_TOKEN>`

Plex cannot send credentials with a webhook. Thus, the secret in the URL is the authentication. The endpoint answers 404 to all other requests. Fitzflix records only the `media.scrobble` events of movies. A scrobble occurs when Plex decides that you watched the item, at approximately 90% of the runtime. Fitzflix ignores the play, pause, and rating events, and the TV episodes. Fitzflix matches the movies to the library by their TMDB guid. This applies to the current Plex Movie agent and to the older TMDB agent.

### History poller (self-healing backstop; no Plex Pass needed)

Set `PLEX_URL` (for example `http://<plex host>:32400`) and `PLEX_TOKEN` in `.env`, and restart. Refer to [finding your token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/) for the token. A scheduled task then reads the Plex watch history each 15 minutes, from a stored cursor. Thus, the next poll finds the watches that occurred while Fitzflix was down. The first poll only sets the cursor. Fitzflix does not import the history from before you enabled the function.

### Mapping watchers to users

Each Fitzflix user can enter a **Plex username** on the Profile page. The watches of that Plex account then go into the diary of the user. The watches of accounts without a map, for example guests or managed users without a link, still count for the household shopping-list priority. They do not get a diary entry.

If Tautulli calls `/api/add-to-cart`, disable that notifier when the direct sources operate correctly. The endpoint continues to operate, but Tautulli and the direct sources would each count the same watch.

## Playing films on an Apple TV

Each user can set a **playback device**. This is the Apple TV that receives the films of the user. When the device is set, a **▶ Play on Apple TV** button shows for the user on each owned film. The button is on the page of the film and in the poster popover card. Thus, you can send a film to your screen from each poster gallery. The button of each household member targets the TV of that member.

Playback can go through **Plex or [Infuse](https://firecore.com/infuse)**. Each user selects a default player on the Profile page. Infuse playback opens the film by its TMDB id, over the Companion protocol of the Apple TV. Before the first use, do the on-screen PIN pairing from the Profile page. The file pages recommend Infuse for the formats that the Plex player cannot play, for example Dolby Vision profile 8 with DD+ Atmos audio.

For Plex playback, Fitzflix makes a play queue on the Plex server and sends it to the player over the [Plex Companion protocol](https://support.plex.tv/articles/202485658-plex-companion-getting-started/). The Plex app must be **open on the device**. tvOS cannot wake an app in the background. If the TV does not show Plex, the button reports "is the Plex app open?" and does not play the film.

### Server setup (once, in `.env`)

Set `PLEX_URL` and `PLEX_TOKEN` as described above. Also set **`PLEX_PLAYER_SERVER_URI`**. This is an **HTTPS** URL at which the *players* can reach the Plex server, for example `https://plex.example.com:443`. This is not `PLEX_URL`. `PLEX_URL` is the address at which Fitzflix reaches the server, and it is frequently the loopback address. Each player gets the play queue from `PLEX_PLAYER_SERVER_URI` and streams from it. Thus, the players must resolve the address from their own network positions. tvOS requires TLS, and it usually refuses an address such as `http://<lan ip>:32400`. Use one of these addresses:

- A custom domain at which you published the server (**Plex Settings → Network → Custom server access URLs**), or
- the `plex.direct` address of the server. The players can already reach this address, because the Plex app uses it for its usual streams. This command lists the addresses:

  ```
  curl -s "https://plex.tv/api/resources?includeHttps=1&X-Plex-Token=<PLEX_TOKEN>" | grep -o 'uri="[^"]*"' | sort -u
  ```

  Use the `https://…plex.direct:32400` entry that contains the IP address of the server.

Restart Fitzflix after you set the value. Without the value, the Profile page does not show the device field.

### Per-user device setup (on the Profile page)

Each user enters the private-network IP address of the device under **Profile → Playback Device**. The port is optional. The default port is `32500`, the Companion default. Fitzflix does not accept hostnames. The play command carries a Plex token. Thus, the address must be a literal on a private range (RFC1918, link-local, or the 100.64/10 range of Tailscale):

1. Find the IP address of the device. On an Apple TV, open **Settings → Network**. As an alternative, look in the client list of your router.
2. Give the device a DHCP reservation or a static IP address. This keeps the address the same.
3. On the device, open the Plex app and enable **Settings → Advertise as Player**.
4. Keep the Plex app open, and save the address. Fitzflix probes the device and reads the machine identifier from the player. Fitzflix saves only a device that it verified.
5. If the device does not answer, make sure that the app is open and that Advertise as Player is on. Also make sure that no firewall blocks TCP port 32500 between the Fitzflix server and the device.

Note: Fitzflix does not use the player discovery of Plex. That discovery sends UDP broadcasts, and a VLAN or subnet boundary stops them. A direct address operates across all boundaries.

To remove the device and its play buttons, clear the field.

**Remote households:** the server side already operates from all locations, because `PLEX_PLAYER_SERVER_URI` is a public HTTPS address. The only requirement is that the *Fitzflix server can reach port 32500 of the device*. A device in an other house usually does not permit this. A VPN solves this problem. Install a VPN such as [Tailscale](https://tailscale.com) on the device and on the Fitzflix server. Tailscale has a native Apple TV app. The user then enters the VPN address of the device as the playback device, and the function operates in the same way. Do not forward port 32500 to the internet. Companion has no authentication of its own.

Fitzflix matches the films to the Plex library by the TMDB guid. If there is no match, it does a title search and verifies the guid. Thus, no other Plex configuration is necessary. Playback can fail with a message about an empty play queue, or about a container that cannot be retrieved. The usual cause is a `PLEX_PLAYER_SERVER_URI` that the device cannot reach. Test the address again from a device on the same network as the player.

## Virtual DVR: live TV channels in Plex

The library can broadcast itself. Fitzflix emulates a network tuner, and the Live TV & DVR function of Plex treats it as cable TV. The result is a dial of 24/7 channels with a full guide grid, streamed only from the files that you own. Fitzflix seeds the default dial on the first lineup build. The default dial contains these channels:

- An all-library mix
- The movie genres with the most films in the library
- A **Criterion** channel of owned films that stream on the Criterion Channel now
- A **Leaving Soon** channel of owned films in the leaving set of the month. Each guide entry starts with the departure date.
- The TV genres with the most episodes in the library
- Theme channels such as **Game Shows** and **British Sitcoms**

After the first build, the dial is data. Edit it at **Admin → DVR Channels**. You can make, delete, renumber, or disable channels. You can match the members of a channel with rules. The rules include genres, TMDB keywords, the origin country of the network, title pins, the Criterion and leaving overlays, and movies or TV or both. A title pin is a substring match that adds a title before all other rules. You can also pin films or series by title, with suggestions as you type.

A channel can mix the two libraries. The series air as blocks in broadcast sequence, in the style of syndication. The share of each series is proportional to its depth. The cursor of each series moves some episodes forward each day. The films space themselves through the episode cycle, as a nightly feature. The lineups rebuild each night, with rotation as on the landing shelves, and after each edit. Plex shows the changes at its next guide refresh.

The schedule is deterministic, and it has no state. A channel is a stored program list and an epoch timestamp. The guide and the stream calculate the current program from the same arithmetic over the real container durations. Fitzflix probes each file with ffprobe one time and caches the duration. Thus, the guide and the stream always agree. Nothing runs when nobody watches. When Plex tunes a channel, ffmpeg opens the file at the offset of the schedule. It transcodes the video to H.264 with AC-3 audio in an MPEG-TS stream, in real time. On macOS, VideoToolbox accelerates the transcode. The cost is one hardware transcode for each tuned channel, and zero when idle. A channel play never goes into a diary (refer to [Tracking Plex watches](#tracking-plex-watches)).

Setup (Plex Pass is required for Live TV & DVR):

1. Set `DVR_TOKEN` in `.env` to a long random string. For example, use `python3 -c "import secrets; print(secrets.token_hex(16))"` to make one. Then restart. The token protects each DVR endpoint. The endpoints answer 404 to all other requests.
2. If Plex runs on a different machine than Fitzflix, also set `DVR_TUNER_URL`. This is the address at which *Plex* reaches Fitzflix. The default is `http://127.0.0.1:8000`. The loopback default is important when a CDN is in front of the public hostname, because live streams must never go through the CDN.
3. Open the **DVR Channels** admin page. It shows the two URLs that you must paste, with copy buttons.
4. In Plex, open **Settings → Live TV & DVR → Set Up**. Enter the tuner address under *"Don't see your HDHomeRun device? Enter its network address manually"*. Plex has no native M3U support. Fitzflix answers the HDHomeRun HTTP protocol at that address, and the manual entry accepts it without network discovery.
5. At the channel-setup step, enter the XMLTV guide URL (**Edit** under Country → **XMLTV**).
6. If the tuner shows but does not add on the first attempt, close the setup dialog and do Set Up again. This is a known Plex problem.
7. If Plex asks you to map the channels, map them. The channel numbers and the names of the tuner match the guide. Then let the guide download.

These size settings are optional: `DVR_GENRE_CHANNELS` and `DVR_TV_CHANNELS` give the number of movie genre channels and TV genre channels that the first build seeds. `DVR_CHANNEL_FILMS` and `DVR_CHANNEL_EPISODES` give the number of programs in each lineup. `DVR_VIDEO_BITRATE_KBPS` gives the stream quality (default 8000).

## System requirements

Fitzflix is developed and operated on macOS with [Homebrew](https://brew.sh). The default binary paths point to `/opt/homebrew/bin`. You can set each path below in the `.env` file. Thus, each platform that supplies these tools is applicable.

### Core

- **Python 3.14** (each recent Python 3 version is applicable)
- **MySQL**, the application database. Fitzflix connects through `mysql+pymysql://` (set `SQLALCHEMY_DATABASE_URI`). If the setting is empty, Fitzflix uses a local SQLite file.
- **Redis**, for the task queues, the scheduler, and the file-import locks (set `REDIS_URL`)

### Third-party binaries

| Binary | `.env` setting | Function |
| --- | --- | --- |
| [MediaInfo](https://mediaarea.net/MediaInfo) (`libmediainfo`) | — (`pymediainfo` loads it as a library) | Scans the video, audio, and subtitle tracks of each imported file |
| [mkvmerge](https://mkvtoolnix.download) (MKVToolNix) | `MKVMERGE_BIN` | Remuxes Matroska files: removes the tracks that are not in your language and the empty tracks, and converts other containers to Matroska |
| [mkvpropedit](https://mkvtoolnix.download) (MKVToolNix) | `MKVPROPEDIT_LOCATION` | Edits Matroska properties in place: the default and forced track flags, and the track statistics |
| [HandBrakeCLI](https://handbrake.fr) | `HANDBRAKE_BIN` | Transcodes library files to smaller versions for Plex (refer to `HANDBRAKE_PRESET` and `HANDBRAKE_PRESET_FILE` for the preset) |
| [ffmpeg](https://ffmpeg.org) | `FFMPEG_BIN` | Converts video |
| [AtomicParsley](https://github.com/wez/atomicparsley) | `ATOMICPARSLEY_BIN` | Removes the embedded metadata from MP4 files that Fitzflix cannot convert to Matroska |

```
brew install mediainfo mkvtoolnix handbrake ffmpeg atomicparsley mysql redis
```

At startup, Fitzflix writes a warning to the log for each configured binary or directory that it cannot find. Thus, a missing tool shows in `logs/fitzflix.log`.

### Optional services

- **TMDB API key** (`TMDB_API_KEY`): standard titles, artwork, cast and crew, and review metadata
- **AWS S3** (`AWS_BUCKET`, `AWS_ACCESS_KEY`, `AWS_SECRET_KEY`, `AWS_SQS_URL`): offsite archival of the original files in Glacier Deep Archive
- **SMTP server** (`MAIL_SERVER` and related settings): error notifications and password-reset emails
- **Sonarr** (`SONARR_URL`, `SONARR_API_KEY`)
- **Radarr** (`RADARR_URL`, `RADARR_API_KEY`, `RADARR_PROXY_URL`)
- **supervisorctl**: process management for the web app and the workers (refer below). You can also start all processes manually.

## Configuration

Make a `.env` file in the project root. The file `config.py` reads it at startup. This is a minimum configuration:

```
SECRET_KEY=<long random string>
SQLALCHEMY_DATABASE_URI=mysql+pymysql://fitzflix:<password>@localhost/fitzflix
REDIS_URL=redis://localhost:6379
MEDIA_LOCATION=/path/to/media
TMDB_API_KEY=<your TMDB API key>
```

### Notable settings

| Setting | Function |
| --- | --- |
| `MEDIA_LOCATION` | The root of the media folders. `IMPORT_DIR`, `LIBRARY_DIR`, `REJECTS_DIR`, and `TRANSCODES_DIR` default to `import`, `library`, `rejects`, and `transcoded` in this root. You can set each one separately. |
| `ISO_639_2_NATIVE_LANGUAGE` | The three-letter code of *your* language (default `eng`). The import removes the audio tracks and the subtitle tracks in other languages. Foreign-language films are an exception. |
| `SERVER_NAME`, `PREFERRED_URL_SCHEME` | The hostname and the scheme for the links in emails |
| `PREVENT_ACCOUNT_CREATION` | Disables the registration page when an admin account exists |
| `ARCHIVE_ORIGINAL_MEDIA` | Uploads each imported original file to AWS S3 for archival |
| `AWS_BUCKET`, `AWS_ACCESS_KEY`, `AWS_SECRET_KEY` | The credentials for the archival bucket. Uploads need all three. |
| `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_USE_TLS` | The SMTP server for error notifications and password-reset emails. Fitzflix sends them from `SERVER_EMAIL` to `ADMIN_EMAIL`. Both default to `MAIL_USERNAME`. |
| `PLEX_URL`, `PLEX_TOKEN`, `PLEX_WEBHOOK_TOKEN` | Direct Plex watch tracking. The URL and the token enable the 15-minute history poller. The webhook token protects the `/api/plex/webhook/<token>` endpoint (refer to [Tracking Plex watches](#tracking-plex-watches)). |
| `PLEX_PLAYER_SERVER_URI` | Remote playback. This is an HTTPS server address that the playback devices can reach. Each user selects a device on the Profile page (refer to [Playing films on an Apple TV](#playing-films-on-an-apple-tv)). |
| `DVR_TOKEN`, `DVR_TUNER_URL` | Virtual DVR channels. The token protects the tuner, guide, and stream endpoints. If the token is not set, the function is disabled. The tuner URL is the Fitzflix origin as Plex reaches it (refer to [Virtual DVR](#virtual-dvr-live-tv-channels-in-plex)). |
| `HANDBRAKE_PRESET`, `HANDBRAKE_PRESET_FILE`, `HANDBRAKE_EXTENSION` | The name of the transcode preset, an optional exported preset file that contains it, and the output container |
| `LOG_FILE`, `LOG_RETENTION_DAYS` | The location of the application log (default `logs/fitzflix.log`) and the number of days of rotated archives to keep (default 14) |
| `*_TASK_TIMEOUT` | The job timeouts for each queue, in seconds (`LOCALIZATION_TASK_TIMEOUT`, `SQL_TASK_TIMEOUT`, `UPLOAD_TASK_TIMEOUT`, `TRANSCODE_TASK_TIMEOUT`, `MKVPROPEDIT_TASK_TIMEOUT`) |

The on/off settings are `PREVENT_ACCOUNT_CREATION`, `ARCHIVE_ORIGINAL_MEDIA`, `MAIL_USE_TLS`, `IGNORE_ETAGS`, and `FORCE_UPLOAD`. A setting is on when it is in `.env` with a value of any kind. To turn a setting off, remove it from `.env`. Refer to [System requirements](#system-requirements) for the binary paths.

## Installation

```
python3 -m venv venv &&
source venv/bin/activate &&
pip install -r requirements.txt &&
pip install gunicorn &&
flask db upgrade
```

### First run

The command `flask db upgrade` makes the database schema and adds the reference data (quality titles and special feature types). Thus, imports operate immediately. Start the application. [Supervisor](#running-via-supervisor) serves it at `http://localhost:8000`, and [`flask run`](#flask) serves it at `http://localhost:5000`. Open the application in a browser and register an account. **The first registered account becomes the admin.** Then set `PREVENT_ACCOUNT_CREATION` in `.env` to disable further registration.

## Running via supervisor

In the file `fitzflix_supervisor.ini`, set the `command`, `directory`, and `user` fields to the values of your installation and your user.

```
brew install supervisor &&
cp fitzflix_supervisor.ini /opt/homebrew/etc/supervisor.d/ &&
brew services start supervisor
```

## Worker queues

Fitzflix divides the background work across six Redis queues. The supervisor configuration and the manual worker commands below decide the number of workers for each queue. The queues are:

| Queue | Function |
| --- | --- |
| `fitzflix-import` | Imports new files: parses the name, removes the tracks that are not in your language, and sorts the file into the library |
| `fitzflix-file-operation` | Does the operations on single files: S3 uploads and downloads, Matroska property edits, and moves of localized files from the staging area into the library |
| `fitzflix-transcode` | Does the HandBrake transcodes (high CPU load; usually one worker) |
| `fitzflix-sql` | Writes to the database, including the database half of the TMDB refreshes. Start exactly one worker, so the writes occur in sequence. |
| `fitzflix-user-request` | Does the jobs from the web UI and the CLI: manual scans, S3 sync, SQS polls, and the network half of the TMDB refreshes |
| `fitzflix-maintenance` | Does the scheduled maintenance: nightly log rotation and backups, recommendation recomputes, awards and Criterion refreshes, and availability cache warms. One worker. |

Fitzflix records the transcoded copies as **derived files**. Each HandBrake output gets a database record with a link to its library original. The command `flask transcodes adopt` adds records for the copies on the transcoded tree that have no record. The page of a file lists its copies. When you delete or replace an original, Fitzflix also removes its derived copies, the rows and the physical files. The derived files are not in the File table, on purpose. Thus, they can never show in the quality rankings, the shopping lists, or the replace logic of the import.

Each file that moves through the pipeline leaves a **trail** with a sequence of stages: Localizing → Moving into the library → Cataloging → Archiving to S3. Remuxes, transcodes, and restores also leave a trail. The **Pipeline Activity** page shows the trails. The link to the page is on the Library Maintenance page. The page shows a status chip for each stage: green for done, blue for running, gray for queued, amber for a retry that waits, and red for failed. The page refreshes each five seconds. The trails come from the job-lifecycle hooks around the queues and the workers. Thus, they record the deferred retries and the failures without instrumentation in the tasks. The trails stay for three days.

A TMDB refresh has two phases. The API queries run on `fitzflix-user-request`. You can run some of them at the same time, because they do not touch the database. Then the `fitzflix-sql` queue applies the payload: the record updates, the file renames, and the duplicate merges. This queue has one worker, so the database writes never run at the same time. All TMDB API traffic goes through a shared Redis rate limiter. The limit is `TMDB_REQUESTS_PER_SECOND` (default 10) across all processes. This keeps Fitzflix below the [TMDB limit of approximately 40 to 50 requests each second](https://developer.themoviedb.org/docs/rate-limiting). Fitzflix does not store the poster and cast artwork locally. The pages link to the [TMDB image CDN](https://developer.themoviedb.org/docs/image-basics) directly. The setting `TMDB_IMAGE_URL` gives the base URL. The cross-origin cache of the service worker keeps the artwork that you saw recently available offline.

## Running Manually

### Redis

#### Scheduler

One scheduler process does all recurring and deferred jobs. It uses the native cron of rq and the scheduled-job mover. It does not use a separate `rq-scheduler` package. Fitzflix evaluates the cron expressions on the local clock of the server. The System page shows the last run and the next run of each task:

```
source venv/bin/activate &&
python scheduler.py
```

#### Workers

Run a maximum of one SQL worker, so the database operations occur in sequence.

```
source venv/bin/activate &&
rq worker fitzflix-sql
```

Run one maintenance worker for the scheduled maintenance tasks, such as log rotation:

```
source venv/bin/activate &&
rq worker fitzflix-maintenance
```

Run as many of the workers below as you need:

```
source venv/bin/activate &&
rq worker fitzflix-user-request
```

```
source venv/bin/activate &&
rq worker fitzflix-import fitzflix-file-operation
```

```
source venv/bin/activate &&
rq worker fitzflix-transcode fitzflix-import fitzflix-file-operation
```

```
source venv/bin/activate &&
rq worker fitzflix-file-operation fitzflix-import
```

### Flask

```
flask run
```

## Command-line tasks

Run these commands from the project root, with the venv active. Each command puts a background job in a queue. Thus, the workers must run, or nothing occurs:

| Command | Function |
| --- | --- |
| `flask scan` | Scans the import directory for files to import. Fitzflix also monitors the directory continuously. This command starts a scan now. |
| `flask sync` | Removes the files from AWS S3 storage that are no longer in the library |
| `flask sqs` | Polls AWS SQS for completed Glacier restores and downloads them |
| `flask refresh tmdb` | Refreshes the TMDB metadata for each matched movie and TV series |
| `flask refresh tmdb movie <tmdb_id>` / `flask refresh tmdb tv <tmdb_id>` | Refreshes the TMDB metadata for one title |
| `flask refresh file <file_id>` | Scans the audio and subtitle track metadata of one file again |
| `flask refresh criterion` | Refreshes the Criterion Collection data from Wikidata. This also runs automatically on the 18th of each month, after the mid-month announcements of Criterion. |
| `flask recs recompute` | Rebuilds the taste profile and the stored recommendations of each user now, and does not wait for the nightly run at 01:45 |
| `flask recs streaming` | Rebuilds the streaming shelf now (otherwise each night at 02:15) |
| `flask recs leaving` | Refreshes the leaving-Criterion set now (otherwise on the 1st of each month) |
| `flask recs awards` | Refreshes the Wikidata award records now: first the film-item pass, then the person-item craft pass (otherwise each Monday) |
| `flask recs copref <dataset-dir>` | Rebuilds the MovieLens co-preference table from an extracted ml-32m directory. This needs `numpy` and `scipy`. Do this only when you adopt a new snapshot. |
| `flask recs evaluate` | Calculates the leave-one-out ranking metrics for the engine. This is the measure for each change to the scores. |
| `flask triage backfill` | Queues the subtitle-triage inspection aids for each existing candidate file |

The Criterion data comes from [Wikidata](https://www.wikidata.org). Fitzflix matches each movie by TMDB id, or by title and year if there is no TMDB id. The match gives the spine number and a direct link to the film page at criterion.com. Box sets are supported. Some films are only in a set, for example a Godzilla Showa-era collection or an Olympic-films collection. Such a film takes the spine number of the set. Fitzflix adds the set title automatically if nobody entered one by hand.

The refresh only adds to the values that you set by hand. It never clears a spine number, and it never replaces a set title that you entered. The in-print flag and the disc-owned flag keep their values. A full refresh also makes library records for the spine releases that Fitzflix did not know before. Thus, new titles show on the Criterion catalog page automatically.

## AWS infrastructure

Fitzflix keeps all its AWS data in one S3 bucket (`AWS_BUCKET`). The bucket has these prefixes:

| Prefix | Contents | Lifecycle |
| --- | --- | --- |
| `untouched/` (`AWS_UNTOUCHED_PREFIX`) | The archived original media. Fitzflix uploads each file at import when `ARCHIVE_ORIGINAL_MEDIA` is set. | Fitzflix uploads the objects as `STANDARD`. A day-0 lifecycle rule moves them to **Glacier Deep Archive**. |
| `backup/` (`AWS_BACKUP_PREFIX`) | The nightly database dumps and the encrypted copy of `.env` | The backup task removes the old objects after its own retention period |
| `custom-posters/` (`AWS_CUSTOM_POSTERS_PREFIX`) | A mirror of the custom artwork tree. The nightly backup syncs it, and deletions propagate. | The noncurrent versions expire after 30 days |

**Bucket versioning** stays on. It is the recovery layer for deleted or replaced objects, and the [disaster recovery](#disaster-recovery) procedure depends on it. Versioning applies to the full bucket, because S3 has no versioning for one prefix. The retention period of the noncurrent versions is different for each prefix. For `untouched/`, the previous version of a deleted or replaced original stays available for **180 days**. This period matches the minimum storage duration of Deep Archive, which is 180 days. Thus, the period has no cost. If the noncurrent versions expired sooner, the early-deletion fees would cost the same. A lifecycle rule for the full bucket also stops **incomplete multipart uploads** after one day. Thus, an interrupted archive upload or backup upload cannot collect invisible parts that have a cost.

**Restore notifications**: the bucket sends the `s3:ObjectRestore:Completed` events for `untouched/` to an SQS queue (`AWS_SQS_URL`). The hourly poll reads the queue and downloads each completed restore. Refer to [Restoring files from AWS](#restoring-files-from-aws).

### Provisioning

```bash
flask aws provision
```

This command is idempotent. It makes the components that do not exist, and it reports each component. The components are:

- the bucket
- the versioning
- the lifecycle rules
- the SQS queue
- the queue policy that lets S3 deliver to the queue
- the restore-event notification
 If the command makes a queue, it prints the `AWS_SQS_URL` line that you must add to `.env`. The command always keeps the existing configuration. It adds to the lifecycle rules and the notifications, and it never replaces them. Thus, the rules that you made by hand stay. If you run the command against an account with a full configuration, it changes nothing and says so.

Each run saves the current lifecycle configuration and notification configuration to `logs/aws-snapshots/` before it writes. If the bucket reports fewer lifecycle rules than the newest snapshot, the command stops. Use `--force` when the decrease is intentional. For example, use it when you removed all the rules to rebuild the set that the command manages.

### IAM

The app operates with a policy that is limited to its bucket and its queue. The daily operation needs this policy:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:RestoreObject", "s3:ListBucket"],
            "Resource": ["arn:aws:s3:::<bucket>", "arn:aws:s3:::<bucket>/*"]
        },
        {
            "Effect": "Allow",
            "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
            "Resource": "arn:aws:sqs:<region>:<account>:<queue>"
        }
    ]
}
```

The command `flask aws provision` also needs `s3:CreateBucket`, `s3:PutBucketVersioning`, `s3:GetBucketVersioning`, `s3:PutLifecycleConfiguration`, `s3:GetLifecycleConfiguration`, `s3:PutBucketNotification`, `s3:GetBucketNotification`, `sqs:CreateQueue`, and `sqs:SetQueueAttributes`. Grant these permissions for a short time. As an alternative, run the command one time with an admin credential, and then return to the runtime policy.

## Restoring files from AWS

The archived originals are in S3 Glacier Deep Archive. Thus, a restore has two steps:

1. On the detail page of the file, request the download. Fitzflix asks AWS to restore the object from Glacier. A restore from Deep Archive usually takes some hours. To restore many files, use the **Restore series from AWS** button on a TV series page. A season page has the **Restore season from AWS** button. Each button requests each best-ranked archived file at the same time. A restore has a cost. Thus, each restore button shows an estimated cost, and you must enter your account password to confirm. The estimate includes the fee for each request, the retrieval fee for each GB, and the transfer fee for each GB. A season or series restore uses the cheaper Bulk retrieval tier. A single file uses the Standard tier. Set the values `AWS_RESTORE_PER_1K_REQUEST_COST`, `AWS_RESTORE_PER_1K_REQUEST_BULK_COST`, `AWS_RESTORE_PER_GB_COST`, `AWS_RESTORE_PER_GB_BULK_COST`, and `AWS_DOWNLOAD_PER_GB_COST` in `.env` to match the current AWS prices.
2. When AWS completes the restore, it sends a notification to the SQS queue (`AWS_SQS_URL`). Configure the S3 bucket to send its restore-completed event notifications to that queue. Fitzflix polls the queue each hour and downloads each completed restore into the library. To poll now, run `flask sqs`.


## Disaster recovery

You can rebuild Fitzflix on a new machine from three items that you keep away from the machine: **the AWS credentials and the bucket name, the `BACKUP_PASSPHRASE`, and access to this git repository**. Keep all three in a password manager. Do these steps in sequence:

1. **Install the system requirements.** Use Homebrew to install MariaDB, Redis, supervisor, and the third-party binaries listed above. Clone this repository, make the virtualenv, and run `pip install -r requirements.txt`.
2. **Recover `.env`.** Download the newest `backup/dotenv-<date>.enc` from the S3 bucket and decrypt it into the project root:

   ```
   BACKUP_PASSPHRASE=<from your password manager> openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in dotenv-<date>.enc -out .env -pass env:BACKUP_PASSPHRASE
   ```
3. **Make the database and the user** that the recovered `SQLALCHEMY_DATABASE_URI` names. Grant the user all privileges on that database. Also add the grant for the restore drill:

   ```
   GRANT ALL PRIVILEGES ON `fitzflix_restore_check`.* TO '<user>'@'localhost';
   ```
4. **Restore the newest dump** from `backup/` in the bucket:

   ```
   zcat fitzflix_db-<date>.sql.gz | mysql --user=<user> --password <database>
   ```

   Then run `flask db upgrade` to update the schema to the current code. If the code is not newer than the dump, this command changes nothing.
5. **Restore the custom posters.** Copy the `custom-posters/` prefix of the bucket back to `app/static/custom/`. For example, run `aws s3 sync s3://<bucket>/custom-posters/ app/static/custom/`. The TMDB artwork does not need a restore. The pages link to the TMDB image CDN, and Fitzflix never stores the artwork locally.
6. **Mount the NAS volumes.** Refer to the SMB notes: put the NAS hostname in `/etc/hosts`, and set `protocol_vers_map` and the signing settings in `/etc/nsmb.conf`. Make the staging directory on the local disk again.
7. **Start the workers** with supervisor. Make sure that the health card on the System page is green. The scheduled jobs register themselves again at startup. Redis needs no restore. The only important data in Redis is the recommendation rankings and the availability caches. This data rebuilds itself in one day, or immediately with the `flask recs` commands.
8. **Only if the NAS is also lost:** you can rebuild the localized library from the untouched archives. The S3 sync task queues a Bulk restore for each rank-1 file that is not on the local disk. The file `inventory/rank_1.csv` in the bucket supports an S3 Batch Operations restore of all files at the same time.

## Logs and maintenance

All processes write to one log, `logs/fitzflix.log`. The setting `LOG_FILE` gives the location. Fitzflix reports configuration problems in the log as warnings at startup. Examples are missing binaries, media directories that Fitzflix cannot reach, and incomplete AWS or mail settings. Thus, look in the log first when a function does not operate.

The log rotates automatically each night at midnight. Fitzflix compresses the file of the day to `fitzflix.log.<date>.gz` in the same directory. It deletes the archives that are older than `LOG_RETENTION_DAYS` (default 14).

Fitzflix backs up the database each night at 00:30 to a compressed dump in `DB_BACKUP_DIR`. The default directory is `backups/` in the project root. Fitzflix keeps `DB_BACKUP_RETENTION_DAYS` (default 14) days of dumps. The media files are in the AWS archive, but the reviews, the Criterion details, and the shopping priorities are only in the database. When AWS is configured, Fitzflix also uploads each dump to the S3 bucket under `AWS_BACKUP_PREFIX` (default `backup`), in Standard storage. It removes the remote dumps that are older than the retention period on the same schedule. Thus, the loss of the machine is not the loss of the database.

The nightly backup also uploads an encrypted copy of `.env` (AES-256). This needs the `BACKUP_PASSPHRASE` setting. Keep the passphrase in a password manager, because it is the key to all other recovery. The backup also mirrors the custom posters in `app/static/custom/` to the bucket under `AWS_CUSTOM_POSTERS_PREFIX` (default `custom-posters`).

On the 1st of each month, a restore drill downloads the newest offsite dump. The drill restores it into a temporary `fitzflix_restore_check` database and compares the row counts with the live database. Thus, you find a dump that does not restore in one month, and not during a disaster. The drill needs a grant that you add one time (refer to Disaster recovery above). The System page shows the live worker health and the last run and the next run of each scheduled task. It also lists the failed background jobs, with buttons to requeue or forget them. The Library Maintenance page includes a filename tester. The tester shows how Fitzflix parses and files a filename, and it does not import the file.

### Subtitle triage

Sometimes a disc has a forced-subtitle track without the forced flag. A forced-subtitle track has the translations of the foreign dialogue. Without the flag, Plex never shows the track. The **Library Maintenance → Possibly-forced subtitles** page applies a heuristic to each file. A candidate is a track without the forced flag that has a small fraction of the cues of its largest same-language track. The page shows each candidate with inspection aids that Fitzflix made at import: a cue-density timeline, and five snapshots with the burned-in subtitles at the cue times of the track. Thus, you can easily tell a real forced track, which has few translation cues, from a trivia track or a commentary track.

When you flag the selected tracks, Fitzflix sets the forced flag in place with mkvpropedit. When you dismiss a file, Fitzflix marks it as reviewed. Each candidate also has a **per-file triage page**. The link is on the card of the candidate, and on the page of the file while candidates are pending. The page loads only the snapshots of that file. After an action, you return to the page that you came from. When you import a replacement file, Fitzflix resets the triage state of the file. A new file is new evidence, so Fitzflix shows its candidates again, even if you reviewed an earlier copy. The command `flask triage backfill` queues the aids for the files that are older than the function.

### Lossy audio triage

The **Library Maintenance → Lossy Files** report lists the files whose audio can be better. A file with the three TrueHD Atmos supplement tracks counts as settled, and the report does not include it. For some files, the selection of the lead track is a judgment call. For these files, the triage flow makes **listening-clip comparisons**. A comparison has six short clips from five points across the full runtime of each track. You can play the clips side by side in the browser. Then one click remuxes the file with the selected track in the lead, or dismisses the file as correct.

Two more review queues are available. A **TMDB triage** page lists the records whose metadata match needs a second look. A report lists the files whose measured duration does not agree with the TMDB runtime of the film. The causes are a wrong match, a partial rip, or an alternative cut with the wrong label.

### Custom posters

Each movie or file can have custom artwork. The poster picker on a movie page shows the full TMDB poster gallery, in groups by language, with the TMDB default highlighted. Select a poster with one click, or upload a poster. Fitzflix keeps the custom posters under `app/static/custom/`. The nightly backup mirrors them to the S3 bucket. You can remove a custom poster with one click. Fitzflix then uses the precedence rules of the library.

### Rejected files

Fitzflix cannot import some files. The causes are a filename that Fitzflix cannot parse, an unknown quality tag, or a failure during the process. Fitzflix moves these files into a subfolder of `REJECTS_DIR`. The name of the subfolder gives the cause. Thus, the folder name tells you what to correct. The `/rejects` page lists the files. The link to the page is on the Library Maintenance page. One click imports a file again, or deletes it. An import moves the file back to the import directory, where Fitzflix finds it automatically. The Library Maintenance page also shows the movies that have the same TMDB id. One click merges them. The merge moves the files and the reviews of the duplicates to the oldest record. The `/queue` page shows the active jobs and the queued jobs.

### Updating

```
git pull &&
venv/bin/pip install -r requirements-dev.txt &&
venv/bin/flask db upgrade &&
supervisorctl restart "fitzflix:*"
```

The file `requirements-dev.txt` includes `requirements.txt`. A runtime-only installation can use `requirements.txt` alone.

## Code formatting

[black](https://github.com/psf/black) formats the commits, and pyflakes lints them, through [pre-commit](https://pre-commit.com). Do this setup one time for each clone:

```
venv/bin/pip install -r requirements-dev.txt
```

```
venv/bin/pre-commit install
```

The hooks run on the staged Python files at each commit. The `migrations/` directory is an exception. Alembic makes those scripts, and they keep their generated format. If black changes a file, the commit stops. Then you can review the changes and stage them again. The command `git commit --no-verify` skips the hooks. The command `venv/bin/black .` formats the full tree by hand, with the same exception from `pyproject.toml`.

## Running the tests

```
venv/bin/python -m pytest
```

The test suite is fully isolated from an installation that runs. It uses a temporary SQLite database, Redis database 9 (the app uses database 0), temporary media directories, and no external services. TMDB, Sonarr, Radarr, AWS, and mail are stubs, or the configuration makes them unreachable. The only requirement in addition to `requirements-dev.txt` is a local Redis server. You can run the suite while the real application runs. The suite takes approximately 30 seconds. Run it after dependency upgrades and before you deploy changes.

The suite covers:

- the filename rules (each example table above is a test fixture)
- the webhook authentication and the Sonarr and Radarr download flow
- the deferred-retry mechanism
- the per-title lock contract
- the log rotation and the database backups
- the system-health probe and its email alerts
- the file-ranking queries
- the page rendering

## Language notes

This document uses ASD-STE100 Simplified Technical English. Product names, file names, settings, and commands are technical names. These words are technical verbs, because the domain has no plain replacement: casefold, commit, dismiss, enqueue, log, merge, mount, parse, pin, poll, probe, queue, remux, rename, restore, scan, scrobble, stream, sync, transcode, and upload.
