#!/bin/bash
#
# setup-cloudfront-cdn.sh: make the CloudFront distribution that Fitzflix
# uses for the AWS_DOWNLOAD_VIA_CDN download path. The path fetches the
# archived originals from the private S3 bucket without the per-GB S3
# egress charge. Refer to infra/README.md.
#
# The script is idempotent. It is safe to run it again. It keeps and
# reuses each resource that exists. It makes:
#   1. An RSA key pair for the CloudFront signed URLs. The private key
#      stays on this machine.
#   2. A CloudFront public key and a key group. The key group is the
#      trusted signer of the distribution.
#   3. An S3 Access Point on the bucket. Its dot-free alias is the
#      CloudFront origin domain, not the bucket name. A bucket name with
#      a dot does not match the wildcard TLS certificate of S3. Thus, a
#      direct origin fails with 502. The alias works for each bucket name.
#   4. A CloudFront Origin Access Control (sigv4, always sign).
#   5. Optionally, a WAF WebACL that blocks each address that is not in
#      ALLOWED_CIDRS.
#   6. A CloudFront distribution: GET and HEAD only, HTTPS only, signed
#      URLs required, caching disabled, IPv6 off. Thus, the WAF and the
#      policy IP checks always see an IPv4 address.
#   7. A bucket-policy delegation to the access point, and an access-point
#      policy that grants s3:GetObject to CloudFront for this one
#      distribution.
#
# Requirements: aws CLI v2, openssl, python3, curl.
# The AWS profile must have the CloudFront, WAFv2, S3, and S3 Control
# permissions. The runtime credentials of Fitzflix do not have them. Use
# a separate admin profile (refer to iam-cdn-admin-policy_example.json).
#
# Usage:
#   ADMIN_PROFILE=myadmin ./setup-cloudfront-cdn.sh
#
# The script reads BUCKET from the environment, then from a saved
# KEY_DIR/cdn-config.env, then from the AWS_BUCKET line of the .env file
# of the project. The first run saves BUCKET, REGION, and PREFIX to
# cdn-config.env. The later runs and the teardown script reuse them.
#
# Optional overrides (environment variables):
#   REGION          the bucket region (default: us-east-1)
#   PREFIX          the name prefix of each resource (default: fitzflix-cdn)
#   ALLOWED_CIDRS   comma-separated IPv4 CIDRs for the WAF allowlist
#                   (default: auto = the current public IP as a /32)
#   ENABLE_WAF      1 to make and attach the WAF WebACL (default: 1)
#   KEY_DIR         where the keys and the outputs go (default: ~/.fitzflix_cdn)
#   ASSUME_YES      1 to skip the bucket-policy confirmation prompt
#
# NOTE: the CloudFront flat-rate pricing plan (Pro: $15 a month for 50 TB)
# is a separate step in the CloudFront console, on the "Pricing plans"
# page. This script cannot enroll for you.

set -euo pipefail

ADMIN_PROFILE=${ADMIN_PROFILE:?set ADMIN_PROFILE to an AWS profile with admin permissions}
KEY_DIR=${KEY_DIR:-$HOME/.fitzflix_cdn}
CONFIG_FILE="$KEY_DIR/cdn-config.env"
PROJECT_ENV="$(cd "$(dirname "$0")/.." && pwd)/.env"

# Reuse the settings that a previous run saved, unless the environment
# overrides them.
CDN_BUCKET=""; CDN_REGION=""; CDN_PREFIX=""
[ -f "$CONFIG_FILE" ] && . "$CONFIG_FILE"

BUCKET=${BUCKET:-$CDN_BUCKET}
if [ -z "$BUCKET" ] && [ -f "$PROJECT_ENV" ]; then
    BUCKET=$(sed -n 's/^AWS_BUCKET=//p' "$PROJECT_ENV" | tail -1 | tr -d '"'"'")
    [ -n "$BUCKET" ] && echo "bucket from $PROJECT_ENV: $BUCKET"
