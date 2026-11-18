"""The import pipeline.

This is the chain that a file follows from the import directory to the
library. Each module of this package is 1 part of the chain, in this
order:

- filenames: parse and price the name of a file, and reject a file.
- chain: the jobs of the chain that wait or run, and the lock of a title.
- space: the free space of the library volume and of the staging area.
- localization: the first job. It archives the untouched original and
  removes the non-native tracks.
- library_move: the second job. It copies the localized file into the
  library. A conversion to SDR runs before it (app.media.sdr_conversion).
- cataloging: the third job. It writes the records and gives the file
  its final name.
- follow_ups: the work after the commit.
- sweep: the hourly sweep of the import directory.

A part calls a function of another part through the module, for
example filenames.evaluate_filename. Thus, a test that replaces a
function in the module that defines it changes each caller.
"""
