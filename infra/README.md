# CloudFront infrastructure for a library rebuild

These scripts make and remove the CloudFront distribution that Fitzflix uses when `AWS_DOWNLOAD_VIA_CDN` is set. With the flag, each restore download fetches the archived original over HTTPS at the flat-rate CloudFront price, not at the per-GB S3 egress price. A full rebuild of the library costs about $90 for each TB through S3. Through CloudFront, the Pro pricing plan covers 50 TB for $15 a month.

The distribution is not a cache. It is a private bucket behind an Origin Access Control, signed URLs from a trusted key group, and an optional WAF IP allowlist. Only the download leg changes. The Glacier restore requests, the restore-completed notifications, and the SQS poll stay on the S3 API.

## Why an S3 access point?

A bucket name with a dot does not match the wildcard TLS certificate of S3 for the `bucket.s3.region.amazonaws.com` address. A CloudFront origin needs a valid certificate. Thus, a distribution that points at such a bucket fails with 502. The setup script makes an S3 Access Point on the bucket, and it uses the dot-free **alias** of the access point as the origin domain. This is a documented CloudFront pattern. It needs OAC, which the script uses in each case, and it also works for a bucket name without a dot. The object access comes from a bucket-policy delegation to the access points of the account, plus an access-point policy that names this one distribution.

## Setup

Run the scripts with an AWS profile that has the CloudFront, WAFv2, S3, and S3 Control permissions. The runtime credentials of Fitzflix do not have them. The least-privilege permission set is in `iam-cdn-admin-policy_example.json`. Copy it to `iam-cdn-admin-policy.json` (git ignores that name, so your values never enter the repository), replace `YOUR_BUCKET_NAME`, `YOUR_REGION`, and `YOUR_ACCOUNT_ID`, and attach it to a dedicated IAM identity:

    cp iam-cdn-admin-policy_example.json iam-cdn-admin-policy.json
    # edit the placeholders, then:
    aws iam put-user-policy --user-name <your-cdn-user> \
        --policy-name fitzflix-cdn-admin \
        --policy-document file://iam-cdn-admin-policy.json

Use a separate, temporary IAM identity. Do not widen the long-lived runtime credentials. Delete that identity and its policy when the rebuild is complete.

Then run the setup script. It makes the distribution and each supporting resource (the key pair, the key group, the access point, the OAC, the WAF rules, and the bucket and access-point policies):

    ADMIN_PROFILE=youradminprofile ./setup-cloudfront-cdn.sh

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

To test one signed URL, ask Fitzflix for it, and send it with curl. A restored object answers 200. An object that is still in cold storage answers 403.

    flask aws cdn-url "untouched/<key>" | xargs curl -sI | head -5

If your public IP changes during the rebuild, update the WAF IP set in the console (WAF & Shield, IP sets, `fitzflix-cdn-ipset`). Fitzflix signs a new URL for each download attempt. Thus, nothing else needs a change.

## Teardown

When the rebuild is complete:

    ADMIN_PROFILE=youradminprofile ./teardown-cloudfront-cdn.sh

The script removes the distribution (10 to 20 minutes to disable and delete), the key group, the public key, the OAC, the WAF resources, the access point, and the delegation statement in the bucket policy. The local keys and the policy backups in `~/.fitzflix_cdn` stay. Then remove `AWS_DOWNLOAD_VIA_CDN` and the `CDN_*` lines from `.env`, and restart the workers. With the flag set and the distribution gone, each restore download fails.