fi
[ -n "$BUCKET" ] || { echo "error: set BUCKET to the S3 bucket that holds the archive" >&2; exit 1; }
REGION=${REGION:-${CDN_REGION:-us-east-1}}
PREFIX=${PREFIX:-${CDN_PREFIX:-fitzflix-cdn}}
ALLOWED_CIDRS=${ALLOWED_CIDRS:-auto}
ENABLE_WAF=${ENABLE_WAF:-1}
ASSUME_YES=${ASSUME_YES:-0}

AP_NAME="${PREFIX//_/-}-ap"
BUCKET_POLICY_SID="FitzflixCdnDelegateToAccessPoints"
CACHING_DISABLED_POLICY_ID="4135ea2d-6df8-44a3-9df3-4b5a84be39ad"  # AWS managed "CachingDisabled"

awsx() { aws --profile "$ADMIN_PROFILE" --output text "$@"; }
say()  { printf '\n==> %s\n' "$*"; }

for tool in aws openssl python3 curl; do
    command -v "$tool" >/dev/null || { echo "error: $tool not found" >&2; exit 1; }
done

say "Verifying credentials"
ACCOUNT=$(awsx sts get-caller-identity --query Account)
echo "account: $ACCOUNT ($(awsx sts get-caller-identity --query Arn))"

BUCKET_REGION=$(awsx s3api get-bucket-location --bucket "$BUCKET" --query 'LocationConstraint' | sed 's/^None$/us-east-1/')
[ "$BUCKET_REGION" = "$REGION" ] || { echo "error: bucket is in $BUCKET_REGION, not $REGION" >&2; exit 1; }

if [ "$ALLOWED_CIDRS" = "auto" ]; then
    ALLOWED_CIDRS="$(curl -s https://checkip.amazonaws.com)/32"
    echo "auto-detected public IP allowlist: $ALLOWED_CIDRS"
fi

mkdir -p "$KEY_DIR"
chmod 700 "$KEY_DIR"

# ---------------------------------------------------------------- 1. key pair
say "Signed-URL key pair"
PRIVATE_KEY="$KEY_DIR/cloudfront_private_key.pem"
PUBLIC_KEY="$KEY_DIR/cloudfront_public_key.pem"
if [ ! -f "$PRIVATE_KEY" ]; then
    openssl genrsa -out "$PRIVATE_KEY" 2048 2>/dev/null
    chmod 600 "$PRIVATE_KEY"
    echo "generated $PRIVATE_KEY"
else
    echo "reusing $PRIVATE_KEY"
fi
openssl rsa -in "$PRIVATE_KEY" -pubout -out "$PUBLIC_KEY" 2>/dev/null

# ------------------------------------------------------- 2. CloudFront pubkey
say "CloudFront public key"
PUBKEY_ID=$(awsx cloudfront list-public-keys \
    --query "PublicKeyList.Items[?Name=='$PREFIX-pubkey'].Id | [0]")
if [ "$PUBKEY_ID" = "None" ] || [ -z "$PUBKEY_ID" ]; then
    python3 - "$PREFIX-pubkey" "$PUBLIC_KEY" > "$KEY_DIR/pubkey-config.json" <<'PY'
import json, sys
name, pem_path = sys.argv[1], sys.argv[2]
json.dump({"CallerReference": name, "Name": name,
           "EncodedKey": open(pem_path).read(),
           "Comment": "Fitzflix CDN signed URL key"}, sys.stdout)
PY
    PUBKEY_ID=$(awsx cloudfront create-public-key \
        --public-key-config "file://$KEY_DIR/pubkey-config.json" \
        --query 'PublicKey.Id')
    echo "created public key $PUBKEY_ID"
else
    echo "reusing public key $PUBKEY_ID"
fi

# ----------------------------------------------------------------- 3. key group
say "CloudFront key group"
KEYGROUP_ID=$(awsx cloudfront list-key-groups \
    --query "KeyGroupList.Items[?KeyGroup.KeyGroupConfig.Name=='$PREFIX-keygroup'].KeyGroup.Id | [0]")
