"""The archive of the library in AWS.

- aws_clients: the S3 and SQS clients, and the small functions that all
  the archive code uses.
- archive_keys: the key of an archive, and the records that claim a key.
- uploads: the upload of a file to its archive.
- restores: the restore of an archive from cold storage.
- downloads: the download of an archive into the library.
- cdn: the download through CloudFront.
- moves: the move and the delete of an archive object.
- sync: the weekly audit of the bucket against the library.
- s3_layout: the folders of the bucket.
- aws_setup and aws_cli: the setup of the AWS components.

A module calls a function of another module of this package through
the module, for example archive_keys.archive_key_for. Thus, a test that
replaces a function in the module that defines it changes each caller.
"""
