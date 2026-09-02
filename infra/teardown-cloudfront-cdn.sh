#!/bin/bash
#
# teardown-cloudfront-cdn.sh: remove each resource that
# setup-cloudfront-cdn.sh made, in dependency order. Run it when the
# rebuild is complete. The local key pair and the config in KEY_DIR stay
# on disk. Delete them yourself when you no longer need them.
#
# Usage:
#   ADMIN_PROFILE=myadmin ./teardown-cloudfront-cdn.sh
#
# The script reads BUCKET, REGION, and PREFIX from KEY_DIR/cdn-config.env
# (the setup script writes it) when the file exists. The environment
# variables override it. To disable and delete the distribution takes 10
# to 20 minutes.
#
# Remember to remove AWS_DOWNLOAD_VIA_CDN and the CDN_* lines from the
# .env file of Fitzflix. With the flag set and the distribution gone,
# each restore download fails.

set -euo pipefail

ADMIN_PROFILE=${ADMIN_PROFILE:?set ADMIN_PROFILE to an AWS profile with admin permissions}
KEY_DIR=${KEY_DIR:-$HOME/.fitzflix_cdn}
CONFIG_FILE="$KEY_DIR/cdn-config.env"

CDN_BUCKET=""; CDN_REGION=""; CDN_PREFIX=""
[ -f "$CONFIG_FILE" ] && . "$CONFIG_FILE"

BUCKET=${BUCKET:-$CDN_BUCKET}
[ -n "$BUCKET" ] || { echo "error: set BUCKET (there is no $CONFIG_FILE to read it from)" >&2; exit 1; }
REGION=${REGION:-${CDN_REGION:-us-east-1}}
PREFIX=${PREFIX:-${CDN_PREFIX:-fitzflix-cdn}}
ASSUME_YES=${ASSUME_YES:-0}

AP_NAME="${PREFIX//_/-}-ap"
BUCKET_POLICY_SID="FitzflixCdnDelegateToAccessPoints"

awsx() { aws --profile "$ADMIN_PROFILE" --output text "$@"; }
say()  { printf '\n==> %s\n' "$*"; }

ACCOUNT=$(awsx sts get-caller-identity --query Account)

if [ "$ASSUME_YES" != "1" ]; then
    read -r -p "Tear down all '$PREFIX' CDN resources in account $ACCOUNT? [y/N] " reply
    [ "$reply" = "y" ] || [ "$reply" = "Y" ] || { echo "stopped"; exit 1; }
fi

# ---------------------------------------------------------------- distribution
say "Distribution"
DIST_ID=$(awsx cloudfront list-distributions \
    --query "DistributionList.Items[?Comment=='$PREFIX'].Id | [0]" 2>/dev/null || true)
if [ -n "$DIST_ID" ] && [ "$DIST_ID" != "None" ]; then
    ETAG=$(awsx cloudfront get-distribution-config --id "$DIST_ID" --query 'ETag')
    aws --profile "$ADMIN_PROFILE" cloudfront get-distribution-config --id "$DIST_ID" \
        --query 'DistributionConfig' --output json > "$KEY_DIR/dist-config-teardown.json"
    ENABLED=$(python3 -c "import json;print(json.load(open('$KEY_DIR/dist-config-teardown.json'))['Enabled'])")
    if [ "$ENABLED" = "True" ]; then
        python3 - "$KEY_DIR/dist-config-teardown.json" <<'PY'
import json, sys
path = sys.argv[1]
cfg = json.load(open(path))
cfg["Enabled"] = False
json.dump(cfg, open(path, "w"))
PY
        awsx cloudfront update-distribution --id "$DIST_ID" --if-match "$ETAG" \
            --distribution-config "file://$KEY_DIR/dist-config-teardown.json" --query 'Distribution.Id' >/dev/null
        echo "disabled distribution $DIST_ID; waiting for the deployment (10 to 20 min)..."
    else
        echo "distribution $DIST_ID is already disabled; waiting until it is deployed..."
    fi
    aws --profile "$ADMIN_PROFILE" cloudfront wait distribution-deployed --id "$DIST_ID"
    ETAG=$(awsx cloudfront get-distribution-config --id "$DIST_ID" --query 'ETag')
    awsx cloudfront delete-distribution --id "$DIST_ID" --if-match "$ETAG"
    echo "deleted distribution $DIST_ID"
else
    echo "no distribution found"
fi

# -------------------------------------------------------------------- key group
say "Key group"
KEYGROUP_ID=$(awsx cloudfront list-key-groups \
    --query "KeyGroupList.Items[?KeyGroup.KeyGroupConfig.Name=='$PREFIX-keygroup'].KeyGroup.Id | [0]")
if [ -n "$KEYGROUP_ID" ] && [ "$KEYGROUP_ID" != "None" ]; then
    ETAG=$(awsx cloudfront get-key-group --id "$KEYGROUP_ID" --query 'ETag')
    awsx cloudfront delete-key-group --id "$KEYGROUP_ID" --if-match "$ETAG"
    echo "deleted key group"