if [ "$KEYGROUP_ID" = "None" ] || [ -z "$KEYGROUP_ID" ]; then
    KEYGROUP_ID=$(awsx cloudfront create-key-group \
        --key-group-config "{\"Name\":\"$PREFIX-keygroup\",\"Items\":[\"$PUBKEY_ID\"]}" \
        --query 'KeyGroup.Id')
    echo "created key group $KEYGROUP_ID"
else
    echo "reusing key group $KEYGROUP_ID"
fi

# -------------------------------------------------------------- 4. access point
say "S3 access point (its dot-free alias is the CloudFront origin domain)"
AP_ALIAS=$(awsx s3control get-access-point --account-id "$ACCOUNT" --name "$AP_NAME" \
    --region "$REGION" --query 'Alias' 2>/dev/null || true)
if [ -z "$AP_ALIAS" ] || [ "$AP_ALIAS" = "None" ]; then
    awsx s3control create-access-point --account-id "$ACCOUNT" --name "$AP_NAME" \
        --bucket "$BUCKET" --region "$REGION" >/dev/null
    AP_ALIAS=$(awsx s3control get-access-point --account-id "$ACCOUNT" --name "$AP_NAME" \
        --region "$REGION" --query 'Alias')
    echo "created access point $AP_NAME (alias: $AP_ALIAS)"
else
    echo "reusing access point $AP_NAME (alias: $AP_ALIAS)"
fi
ORIGIN_DOMAIN="$AP_ALIAS.s3.$REGION.amazonaws.com"

# ------------------------------------------------------------------------ 5. OAC
say "Origin access control"
OAC_ID=$(awsx cloudfront list-origin-access-controls \
    --query "OriginAccessControlList.Items[?Name=='$PREFIX-oac'].Id | [0]")
if [ "$OAC_ID" = "None" ] || [ -z "$OAC_ID" ]; then
    OAC_ID=$(awsx cloudfront create-origin-access-control \
        --origin-access-control-config "{\"Name\":\"$PREFIX-oac\",\"Description\":\"Fitzflix CDN\",\"SigningProtocol\":\"sigv4\",\"SigningBehavior\":\"always\",\"OriginAccessControlOriginType\":\"s3\"}" \
        --query 'OriginAccessControl.Id')
    echo "created OAC $OAC_ID"
else
    echo "reusing OAC $OAC_ID"
fi

# ------------------------------------------------------------------------ 6. WAF
WEBACL_ARN=""
if [ "$ENABLE_WAF" = "1" ]; then
    say "WAF IP allowlist ($ALLOWED_CIDRS)"
    IPSET_ARN=$(awsx wafv2 list-ip-sets --scope CLOUDFRONT --region us-east-1 \
        --query "IPSets[?Name=='$PREFIX-ipset'].ARN | [0]")
    if [ "$IPSET_ARN" = "None" ] || [ -z "$IPSET_ARN" ]; then
        ADDR_JSON=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1].split(',')))" "$ALLOWED_CIDRS")
        IPSET_ARN=$(awsx wafv2 create-ip-set --scope CLOUDFRONT --region us-east-1 \
            --name "$PREFIX-ipset" --ip-address-version IPV4 \
            --addresses "$ADDR_JSON" --query 'Summary.ARN')
        echo "created IP set"
    else
        echo "reusing IP set (NOTE: its addresses stay as they are; edit them in the console if your IP changed)"
    fi
    WEBACL_ARN=$(awsx wafv2 list-web-acls --scope CLOUDFRONT --region us-east-1 \
        --query "WebACLs[?Name=='$PREFIX-acl'].ARN | [0]")
    if [ "$WEBACL_ARN" = "None" ] || [ -z "$WEBACL_ARN" ]; then
        WEBACL_ARN=$(awsx wafv2 create-web-acl --scope CLOUDFRONT --region us-east-1 \
            --name "$PREFIX-acl" --default-action 'Block={}' \
            --visibility-config "SampledRequestsEnabled=true,CloudWatchMetricsEnabled=true,MetricName=${PREFIX//-/}" \
            --rules "[{\"Name\":\"allow-listed-ips\",\"Priority\":0,\"Statement\":{\"IPSetReferenceStatement\":{\"ARN\":\"$IPSET_ARN\"}},\"Action\":{\"Allow\":{}},\"VisibilityConfig\":{\"SampledRequestsEnabled\":true,\"CloudWatchMetricsEnabled\":true,\"MetricName\":\"allowips\"}}]" \
            --query 'Summary.ARN')
        echo "created WebACL"
    else
        echo "reusing WebACL"
    fi
