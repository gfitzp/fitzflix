# CloudFront infrastructure for a library rebuild

These scripts make and remove the CloudFront distribution that Fitzflix uses when `AWS_DOWNLOAD_VIA_CDN` is set. With the flag, each restore download fetches the archived original over HTTPS at the flat-rate CloudFront price, not at the per-GB S3 egress price. A full rebuild of the library costs about $90 for each TB through S3. Through CloudFront, the Pro pricing plan covers 50 TB for $15 a month.

The distribution is not a cache. It is a private bucket behind an Origin Access Control, signed URLs from a trusted key group, and an optional WAF IP allowlist. Only the download leg changes. The Glacier restore requests, the restore-completed notifications, and the SQS poll stay on the S3 API.

## Why an S3 access point?

A bucket name with a dot does not match the wildcard TLS certificate of S3 for the `bucket.s3.region.amazonaws.com` address. A CloudFront origin needs a valid certificate. Thus, a distribution that points at such a bucket fails with 502. The setup script makes an S3 Access Point on the bucket, and it uses the dot-free **alias** of the access point as the origin domain. This is a documented CloudFront pattern. It needs OAC, which the script uses in each case, and it also works for a bucket name without a dot. The object access comes from a bucket-policy delegation to the access points of the account, plus an access-point policy that names this one distribution.

## Setup

The scripts need the CloudFront, WAFv2, S3, and S3 Control permissions. The runtime credentials of Fitzflix do not have them. Do not widen the long-lived runtime credentials. Make a separate, temporary IAM user for the rebuild, and give it its own AWS CLI profile.

Two names are important here:

- The **IAM user name** is the name of the user in your AWS account. The steps below use `fitzflix-cdn-admin`.
- The **profile name** is the label of the access keys of that user in `~/.aws/credentials`. You select it when you run `aws configure --profile`. The scripts read it from `ADMIN_PROFILE`.

The two names can be different. Use the same name for both. Thus, you do not confuse them. The steps below use `fitzflix-cdn-admin` for both.

Do steps 1 to 4 with credentials that can manage IAM users (your usual administrator profile). Do them in this `infra` directory.

1. Make the temporary IAM user:

       aws iam create-user --user-name fitzflix-cdn-admin

2. Make your copy of the least-privilege policy. Git ignores `iam-cdn-admin-policy.json`, so your values never enter the repository:

       cp iam-cdn-admin-policy_example.json iam-cdn-admin-policy.json

   Edit `iam-cdn-admin-policy.json`, and replace the three placeholders:

   - `YOUR_ACCOUNT_ID`: the 12-digit ID of your AWS account. `aws sts get-caller-identity` shows it.
   - `YOUR_BUCKET_NAME`: the value of the `AWS_BUCKET` line in the project `.env` file.
   - `YOUR_REGION`: the region of the bucket. This is usually `us-east-1`.

3. Attach the policy to the user:

       aws iam put-user-policy --user-name fitzflix-cdn-admin \
           --policy-name fitzflix-cdn-admin \
           --policy-document file://iam-cdn-admin-policy.json

4. Make access keys for the user. The output shows the secret access key one time only:

       aws iam create-access-key --user-name fitzflix-cdn-admin

5. Save the keys as a named profile. Type the access key ID and the secret access key from step 4. Type `us-east-1` as the default region:

       aws configure --profile fitzflix-cdn-admin

   Make sure that the profile works. The output must show the ARN of `user/fitzflix-cdn-admin`:

       aws sts get-caller-identity --profile fitzflix-cdn-admin

   New keys can need some seconds before they work. If this command fails, wait a short time and run it again.

6. Run the setup script with the profile name from step 5. It makes the distribution and each supporting resource. These are the key pair, the key group, the access point, the OAC, the WAF rules, and the bucket and access-point policies:

       ADMIN_PROFILE=fitzflix-cdn-admin ./setup-cloudfront-cdn.sh

Keep the user and the profile until you complete the teardown. The teardown script also uses them.

The script reads the bucket name from the `AWS_BUCKET` line of the project `.env` file. `BUCKET` in the environment overrides it. `REGION` defaults to `us-east-1`. The first run saves these values to `~/.fitzflix_cdn/cdn-config.env`. The later runs and the teardown script reuse them. Thus, no installation-specific value lives in the repository.