else
    echo "no key group found"
fi

# ------------------------------------------------------------------- public key
say "Public key"
PUBKEY_ID=$(awsx cloudfront list-public-keys \
    --query "PublicKeyList.Items[?Name=='$PREFIX-pubkey'].Id | [0]")
if [ -n "$PUBKEY_ID" ] && [ "$PUBKEY_ID" != "None" ]; then
    ETAG=$(awsx cloudfront get-public-key --id "$PUBKEY_ID" --query 'ETag')
    awsx cloudfront delete-public-key --id "$PUBKEY_ID" --if-match "$ETAG"
    echo "deleted public key"
else
    echo "no public key found"
fi

# -------------------------------------------------------------------------- OAC
say "Origin access control"
OAC_ID=$(awsx cloudfront list-origin-access-controls \
    --query "OriginAccessControlList.Items[?Name=='$PREFIX-oac'].Id | [0]")
if [ -n "$OAC_ID" ] && [ "$OAC_ID" != "None" ]; then
    ETAG=$(awsx cloudfront get-origin-access-control --id "$OAC_ID" --query 'ETag')
    awsx cloudfront delete-origin-access-control --id "$OAC_ID" --if-match "$ETAG"
    echo "deleted OAC"
else
    echo "no OAC found"
fi

# -------------------------------------------------------------------------- WAF
say "WAF"
WEBACL_ID=$(awsx wafv2 list-web-acls --scope CLOUDFRONT --region us-east-1 \
    --query "WebACLs[?Name=='$PREFIX-acl'].Id | [0]")
if [ -n "$WEBACL_ID" ] && [ "$WEBACL_ID" != "None" ]; then
    LOCK=$(awsx wafv2 get-web-acl --scope CLOUDFRONT --region us-east-1 \
        --name "$PREFIX-acl" --id "$WEBACL_ID" --query 'LockToken')
    awsx wafv2 delete-web-acl --scope CLOUDFRONT --region us-east-1 \
        --name "$PREFIX-acl" --id "$WEBACL_ID" --lock-token "$LOCK"
    echo "deleted WebACL"
else
    echo "no WebACL found"
fi
IPSET_ID=$(awsx wafv2 list-ip-sets --scope CLOUDFRONT --region us-east-1 \
    --query "IPSets[?Name=='$PREFIX-ipset'].Id | [0]")
if [ -n "$IPSET_ID" ] && [ "$IPSET_ID" != "None" ]; then
    LOCK=$(awsx wafv2 get-ip-set --scope CLOUDFRONT --region us-east-1 \
        --name "$PREFIX-ipset" --id "$IPSET_ID" --query 'LockToken')
    awsx wafv2 delete-ip-set --scope CLOUDFRONT --region us-east-1 \
        --name "$PREFIX-ipset" --id "$IPSET_ID" --lock-token "$LOCK"
    echo "deleted IP set"
else
    echo "no IP set found"
fi

# ----------------------------------------------------------------- access point
say "Access point"
if awsx s3control get-access-point --account-id "$ACCOUNT" --name "$AP_NAME" \
    --region "$REGION" --query 'Name' >/dev/null 2>&1; then
    awsx s3control delete-access-point --account-id "$ACCOUNT" --name "$AP_NAME" --region "$REGION"
    echo "deleted access point $AP_NAME"
else
    echo "no access point found"
fi

# ---------------------------------------------------------------- bucket policy
say "Bucket policy (removing the delegation statement)"
EXISTING_POLICY=$(awsx s3api get-bucket-policy --bucket "$BUCKET" --query 'Policy' 2>/dev/null || echo "")
if [ -n "$EXISTING_POLICY" ] && [ "$EXISTING_POLICY" != "None" ]; then
    NEW_POLICY=$(EXISTING_POLICY="$EXISTING_POLICY" python3 - "$BUCKET_POLICY_SID" <<'PY'
import json, os, sys
policy = json.loads(os.environ["EXISTING_POLICY"])
policy["Statement"] = [s for s in policy.get("Statement", [])
                       if s.get("Sid") != sys.argv[1]]
print(json.dumps(policy) if policy["Statement"] else "")
PY
)
    if [ -n "$NEW_POLICY" ]; then
        awsx s3api put-bucket-policy --bucket "$BUCKET" --policy "$NEW_POLICY"
        echo "removed the delegation statement from the bucket policy"
    else
        awsx s3api delete-bucket-policy --bucket "$BUCKET"
        echo "the bucket policy held only our statement; deleted the policy"
    fi
else
    echo "no bucket policy present"
fi

say "Teardown complete"
echo "The local files in $KEY_DIR (private key, config, policy backups) were kept."
echo "Remove AWS_DOWNLOAD_VIA_CDN and the CDN_* lines from .env, and restart the workers."