fi

# --------------------------------------------------------------- 7. distribution
say "CloudFront distribution"
DIST_ID=$(awsx cloudfront list-distributions \
    --query "DistributionList.Items[?Comment=='$PREFIX'].Id | [0]" 2>/dev/null || true)
if [ "$DIST_ID" = "None" ] || [ -z "$DIST_ID" ]; then
    cat > "$KEY_DIR/distribution-config.json" <<EOF
{
  "CallerReference": "$PREFIX",
  "Comment": "$PREFIX",
  "Enabled": true,
  "PriceClass": "PriceClass_100",
  "IsIPV6Enabled": false,
  "HttpVersion": "http2",
  "WebACLId": "$WEBACL_ARN",
  "Origins": {
    "Quantity": 1,
    "Items": [{
      "Id": "s3-origin",
      "DomainName": "$ORIGIN_DOMAIN",
      "OriginAccessControlId": "$OAC_ID",
      "S3OriginConfig": {"OriginAccessIdentity": ""},
      "OriginShield": {"Enabled": false},
      "ConnectionAttempts": 3,
      "ConnectionTimeout": 10
    }]
  },
  "DefaultCacheBehavior": {
    "TargetOriginId": "s3-origin",
    "ViewerProtocolPolicy": "https-only",
    "AllowedMethods": {
      "Quantity": 2, "Items": ["GET", "HEAD"],
      "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}
    },
    "CachePolicyId": "$CACHING_DISABLED_POLICY_ID",
    "Compress": false,
    "TrustedKeyGroups": {"Enabled": true, "Quantity": 1, "Items": ["$KEYGROUP_ID"]},
    "TrustedSigners": {"Enabled": false, "Quantity": 0}
  }
}
EOF
    DIST_ID=$(awsx cloudfront create-distribution \
        --distribution-config "file://$KEY_DIR/distribution-config.json" \
        --query 'Distribution.Id')
    echo "created distribution $DIST_ID"
else
    echo "reusing distribution $DIST_ID"
fi
DIST_DOMAIN=$(awsx cloudfront get-distribution --id "$DIST_ID" --query 'Distribution.DomainName')
DIST_ARN="arn:aws:cloudfront::$ACCOUNT:distribution/$DIST_ID"

# ------------------------------------------------------------------- 8. policies
say "Bucket policy (delegate the object access to the access points of this account)"
POLICY_BACKUP="$KEY_DIR/bucket-policy-backup-$(date +%Y%m%d%H%M%S).json"
# Only "no bucket policy" may read as empty. Any other failure (a denied
# GetBucketPolicy, throttling) must stop the script, or the merge below
# would replace the real policy with this script's single statement.
POLICY_ERR=$(mktemp)
if EXISTING_POLICY=$(awsx s3api get-bucket-policy --bucket "$BUCKET" --query 'Policy' 2>"$POLICY_ERR"); then
    :
elif grep -q NoSuchBucketPolicy "$POLICY_ERR"; then
    EXISTING_POLICY=""
else
    echo "error: could not read the bucket policy of $BUCKET:" >&2
    cat "$POLICY_ERR" >&2
    rm -f "$POLICY_ERR"
    exit 1
fi
rm -f "$POLICY_ERR"
if [ -n "$EXISTING_POLICY" ] && [ "$EXISTING_POLICY" != "None" ]; then
    printf '%s' "$EXISTING_POLICY" > "$POLICY_BACKUP"
    echo "existing bucket policy saved to $POLICY_BACKUP"
