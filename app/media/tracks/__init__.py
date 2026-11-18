"""Read and rewrite the streams inside a media file.

- scan: the scan of the tracks of a file with MediaInfo, and the track
  records. This includes the parse of the Dolby Vision profile.
- languages: the language codes and the language names of the tracks.
- edits: the edits of the tracks of a file. These are the flags, the
  languages, the order, and the removal of tracks.
- remux: the remux of a library file with mkvmerge, through the staging
  area.
- rewrite_guard: the checks before and after a task rewrites a library
  file.
- audio_supplements: the second audio tracks for players that cannot
  decode a lossless track.

A module calls a function of another module of this package through
the module, for example scan.get_audio_tracks_from_file. Thus, a test
that replaces a function in the module that defines it changes each
caller. The shared lock, subprocess, and copy code is in
app.media.videos.
"""