The script is idempotent. Run it again after you fix a failure. It prompts before the one sensitive change (the delegation statement in the bucket policy), and it saves the existing policy first. The outputs go to `~/.fitzflix_cdn/cdn-config.env`:

    CDN_DOMAIN=dxxxxxxxxxxxxx.cloudfront.net
    CDN_KEY_PAIR_ID=KXXXXXXXXXXXXX
    CDN_PRIVATE_KEY=/Users/you/.fitzflix_cdn/cloudfront_private_key.pem
    CDN_DISTRIBUTION_ID=EXXXXXXXXXXXX
    CDN_ALLOWED_CIDRS=x.x.x.x/32

The private key never leaves the machine. Fitzflix signs each URL locally.

Add these lines to the `.env` file of Fitzflix, and restart the workers:

    AWS_DOWNLOAD_VIA_CDN=1
    CDN_DOMAIN=dxxxxxxxxxxxxx.cloudfront.net
    CDN_KEY_PAIR_ID=KXXXXXXXXXXXXX
    CDN_PRIVATE_KEY=/Users/you/.fitzflix_cdn/cloudfront_private_key.pem

**A separate step, in the CloudFront console only:** enroll in a flat-rate pricing plan on the "Pricing plans" page. The Pro plan at $15 a month covers 50 TB of transfer and 10M requests. Without a plan, the transfers cost about $0.085 for each GB. The plan is a monthly commitment. Enroll when the restores start, and cancel when the rebuild is complete.

To test one signed URL, ask Fitzflix for it, and send it with curl. Do these steps after you add the `CDN_*` lines to `.env`. The test does not need a worker restart.

1. Go to the project root, one level above this `infra` directory:

       cd ..

2. Activate the virtual environment of the project:

       source venv/bin/activate

3. Get an object key. Open the page of an archived file in Fitzflix. The text "Archived offsite ... as" shows its key.

4. Ask for a signed URL, and download the first byte of the object with it. Put the key in double quotes, because keys usually contain spaces, brackets, and apostrophes:

       flask aws cdn-url "untouched/Film (2021) - [Bluray-1080p].mkv" | xargs curl -s -r 0-0 -o /dev/null -w '%{http_code}\n'

The output is the "Fitzflix startup" log line and then the HTTP status:

- `206`: CloudFront sent the byte. The object is in S3 Standard, or its restore is complete. The CDN path operates.
- `403`: CloudFront did not send the byte. The object is still in cold storage, the key is incorrect, or the WAF blocks your IP address. S3 answers 403, not 404, for a key that does not exist.

Do not use a HEAD request (`curl -I`) for this test. S3 answers a HEAD request with 200 for an object in Glacier Deep Archive also, because a HEAD request reads only the metadata.

A HEAD request can help you find the cause of a 403, however. Send one with a new signed URL for the same key:

    flask aws cdn-url "untouched/Film (2021) - [Bluray-1080p].mkv" | xargs curl -sI

- `200` with `x-amz-storage-class: DEEP_ARCHIVE` or `GLACIER`: the key is correct, and the object is in cold storage. Request a restore, and wait for it to complete. After a restore, the response also has an `x-amz-restore` line.
- `403`: the key is incorrect, or the WAF blocks your IP address.

The signed URL expires after 600 seconds. To change this, add `--expires <seconds>` after `cdn-url`.

If your public IP changes during the rebuild, update the WAF IP set in the console (WAF & Shield, IP sets, `fitzflix-cdn-ipset`). Fitzflix signs a new URL for each download attempt. Thus, nothing else needs a change.

## Teardown

When the rebuild is complete:

    ADMIN_PROFILE=fitzflix-cdn-admin ./teardown-cloudfront-cdn.sh

The script removes the distribution, which takes 10 to 20 minutes to disable and delete. It also removes the key group, the public key, the OAC, the WAF resources, and the access point. Last, it removes the delegation statement in the bucket policy. The local keys and the policy backups in `~/.fitzflix_cdn` stay. Then remove `AWS_DOWNLOAD_VIA_CDN` and the `CDN_*` lines from `.env`, and restart the workers. With the flag set and the distribution gone, each restore download fails.

Then delete the temporary IAM user. Use the same administrator credentials as in setup steps 1 to 4. Get the access key ID from `aws iam list-access-keys --user-name fitzflix-cdn-admin`:

    aws iam delete-access-key --user-name fitzflix-cdn-admin \
        --access-key-id <access-key-id>
    aws iam delete-user-policy --user-name fitzflix-cdn-admin \
        --policy-name fitzflix-cdn-admin
    aws iam delete-user --user-name fitzflix-cdn-admin

Last, remove the `[fitzflix-cdn-admin]` section from `~/.aws/credentials` and the `[profile fitzflix-cdn-admin]` section from `~/.aws/config`.