fi
NEW_POLICY=$(EXISTING_POLICY="$EXISTING_POLICY" python3 - "$BUCKET" "$ACCOUNT" "$BUCKET_POLICY_SID" <<'PY'
import json, os, sys
bucket, account, sid = sys.argv[1], sys.argv[2], sys.argv[3]
existing = os.environ.get("EXISTING_POLICY", "").strip()
policy = json.loads(existing) if existing and existing != "None" else {"Version": "2012-10-17", "Statement": []}
policy["Statement"] = [s for s in policy.get("Statement", []) if s.get("Sid") != sid]
policy["Statement"].append({
    "Sid": sid,
    "Effect": "Allow",
    "Principal": {"AWS": "*"},
    "Action": "s3:GetObject",
    "Resource": f"arn:aws:s3:::{bucket}/*",
    "Condition": {"StringEquals": {"s3:DataAccessPointAccount": account}}})
json.dump(policy, sys.stdout, indent=2)
PY
)
echo "$NEW_POLICY"
if [ "$ASSUME_YES" != "1" ]; then
    read -r -p "Apply this bucket policy to $BUCKET? [y/N] " reply
    [ "$reply" = "y" ] || [ "$reply" = "Y" ] || { echo "stopped before the bucket policy change"; exit 1; }
fi
awsx s3api put-bucket-policy --bucket "$BUCKET" --policy "$NEW_POLICY"
echo "bucket policy applied"

say "Access point policy (CloudFront gets GetObject for this distribution only)"
AP_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "AllowCloudFrontOAC",
    "Effect": "Allow",
    "Principal": {"Service": "cloudfront.amazonaws.com"},
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:$REGION:$ACCOUNT:accesspoint/$AP_NAME/object/*",
    "Condition": {"StringEquals": {"AWS:SourceArn": "$DIST_ARN"}}
  }]
}
EOF
)
awsx s3control put-access-point-policy --account-id "$ACCOUNT" --name "$AP_NAME" \
    --region "$REGION" --policy "$AP_POLICY"
echo "access point policy applied"

# --------------------------------------------------------------------- summary
cat > "$CONFIG_FILE" <<EOF
# Generated by setup-cloudfront-cdn.sh $(date -u +%Y-%m-%dT%H:%M:%SZ)
CDN_DOMAIN=$DIST_DOMAIN
CDN_KEY_PAIR_ID=$PUBKEY_ID
CDN_PRIVATE_KEY=$PRIVATE_KEY
CDN_DISTRIBUTION_ID=$DIST_ID
CDN_ALLOWED_CIDRS=$ALLOWED_CIDRS
CDN_BUCKET=$BUCKET
CDN_REGION=$REGION
CDN_PREFIX=$PREFIX
EOF

say "Done"
cat "$CONFIG_FILE"
cat <<EOF

Next steps:
  1. Wait until the distribution is deployed (5 to 15 min):
       aws cloudfront wait distribution-deployed --id $DIST_ID --profile $ADMIN_PROFILE
  2. Enroll in a CloudFront flat-rate pricing plan (Pro: \$15 a month, 50 TB)
     in the CloudFront console, on the "Pricing plans" page. Without a plan,
     you pay about \$0.085 for each GB.
  3. Smoke test. Expect 403 "Missing Key". That proves that the signed
     URLs are enforced:
       curl -si "https://$DIST_DOMAIN/anything" | head -5
  4. Add these lines to the .env file of Fitzflix, and restart the workers:
       AWS_DOWNLOAD_VIA_CDN=1
       CDN_DOMAIN=$DIST_DOMAIN
       CDN_KEY_PAIR_ID=$PUBKEY_ID
       CDN_PRIVATE_KEY=$PRIVATE_KEY
  5. Test one signed URL against a restored object:
       flask aws cdn-url "untouched/<key>" | xargs curl -sI | head -5
EOF
